"""Evidence grouping and candidate drafting for the weekly retrospective.

Mining evidence bar: a candidate rule needs >= ``min_root_incidents``
*distinct root incidents* before it is worth a seat call -- e.g. two separate
fix-up PR pairs touching the same file. A fix-up pair's root incident is its
original PR (the PR that introduced the defect); a SessionLearning note's
root incident is resolved to whichever pair's PR it references. This means a
single fix-up pair plus a note about that SAME PR is only ONE root incident
and does NOT qualify. ``find_fixup_pairs`` pairing is a co-editing
*correlation*, not defect attribution on its own; the root-incident floor and
the requirement that prompts carry compact diff/review excerpts (not just PR
titles) keep single-coincidence noise out of the gotcha registry.

Anti-Goodhart framing: this job is not scored by how many candidates it
produces. Zero proposals in a given week is a successful outcome -- it means
no repeated mistake pattern cleared the evidence bar that week, not that the
job failed to find one. Never loosen the evidence bar, the per-PR candidate
cap, or the exclusions below to manufacture output. Retrospective-generated
PRs, weekly telemetry-rollup PRs, and instruction-file- or generated-
telemetry-only fix-up pairs are excluded from ordinary defect-evidence mining
(:func:`filter_defect_evidence_contexts`) so the job does not treat its own
weekly housekeeping output as evidence of an agent mistake.
"""

from __future__ import annotations

import fnmatch
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import timezone, date, datetime
from typing import Any

from corral.retro.bridge.readers import merge_bridge_groups, render_bridge_evidence
from corral.retro.types import BridgeEvidence, EvidenceGroup, FixupPairContext

MIN_ROOT_INCIDENTS = 2  # minimum DISTINCT ROOT INCIDENTS required to qualify
MAX_CANDIDATES_PER_WEEKLY_PR = 3  # cap on drafted candidates per weekly PR
ALLOWED_CONTROL_TYPES = {
    "hook",
    "labeler",
    "lint",
    "prompt_only",
    "regression_test",
    "schema_validator",
}
DEFAULT_SEVERITIES = ("info", "P2", "P1", "P0")
DEFAULT_WORKFLOW_KIND = "fix-issue"

#: Retrospective-generated PRs and weekly telemetry-rollup PRs correlate with
#: themselves/each other by construction (they touch the same tracked artifact
#: files every week) -- mining them as "defect evidence" would create a
#: self-referential feedback loop, not surface a real agent mistake. Matched
#: case-insensitively as a substring of either PR title in a fix-up pair.
DEFAULT_IGNORED_TITLE_PATTERNS = (
    "weekly gotcha retrospective",
    "weekly agent rollup",
)

#: A fix-up pair whose ONLY shared files are instruction docs or generated
#: telemetry/memory artifacts is a co-editing correlation on housekeeping
#: files, not defect evidence -- e.g. two unrelated PRs that both happened to
#: touch AGENTS.md, or two weeks of the same generated telemetry rollup.
INSTRUCTION_FILE_GLOBS = (
    "CLAUDE.md",
    "AGENTS.md",
    "README.md",
    "CLAUDE/*",
    "wiki/*",
)
GENERATED_TELEMETRY_GLOBS = (
    "agent_telemetry/*",
    "agent_memory/gotchas.json",
)
DEFAULT_IGNORED_PATH_GLOBS = INSTRUCTION_FILE_GLOBS + GENERATED_TELEMETRY_GLOBS

#: Instruction-file-only fix-up pairs normally look like housekeeping churn,
#: except when their titles identify the recurring cross-PR ratchet-collision
#: defect class. Generated-telemetry-only pairs never receive this carve-out.
RATCHET_COLLISION_TITLE_MARKERS = (
    "ratchet",
    "merged-tree",
    "shrink",
    "instruction budget",
)


def _isoformat(value: Any) -> str:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def build_contexts(fixup_rows: Sequence[Mapping[str, Any]]) -> list[FixupPairContext]:
    """Build contexts from ``find_fixup_pairs`` rows (live-fetch or parquet).

    Rows carry ``shared_files`` as a list and optional PR titles; excerpts
    fetched separately (:mod:`corral.retro.evidence`) carry the load-bearing
    context, not titles.
    """
    contexts: list[FixupPairContext] = []
    for row in fixup_rows:
        contexts.append(
            FixupPairContext(
                original_pr=int(row["original_pr"]),
                original_author=str(row["original_author"]),
                fixup_pr=int(row["fixup_pr"]),
                fixup_author=str(row["fixup_author"]),
                days_between=float(row["days_between"]),
                shared_files=tuple(row["shared_files"]),
                agent=str(row["agent"]),
                area=str(row["area"]),
                original_title=str(row.get("original_title", "")),
                fixup_title=str(row.get("fixup_title", "")),
            )
        )
    return contexts


def is_excluded_from_mining(
    context: FixupPairContext,
    *,
    ignored_title_patterns: Sequence[str] = DEFAULT_IGNORED_TITLE_PATTERNS,
    ignored_path_globs: Sequence[str] = DEFAULT_IGNORED_PATH_GLOBS,
) -> bool:
    """True if this fix-up pair is excluded from defect-evidence mining.

    A retrospective/telemetry-rollup PR on either side of the pair, or the
    pair's only shared files being housekeeping paths. Anti-Goodhart: the
    retrospective must not treat its own weekly output as evidence of an
    agent mistake.
    """
    titles = f"{context.original_title} {context.fixup_title}".lower()
    if any(pattern.lower() in titles for pattern in ignored_title_patterns):
        return True
    if not context.shared_files:
        return False
    if not all(
        any(fnmatch.fnmatch(path, pattern) for pattern in ignored_path_globs)
        for path in context.shared_files
    ):
        return False
    if _is_instruction_file_only_pair(context) and _looks_like_ratchet_collision(titles):
        return False
    return True


def _is_instruction_file_only_pair(context: FixupPairContext) -> bool:
    return bool(context.shared_files) and all(
        any(fnmatch.fnmatch(path, pattern) for pattern in INSTRUCTION_FILE_GLOBS)
        for path in context.shared_files
    )


def _looks_like_ratchet_collision(titles: str) -> bool:
    return any(marker in titles for marker in RATCHET_COLLISION_TITLE_MARKERS)


def filter_defect_evidence_contexts(
    contexts: Sequence[FixupPairContext],
    *,
    ignored_title_patterns: Sequence[str] = DEFAULT_IGNORED_TITLE_PATTERNS,
    ignored_path_globs: Sequence[str] = DEFAULT_IGNORED_PATH_GLOBS,
) -> list[FixupPairContext]:
    """Drop fix-up pairs excluded from ordinary defect-evidence mining."""
    return [
        context
        for context in contexts
        if not is_excluded_from_mining(
            context,
            ignored_title_patterns=ignored_title_patterns,
            ignored_path_globs=ignored_path_globs,
        )
    ]


def _group_key(context: FixupPairContext) -> str:
    """Deterministic clustering signature: (agent, alphabetically-first shared file).

    A deliberately simple heuristic, not semantic dedup -- two pairs that share
    multiple files but disagree on the alphabetically-first one land in separate
    groups. Documented as an accepted v1 limitation.
    """
    primary_file = min(context.shared_files) if context.shared_files else "(no-shared-file)"
    return f"{context.agent}::{primary_file}"


def group_evidence(
    contexts: Sequence[FixupPairContext],
    session_learning_notes_by_pr: Mapping[int, Sequence[str]] | None = None,
    bridge_evidence: Sequence[BridgeEvidence] | None = None,
    *,
    ignored_title_patterns: Sequence[str] = DEFAULT_IGNORED_TITLE_PATTERNS,
    ignored_path_globs: Sequence[str] = DEFAULT_IGNORED_PATH_GLOBS,
) -> list[EvidenceGroup]:
    """Cluster fix-up pairs into evidence groups and attach matching
    SessionLearning notes (if any), tagged with the PR each note was filed
    against so ``EvidenceGroup.root_incident_ids`` can collapse same-incident
    artifacts.

    Applies :func:`filter_defect_evidence_contexts` first: retrospective/
    telemetry-rollup PRs and housekeeping-file-only pairs never enter a group.
    """
    notes_by_pr = session_learning_notes_by_pr or {}
    buckets: dict[str, list[FixupPairContext]] = {}
    for context in filter_defect_evidence_contexts(
        contexts,
        ignored_title_patterns=ignored_title_patterns,
        ignored_path_globs=ignored_path_globs,
    ):
        buckets.setdefault(_group_key(context), []).append(context)

    groups: list[EvidenceGroup] = []
    for key, pairs in buckets.items():
        note_entries: list[tuple[int, str]] = []
        for pair in pairs:
            for pr_number in (pair.original_pr, pair.fixup_pr):
                note_entries.extend((pr_number, note) for note in notes_by_pr.get(pr_number, []))
        deduped = list(dict.fromkeys(note_entries))
        groups.append(
            EvidenceGroup(
                key=key,
                agent=pairs[0].agent,
                area=pairs[0].area,
                pairs=tuple(pairs),
                extra_notes=tuple(note for _, note in deduped),
                note_source_prs=tuple(pr for pr, _ in deduped),
            )
        )
    return merge_bridge_groups(groups, bridge_evidence or ())


def qualifying_groups(
    groups: Sequence[EvidenceGroup], min_root_incidents: int = MIN_ROOT_INCIDENTS
) -> list[EvidenceGroup]:
    """Return only groups meeting the distinct-root-incident floor,
    most-evidenced first."""
    qualified = [g for g in groups if g.evidence_count >= min_root_incidents]
    return sorted(qualified, key=lambda g: (-g.evidence_count, g.key))


@dataclass(frozen=True)
class GotchaCandidate:
    """A drafted candidate rule, before id assignment."""

    rule: str
    workflow_kinds: list[str]
    repo_paths: list[str]
    surface_ids: list[str]
    source_prs: list[int]
    control_type: str
    control_path: str | None
    inject_into_briefer: bool
    confidence: float
    rationale: str
    severity: str
    created: str
    evidence_key: str
    source_refs: list[str] = field(default_factory=list)


def build_prompt(
    group: EvidenceGroup,
    excerpts: Mapping[int, str],
    *,
    allowed_severities: Sequence[str] = DEFAULT_SEVERITIES,
    severe_severities: Sequence[str] = (),
) -> str:
    """Build the drafter prompt from a qualifying evidence group's compact
    excerpts.

    ``excerpts`` maps PR number -> a compact diff/review/CI excerpt string;
    pairs are described with their shared files and, where available, real
    excerpt text -- never titles alone.
    """
    pair_blocks = []
    for pair in group.pairs:
        original_excerpt = excerpts.get(pair.original_pr, "").strip()
        fixup_excerpt = excerpts.get(pair.fixup_pr, "").strip()
        pair_blocks.append(
            f"- Original PR #{pair.original_pr} by `{pair.original_author}` "
            f"({pair.original_title or 'no title available'})\n"
            f"  Fix-up PR #{pair.fixup_pr} by `{pair.fixup_author}`, "
            f"{pair.days_between:.1f} days later "
            f"({pair.fixup_title or 'no title available'})\n"
            f"  Shared files: {', '.join(pair.shared_files) or '(none recorded)'}\n"
            f"  Original PR excerpt:\n{original_excerpt or '(unavailable)'}\n"
            f"  Fix-up PR excerpt:\n{fixup_excerpt or '(unavailable)'}"
        )
    notes_block = "\n".join(f"- {note}" for note in group.extra_notes) or "(none)"
    bridge_block = render_bridge_evidence(group.bridge_evidence)
    allowed_controls = ", ".join(sorted(ALLOWED_CONTROL_TYPES))
    allowed_severity_list = ", ".join(sorted(allowed_severities))
    severity_note = ""
    if severe_severities:
        severe = "/".join(sorted(severe_severities))
        severity_note = (
            f" -- {severe} means this should file an immediate review issue "
            "rather than wait for the weekly PR"
        )
    return (
        "You extract durable AI-agent gotchas from fix-up PR evidence for a weekly "
        f"retrospective. This candidate has already met a >={MIN_ROOT_INCIDENTS}-"
        "distinct-root-incident bar (co-editing correlation is a LEAD, not proof "
        "of a defect) -- your job is to decide whether the evidence below actually "
        "supports one concrete, reusable rule, and to draft it precisely. This "
        "retrospective is not scored by how many rules it produces -- if the "
        "evidence is weak or coincidental, say so with low confidence rather than "
        "inventing a rule to justify the evidence group.\n\n"
        f"Agent: `{group.agent}` | Area: `{group.area}`\n\n"
        "## Fix-up pairs\n" + "\n".join(pair_blocks) + "\n\n"
        f"## SessionLearning notes on these PRs\n{notes_block}\n\n"
        f"## Sanitized file-backed bridge evidence\n{bridge_block}\n\n"
        "Return ONLY a JSON object with these fields:\n"
        "- rule: one precise, actionable sentence (what an agent should ALWAYS or "
        "NEVER do)\n"
        "- workflow_kinds: array of applicable agent workflow kinds, e.g. "
        '["fix-issue", "pr-review"]\n'
        "- repo_paths: array of repo-relative file path globs this rule applies to "
        "(use the shared files above as a starting point)\n"
        "- surface_ids: array of surfaces.yaml keys this rule applies to, "
        "if any are clearly implicated (else [])\n"
        f"- control_type: one of {allowed_controls}\n"
        "- control_path: repo-relative path to the control file if one already "
        "exists, else null\n"
        "- inject_into_briefer: boolean\n"
        "- confidence: float 0.0-1.0 -- use LOW confidence when the evidence is "
        "only a file-overlap correlation without a clear causal story\n"
        "- rationale: one or two sentences citing the specific evidence above\n"
        f"- severity: one of {allowed_severity_list}{severity_note}\n\n"
        "Do not invent facts not present in the evidence above."
    )


def normalize_candidate(
    payload: dict[str, Any],
    group: EvidenceGroup,
    *,
    created_on: date | None = None,
    allowed_severities: Sequence[str] = DEFAULT_SEVERITIES,
) -> GotchaCandidate:
    rule = str(payload.get("rule", "")).strip()
    if not rule:
        raise ValueError("Candidate gotcha is missing non-empty `rule`")
    confidence = _coerce_confidence(payload.get("confidence"))
    workflow_kinds = _coerce_string_list(
        payload.get("workflow_kinds"), default=[DEFAULT_WORKFLOW_KIND]
    )
    repo_paths = _coerce_string_list(payload.get("repo_paths"), default=[])
    surface_ids = _coerce_string_list(payload.get("surface_ids"), default=[])
    control_type = str(payload.get("control_type", "prompt_only")).strip()
    if control_type not in ALLOWED_CONTROL_TYPES:
        control_type = "prompt_only"
    severity = _normalize_severity(payload.get("severity"), allowed_severities)
    created = (created_on or datetime.now(timezone.utc).date()).isoformat()
    return GotchaCandidate(
        rule=rule,
        workflow_kinds=workflow_kinds,
        repo_paths=repo_paths,
        surface_ids=surface_ids,
        source_prs=group.pr_numbers,
        source_refs=[record.source_ref for record in group.bridge_evidence],
        control_type=control_type,
        control_path=_coerce_optional_string(payload.get("control_path")),
        inject_into_briefer=bool(payload.get("inject_into_briefer", True)),
        confidence=confidence,
        rationale=str(payload.get("rationale", "")).strip(),
        severity=severity,
        created=created,
        evidence_key=group.key,
    )


def _normalize_severity(value: Any, allowed_severities: Sequence[str]) -> str:
    """Case-insensitive membership check that keeps the configured casing.

    The source stored uppercase priority levels but the literal ``info``
    fallback; canonical strings come from the configured list.
    """
    canonical = {str(item).upper(): str(item) for item in allowed_severities}
    return canonical.get(str(value).strip().upper(), "info")


def candidate_to_json(candidate: GotchaCandidate) -> str:
    return json.dumps(asdict(candidate), indent=2, sort_keys=True)


def _coerce_confidence(value: Any) -> float:
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise ValueError("Candidate gotcha is missing numeric `confidence`")
    return max(0.0, min(1.0, float(value)))


def _coerce_string_list(value: Any, *, default: list[str]) -> list[str]:
    if not isinstance(value, list):
        return list(default)
    cleaned = [str(item).strip() for item in value if str(item).strip()]
    return cleaned or list(default)


def _coerce_optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


__all__ = [
    "ALLOWED_CONTROL_TYPES",
    "DEFAULT_IGNORED_PATH_GLOBS",
    "DEFAULT_IGNORED_TITLE_PATTERNS",
    "DEFAULT_SEVERITIES",
    "DEFAULT_WORKFLOW_KIND",
    "GENERATED_TELEMETRY_GLOBS",
    "INSTRUCTION_FILE_GLOBS",
    "MAX_CANDIDATES_PER_WEEKLY_PR",
    "MIN_ROOT_INCIDENTS",
    "RATCHET_COLLISION_TITLE_MARKERS",
    "BridgeEvidence",
    "EvidenceGroup",
    "FixupPairContext",
    "GotchaCandidate",
    "build_contexts",
    "build_prompt",
    "candidate_to_json",
    "filter_defect_evidence_contexts",
    "group_evidence",
    "is_excluded_from_mining",
    "normalize_candidate",
    "qualifying_groups",
]
