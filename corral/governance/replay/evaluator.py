"""Deterministic retrieval-replay evaluation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..manifest.evaluator import resolve_bundle
from ..manifest.model import Manifest
from .corpus import Corpus, CorpusCase
from .triggers import MatchResult, TriggerRules, match_rules, topic_loads

TokenFn = Callable[[str], int]


@dataclass(frozen=True)
class Finding:
    severity: str
    case_ref: str
    message: str


@dataclass(frozen=True)
class CaseResult:
    case: CorpusCase
    match: MatchResult
    missing: tuple[str, ...]
    forbidden_hits: tuple[str, ...]
    bundle_tokens: int
    token_ceiling: int
    findings: tuple[Finding, ...]

    @property
    def expected_n(self) -> int:
        return len(self.case.expected_loads)

    @property
    def matched_expected_n(self) -> int:
        return self.expected_n - len(self.missing)


@dataclass(frozen=True)
class CorpusResult:
    case_results: tuple[CaseResult, ...]
    overall_recall: float
    tier_recall: dict[str, float]
    min_overall_recall: float
    findings: tuple[Finding, ...]

    @property
    def fail_findings(self) -> tuple[Finding, ...]:
        return tuple(finding for finding in self.findings if finding.severity == "FAIL")

    @property
    def ok(self) -> bool:
        return not self.fail_findings


def always_bundle_paths(manifest: Manifest, profile_id: str) -> set[str]:
    profile = manifest.profiles[profile_id]
    return {manifest.units[uid].path for uid in resolve_bundle(profile, manifest.units)}


def bundle_tokens(
    root: Path, paths: set[str] | frozenset[str], token_fn: TokenFn
) -> tuple[int, list[str]]:
    total = 0
    missing_files: list[str] = []
    for relative in sorted(paths):
        target = root / relative
        if not target.is_file():
            missing_files.append(relative)
        else:
            total += token_fn(target.read_text(encoding="utf-8"))
    return total, missing_files


def evaluate_case(
    case: CorpusCase,
    rules: TriggerRules,
    always_paths: set[str],
    root: Path,
    token_fn: TokenFn,
    *,
    topic_prefixes: list[str] | tuple[str, ...] = (),
    critical_tiers: set[str] | frozenset[str] = frozenset(),
    token_ceilings: dict[str, int] | None = None,
) -> CaseResult:
    match = match_rules(rules, list(case.touched_paths), case.task_text)
    expected = set(case.expected_loads)
    forbidden = set(case.forbidden_loads)
    matched = set(match.matched_loads)
    missing = expected - matched
    # Compare canonical on-disk identities so the forbidden guard cannot be
    # bypassed by respelling a load path ("./x", "a//b", "../a/b", case on
    # case-insensitive filesystems, or a symlink alias of the same file).
    matched_canonical = {(root / path).resolve() for path in matched}
    forbidden_hits = {
        path for path in forbidden if (root / path).resolve() in matched_canonical
    }
    missing_topics = topic_loads(missing, topic_prefixes)
    tokens, missing_files = bundle_tokens(root, always_paths | matched, token_fn)
    configured = (token_ceilings or {}).get(case.tier)
    ceiling = min(case.max_bundle_tokens, configured) if configured else case.max_bundle_tokens

    findings: list[Finding] = []
    if forbidden_hits:
        findings.append(
            Finding(
                "FAIL",
                case.ref,
                f"forbidden load(s) triggered: {sorted(forbidden_hits)} "
                f"(fired rules: {sorted(match.fired_rule_ids)}).",
            )
        )
    if missing_topics:
        findings.append(
            Finding(
                "FAIL",
                case.ref,
                f"missing topic trigger(s): {sorted(missing_topics)} "
                "(a configured topic file the case expects was not retrieved).",
            )
        )
    if case.tier in critical_tiers and missing:
        findings.append(
            Finding(
                "FAIL",
                case.ref,
                f"{case.tier} case missing expected load(s): {sorted(missing)} "
                "(configured critical tiers require 100% recall).",
            )
        )
    if missing_files:
        findings.append(
            Finding(
                "FAIL",
                case.ref,
                f"bundle references path(s) not on disk: {missing_files}.",
            )
        )
    if tokens > ceiling:
        findings.append(
            Finding(
                "FAIL",
                case.ref,
                f"effective bundle {tokens} tokens > {case.tier} ceiling {ceiling} "
                "(over-retrieval).",
            )
        )
    return CaseResult(
        case=case,
        match=match,
        missing=tuple(sorted(missing)),
        forbidden_hits=tuple(sorted(forbidden_hits)),
        bundle_tokens=tokens,
        token_ceiling=ceiling,
        findings=tuple(findings),
    )


def evaluate_corpus(
    corpus: Corpus,
    rules: TriggerRules,
    manifest: Manifest,
    root: Path,
    token_fn: TokenFn,
    min_overall_recall: float = 0.95,
    *,
    topic_prefixes: list[str] | tuple[str, ...] = (),
    critical_tiers: set[str] | frozenset[str] = frozenset(),
    token_ceilings: dict[str, int] | None = None,
) -> CorpusResult:
    always_paths = always_bundle_paths(manifest, corpus.profile)
    case_results = tuple(
        evaluate_case(
            case,
            rules,
            always_paths,
            root,
            token_fn,
            topic_prefixes=topic_prefixes,
            critical_tiers=critical_tiers,
            token_ceilings=token_ceilings,
        )
        for case in corpus.cases
    )
    total_expected = sum(result.expected_n for result in case_results)
    total_matched = sum(result.matched_expected_n for result in case_results)
    overall = total_matched / total_expected if total_expected else 1.0

    tier_recall: dict[str, float] = {}
    for tier in sorted({case.tier for case in corpus.cases}):
        expected_n = sum(
            result.expected_n for result in case_results if result.case.tier == tier
        )
        matched_n = sum(
            result.matched_expected_n
            for result in case_results
            if result.case.tier == tier
        )
        if expected_n:
            tier_recall[tier] = matched_n / expected_n

    findings = [finding for result in case_results for finding in result.findings]
    if overall < min_overall_recall:
        findings.append(
            Finding(
                "FAIL",
                "corpus",
                f"overall expected-load recall {overall:.4f} < required "
                f"{min_overall_recall:.4f}.",
            )
        )
    return CorpusResult(
        case_results,
        overall,
        tier_recall,
        min_overall_recall,
        tuple(findings),
    )


def validate_rule_loads_against_manifest(
    rules: TriggerRules,
    manifest: Manifest,
    topic_prefixes: list[str] | tuple[str, ...],
) -> list[str]:
    unit_paths = {unit.path for unit in manifest.units.values()}
    return [
        f"trigger-rule load {path!r} is a configured topic file but not a manifest unit"
        for path in sorted(topic_loads(rules.all_load_paths(), topic_prefixes))
        if path not in unit_paths
    ]
