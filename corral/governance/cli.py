"""CLI implementation for instruction governance and deterministic replay.

Trusted-base topology
---------------------
On a pull request, validator code is policy and the head ref is untrusted data.
``corral governance check --base-ref BASE --head-ref HEAD`` therefore executes
the ``corral`` package materialized from ``BASE`` before evaluating anything.
That base validator reads both the baseline registry and ``governance:``
configuration from ``BASE``; it reads only the proposed registry/instruction
documents and diff from ``HEAD``.  CI must bootstrap this command from a
trusted checkout or otherwise protect the tiny re-exec launcher itself.  A PR
must never install and directly invoke its head version with the internal
``CORRAL_GOVERNANCE_BASE_EXECUTED`` bypass variable set.

The re-exec environment is sanitized as well: the nested interpreter receives
a ``PYTHONPATH`` pointing ONLY at the materialized BASE package, never the
ambient ``PYTHONPATH``, so HEAD-controlled directories cannot inject code
(e.g. via ``sitecustomize.py``) or shadow imports in the trusted child.

This preserves the source gate's central security property: editing validator
logic in the same PR cannot weaken the verdict that judges that PR.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass, field
from datetime import date
from io import BytesIO
from pathlib import Path
from typing import Any

import yaml

from corral.config import load_config

from .budget import lint_manifest, token_estimate_tokens
from .config import (
    DEFAULT_MANIFEST_SCHEMA,
    DEFAULT_TRIGGER_RULES_SCHEMA,
    GovernanceConfig,
    governance_config_from_document,
)
from .git_diff import GitRepository
from .manifest.model import load_manifest
from .proposals import check_governance, is_instruction_file, parse_proposal_block
from .registry import Finding, check_consistency, parse_registry
from .replay.builder import build_corpus, load_reviewed_cases, write_corpus
from .replay.corpus import load_corpus
from .replay.evaluator import (
    always_bundle_paths,
    evaluate_corpus,
    validate_rule_loads_against_manifest,
)
from .replay.triggers import load_trigger_rules

TRUSTED_MARKER = "CORRAL_GOVERNANCE_BASE_EXECUTED"


@dataclass
class Report:
    mode: str
    findings: list[Finding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(finding.severity == "FAIL" for finding in self.findings)

    def to_json(self) -> str:
        return json.dumps(
            {
                "mode": self.mode,
                "ok": self.ok,
                "findings": [
                    {
                        "severity": finding.severity,
                        "check": finding.check,
                        "message": finding.message,
                    }
                    for finding in self.findings
                ],
            },
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )


def _worktree_reader(root: Path):
    def read(path: str) -> str | None:
        target = root / path
        return target.read_text(encoding="utf-8") if target.is_file() else None

    return read


def _config_ref_path(root: Path, config_path: Path | None) -> str:
    if config_path is None:
        return "corral.yaml"
    resolved = config_path if config_path.is_absolute() else root / config_path
    try:
        return resolved.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise ValueError("CI governance config must live inside the evaluated repository") from exc


def load_base_governance_config(
    repository: GitRepository, base_ref: str, config_path: Path | None
) -> GovernanceConfig:
    relative = _config_ref_path(repository.root, config_path)
    text = repository.read_ref_file(base_ref, relative)
    document = yaml.safe_load(text) if text.strip() else {}
    return governance_config_from_document(document)


def run_local_check(root: Path, config: GovernanceConfig) -> Report:
    report = Report("check")
    registry_path = root / config.registry
    if not registry_path.is_file():
        report.findings.append(
            Finding("FAIL", "consistency", f"registry not found: {config.registry}")
        )
        return report
    registry = parse_registry(registry_path.read_text(encoding="utf-8"), config)
    report.findings.extend(check_consistency(registry, _worktree_reader(root)))
    return report


def run_gate(
    base_ref: str,
    head_ref: str,
    pr_body: str,
    repo_root: Path,
    *,
    config: GovernanceConfig | None = None,
    config_path: Path | None = None,
) -> Report:
    """Evaluate HEAD as data using base registry/config semantics."""
    repository = GitRepository(repo_root)
    cfg = config or load_base_governance_config(repository, base_ref, config_path)
    report = Report("gate")
    base_registry = parse_registry(
        repository.read_ref_file(base_ref, cfg.registry), cfg
    )
    head_registry = parse_registry(
        repository.read_ref_file(head_ref, cfg.registry), cfg
    )
    report.findings.extend(
        check_consistency(head_registry, repository.ref_reader(head_ref))
    )
    changed_paths = repository.changed_paths(base_ref, head_ref)
    instruction_changed = [
        path for path in changed_paths if is_instruction_file(path, cfg.instruction_globs)
    ]
    added_by_file = repository.added_lines_by_file(
        base_ref, head_ref, instruction_changed
    )
    proposal_block, proposal_errors = parse_proposal_block(pr_body)
    report.findings.extend(
        check_governance(
            base_registry=base_registry,
            head_registry=head_registry,
            base_read_file=repository.ref_reader(base_ref),
            changed_paths=changed_paths,
            added_by_file=added_by_file,
            proposal_block=proposal_block,
            proposal_errors=proposal_errors,
            config=cfg,
        )
    )
    return report


def _safe_extract_archive(payload: bytes, destination: Path) -> None:
    with tarfile.open(fileobj=BytesIO(payload), mode="r:") as archive:
        root = destination.resolve()
        for member in archive.getmembers():
            target = (destination / member.name).resolve()
            if target != root and root not in target.parents:
                raise RuntimeError(f"unsafe path in base archive: {member.name}")
            # Symlinks, hard links, and device/special members are rejected
            # outright: the archive is `git archive` output of a source tree,
            # so anything but files and directories means a poisoned BASE.
            # This mirrors tarfile's filter="data" (unavailable on Python
            # patch releases predating the CVE-2007-4559 backport).
            if not (member.isfile() or member.isdir()):
                raise RuntimeError(
                    f"unsupported member type in base archive: {member.name}"
                )
        try:
            archive.extractall(destination, filter="data")
        except TypeError:
            archive.extractall(destination)  # members validated above


def _trusted_base_argv(args: Any, root: Path) -> list[str]:
    argv = ["governance", "check", "--root", str(root)]
    if args.config is not None:
        argv.extend(["--config", str(args.config)])
    argv.extend(["--base-ref", args.base_ref, "--head-ref", args.head_ref])
    if args.pr_body_file is not None:
        argv.extend(["--pr-body-file", str(args.pr_body_file)])
    if args.json:
        argv.append("--json")
    return argv


def execute_from_base(args: Any, root: Path) -> int:
    """Materialize and execute validator code from BASE, never HEAD."""
    result = subprocess.run(
        ["git", "archive", "--format=tar", args.base_ref, "--", "corral"],
        cwd=root,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode(errors="replace").strip() or "git archive failed")
    with tempfile.TemporaryDirectory(prefix="corral-governance-base-") as temp_name:
        temp = Path(temp_name)
        _safe_extract_archive(result.stdout, temp)
        environment = os.environ.copy()
        # Never inherit the ambient PYTHONPATH: HEAD-controlled directories
        # on it (typically the checkout root) would execute code inside the
        # trusted child -- sitecustomize.py is imported automatically at
        # interpreter startup from any sys.path entry, and any non-stdlib
        # import could be shadowed. The nested validator needs only the
        # materialized BASE package plus the interpreter's site-packages.
        environment["PYTHONPATH"] = str(temp)
        environment[TRUSTED_MARKER] = args.base_ref
        command = [
            sys.executable,
            "-c",
            "import sys; from corral.cli import main; raise SystemExit(main(sys.argv[1:]))",
            *_trusted_base_argv(args, root),
        ]
        # Import from the extracted BASE package, not the repository cwd where
        # the untrusted HEAD package is visible as ``./corral``.
        completed = subprocess.run(command, cwd=temp, env=environment, check=False)
        return completed.returncode


def run_check_command(args: Any) -> int:
    root = Path(args.root).resolve() if args.root is not None else Path.cwd().resolve()
    if bool(args.base_ref) != bool(args.head_ref):
        sys.stderr.write("--base-ref and --head-ref must be supplied together\n")
        return 2
    try:
        if args.base_ref and os.environ.get(TRUSTED_MARKER) != args.base_ref:
            return execute_from_base(args, root)
        if args.base_ref:
            pr_body = (
                Path(args.pr_body_file).read_text(encoding="utf-8")
                if args.pr_body_file
                else ""
            )
            report = run_gate(
                args.base_ref,
                args.head_ref,
                pr_body,
                root,
                config_path=args.config,
            )
        else:
            loaded = load_config(args.config)
            root = Path(args.root).resolve() if args.root is not None else loaded.root
            report = run_local_check(root, loaded.governance)
    except Exception as exc:
        report = Report("gate" if args.base_ref else "check")
        message = " ".join(str(exc).split())
        report.findings.append(Finding("FAIL", "error", message))
        if args.json:
            print(report.to_json())
        else:
            sys.stderr.write(f"instruction-governance input error: {message}\n")
        return 2
    if args.json:
        print(report.to_json())
    else:
        for finding in report.findings:
            stream = sys.stderr if finding.severity == "FAIL" else sys.stdout
            stream.write(f"[{finding.severity}] ({finding.check}) {finding.message}\n")
        if report.ok:
            print("instruction-governance: clean")
    return 0 if report.ok else 1


def _resolve(root: Path, override: Path | None, configured: str) -> Path:
    if override is not None:
        return override if override.is_absolute() else root / override
    path = Path(configured)
    return path if path.is_absolute() else root / path


def _load_replay_inputs(args: Any):
    loaded = load_config(args.config)
    root = Path(args.root).resolve() if args.root is not None else loaded.root
    cfg = loaded.governance
    manifest_path = _resolve(root, args.manifest, cfg.replay.manifest)
    rules_path = _resolve(root, args.trigger_rules, cfg.replay.trigger_rules)
    corpus_path = _resolve(root, args.corpus, cfg.replay.corpus)
    manifest = load_manifest(manifest_path, DEFAULT_MANIFEST_SCHEMA)
    rules = load_trigger_rules(rules_path, DEFAULT_TRIGGER_RULES_SCHEMA)
    corpus = load_corpus(corpus_path)
    return root, cfg, manifest, rules, corpus


def run_replay_command(args: Any) -> int:
    try:
        root, cfg, manifest, rules, corpus = _load_replay_inputs(args)
        paths = rules.all_load_paths()
        for case in corpus.cases:
            paths.update(case.expected_loads)
            paths.update(case.forbidden_loads)
        paths.update(always_bundle_paths(manifest, corpus.profile))
        missing_paths = sorted(path for path in paths if not (root / path).is_file())
        if missing_paths:
            raise ValueError(f"referenced path(s) not on disk: {missing_paths}")
        minimum = args.min_recall if args.min_recall is not None else cfg.replay.min_recall
        result = evaluate_corpus(
            corpus,
            rules,
            manifest,
            root,
            token_estimate_tokens,
            minimum,
            topic_prefixes=cfg.replay.topic_prefixes,
            critical_tiers=frozenset(cfg.replay.critical_tiers),
            token_ceilings=cfg.budget.token_ceilings,
        )
        warnings = validate_rule_loads_against_manifest(
            rules, manifest, cfg.replay.topic_prefixes
        )
    except (OSError, ValueError, KeyError, re.error) as exc:
        sys.stderr.write(f"instruction-replay input error: {exc}\n")
        return 2
    for case_result in result.case_results:
        status = "FAIL" if case_result.findings else "ok"
        print(
            f"[{status:>4}] {case_result.case.ref} tier={case_result.case.tier} "
            f"recall={case_result.matched_expected_n}/{case_result.expected_n} "
            f"bundle={case_result.bundle_tokens}<={case_result.token_ceiling} "
            f"fired={sorted(case_result.match.fired_rule_ids)}"
        )
    print(
        f"overall expected-load recall: {result.overall_recall:.4f} "
        f"(floor {result.min_overall_recall:.2f})"
    )
    for tier in sorted(result.tier_recall):
        print(f"  {tier} recall: {result.tier_recall[tier]:.4f}")
    for warning in warnings:
        print(f"[WARN] {warning}")
    for finding in result.fail_findings:
        print(f"[FAIL] {finding.case_ref}: {finding.message}")
    if result.ok:
        print(
            f"Instruction retrieval-replay: clean ({len(corpus.cases)} cases; "
            f"overall recall {result.overall_recall:.4f})."
        )
    else:
        print("Instruction retrieval-replay: FAILED.")
    return 0 if result.ok else 1


def run_build_corpus_command(args: Any) -> int:
    try:
        loaded = load_config(args.config)
        root = Path(args.root).resolve() if args.root is not None else loaded.root
        cfg = loaded.governance
        reviewed, metadata = load_reviewed_cases(Path(args.reviewed_cases))
        manifest_path = _resolve(root, args.manifest, cfg.replay.manifest)
        rules_path = _resolve(root, args.trigger_rules, cfg.replay.trigger_rules)
        manifest = load_manifest(manifest_path, DEFAULT_MANIFEST_SCHEMA)
        rules = load_trigger_rules(rules_path, DEFAULT_TRIGGER_RULES_SCHEMA)
        profile = args.profile or metadata.get("profile") or next(iter(manifest.profiles))
        corpus = build_corpus(
            reviewed,
            root,
            manifest,
            rules,
            cfg,
            profile=profile,
            source_repo=str(metadata.get("source_repo", "")),
            generated_on=args.generated_on,
        )
        output = _resolve(root, args.output, cfg.replay.corpus)
        write_corpus(corpus, output)
    except (OSError, ValueError, KeyError, re.error) as exc:
        sys.stderr.write(f"build-corpus input error: {exc}\n")
        return 2
    print(f"Wrote {len(corpus['cases'])} cases to {output}.")
    return 0


def run_lint_budget_command(args: Any) -> int:
    try:
        loaded = load_config(args.config)
        root = Path(args.root).resolve() if args.root is not None else loaded.root
        cfg = loaded.governance
        manifest_path = _resolve(root, args.manifest, cfg.replay.manifest)
        manifest = load_manifest(manifest_path, DEFAULT_MANIFEST_SCHEMA)
        findings = lint_manifest(
            manifest,
            root,
            as_of=args.as_of or date.today(),
            token_ceilings=cfg.budget.token_ceilings,
        )
    except (OSError, ValueError, KeyError) as exc:
        sys.stderr.write(f"instruction-budget input error: {exc}\n")
        return 2
    for finding in findings:
        print(f"[{finding.severity}] {finding.message}")
    failures = [finding for finding in findings if finding.severity == "FAIL"]
    if failures:
        print(f"Instruction-budget lint: {len(failures)} failure(s).")
        return 1
    print(f"Instruction-budget lint: clean ({len(manifest.units)} units).")
    return 0


# ---------------------------------------------------------------------------
# corral governance staleness
# ---------------------------------------------------------------------------


def run_staleness_command(args: Any) -> int:
    """Quarterly instruction-staleness report.

    Write-safety discipline (same as the retrospective): the report goes to
    stdout by default; file output is opt-in via ``--output``; filing demotion
    issues on GitHub is opt-in via ``--issue-sink github``. ``--dry-run``
    prints the report and writes/files nothing regardless of other flags.
    """
    from datetime import timezone, datetime

    from corral.governance.staleness import report as staleness_report
    from corral.governance.staleness import sources

    try:
        loaded = load_config(args.config)
        if args.root is not None:
            root_override = Path(args.root).resolve()
            loaded = _ConfigWithRoot(loaded, root_override)
        as_of = args.as_of or datetime.now(timezone.utc).date()
        github = _staleness_github_client(loaded)
        run = sources.run_staleness(
            as_of=as_of, config=loaded, github=github, dry_run=bool(args.dry_run)
        )
    except (OSError, ValueError, KeyError) as exc:
        sys.stderr.write(f"instruction-staleness input error: {exc}\n")
        return 2

    dry_run = bool(args.dry_run)
    report_md = run.report_markdown
    if dry_run:
        print(report_md)
        print("\n--- DRY RUN: report not written, no issues filed ---")
        return 0

    print(report_md)

    output_path = args.output
    if output_path is not None:
        output_path = Path(output_path)
        if not output_path.is_absolute():
            output_path = loaded.root / output_path
        conflict = _staleness_output_destination_conflict(loaded, output_path)
        if conflict:
            sys.stderr.write(
                "instruction-staleness: refusing to write the report over an "
                f"instruction/proposal target: {conflict}\n"
            )
            return 2
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(report_md, encoding="utf-8")
        print(f"\nWrote {output_path}")

    if args.issue_sink == "github":
        _file_demotion_issues(loaded, run.result)
    return 0


def _staleness_output_destination_conflict(config: Any, output_path: Path) -> str | None:
    """Prevent an opt-in report artifact from aliasing governed instruction prose."""
    from corral.governance.proposals import path_matches

    root_path = Path(config.root)
    relatives: set[str] = set()
    for normalized, normalized_root in (
        (output_path.absolute(), root_path.absolute()),
        (output_path.resolve(), root_path.resolve()),
    ):
        try:
            relatives.add(normalized.relative_to(normalized_root).as_posix())
        except ValueError:
            pass
    governance = config.governance
    patterns = [
        *governance.instruction_globs,
        *governance.protected_paths,
        *config.retro.proposals.target_globs,
    ]
    if governance.staleness.demote_target_glob:
        patterns.append(governance.staleness.demote_target_glob)
    for relative in relatives:
        if relative == governance.registry or any(
            path_matches(relative, pattern) for pattern in patterns
        ):
            return relative
    return None


def _staleness_github_client(config: Any):
    from corral.retro.github import GhCliGitHub

    retro = getattr(config, "retro")
    repository = retro.repository
    if not repository:
        raise ValueError(
            "retro.repository must be configured (owner/name) to resolve merged-PR path data"
        )
    return GhCliGitHub(repository, timeout_s=retro.github.timeout_s)


def _file_demotion_issues(config: Any, result: Any) -> None:
    """File one issue per demotion candidate (opt-in sink). Never edits files."""
    from corral.governance.staleness.report import (
        render_demotion_proposal,
        render_proposal_block_yaml,
        validate_proposal_through_gate,
    )

    governance = getattr(config, "governance")
    reviewer = (governance.reviewer or "").strip()
    if not governance.staleness.demote_target_glob or not reviewer:
        print(
            "issue sink github: no demotion issues filed (demote_target_glob and "
            "governance.reviewer must both be configured)."
        )
        return
    github = _staleness_github_client(config)
    for v in result.demotion_candidates:
        proposal = render_demotion_proposal(v, cfg=governance, reviewer=reviewer)
        errors = validate_proposal_through_gate(proposal, governance.proposals)
        if errors:
            print(
                f"warning: no demotion issue filed for {v.rule_id}; proposal failed "
                f"the governance contract: {errors}"
            )
            continue
        title = f"[instruction-governance] Demote {v.rule_id} (staleness {result.quarter})"
        body = (
            f"Staleness report {result.quarter} flags `{v.rule_id}` "
            f"(`{v.rule.file}`) for demotion.\n\n"
            f"Applicability {v.demote_window.applicability:.1%} over "
            f"{governance.staleness.demote_days}d "
            f"(n={v.demote_window.denominator} evaluable sessions), below the "
            f"{governance.staleness.demote_rate:.0%} floor and not retained.\n\n"
            "Action (human): move the prose to the configured demotion target marked "
            "NON-NORMATIVE, drop the registry entry, and open the demotion PR carrying "
            f"this validated proposal block:\n\n{render_proposal_block_yaml([proposal])}\n"
        )
        try:
            url = github.create_issue(title, body)
        except Exception as exc:  # filing failure must not abort the report
            print(f"warning: failed to file demotion issue for {v.rule_id}: {exc}")
            continue
        print(f"Filed demotion proposal for {v.rule_id}: {url}")


class _ConfigWithRoot:
    """Shallow config view overriding ``root`` (for ``--root``)."""

    def __init__(self, inner: Any, root: Path) -> None:
        self._inner = inner
        self._root = root

    @property
    def root(self) -> Path:
        return self._root

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)
