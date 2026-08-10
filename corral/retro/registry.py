"""Dedup, sequential id allocation, and validated gotcha-registry writes.

Single-writer contract: ``corral retro run`` is the only writer of the
gotcha registry, intended to run from a fresh base-ref checkout once a week.
:func:`allocate_next_ids` assigns ids purely from the gotchas already present
in the file the caller loaded -- the caller is responsible for the "base
moved, fail the job" guarantee at the git level (see the base-ref check in
:mod:`corral.retro.cli`), not this module.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import date, timedelta
from itertools import combinations
from pathlib import Path
from typing import Any

from corral.memory import registry as memory_registry
from corral.retro.mining import EvidenceGroup, GotchaCandidate

_ID_RE = re.compile(r"^G-(\d{4})-(\d{3})$")
_ISSUE_TITLE_PR_PAIR_RE = re.compile(r"PR #(\d+)\s*->\s*#(\d+)")
_ISSUE_PR_REF_RE = re.compile(r"#(\d+)")
_ISSUE_SOURCE_REF_RE = re.compile(r"\b(?:memory|ai-run|bridge-incident|run-audit):[^\s,)\]]+")

#: Every retro-generated gotcha gets a mandatory review-by/expiry date --
#: never ``expires: null``. 90 days keeps auto-drafted rules from silently
#: living forever without a human re-confirming they still apply. Manually-
#: filed gotchas go through human review at filing time and may still set
#: ``expires: null`` for a deliberately permanent rule; that path does not
#: call :func:`default_expiry`.
DEFAULT_GOTCHA_EXPIRY_DAYS = 90


def existing_source_prs(gotchas: list[dict[str, Any]]) -> set[int]:
    prs: set[int] = set()
    for entry in gotchas:
        for pr in entry.get("source_prs", []) or []:
            if isinstance(pr, int):
                prs.add(pr)
    return prs


def existing_source_refs(gotchas: list[dict[str, Any]]) -> set[str]:
    refs: set[str] = set()
    for entry in gotchas:
        for ref in entry.get("source_refs", []) or []:
            if isinstance(ref, str) and ref:
                refs.add(ref)
    return refs


def open_issue_pr_pairs(issues: list[dict[str, Any]]) -> set[tuple[int, int]]:
    """Parse open gotcha issues for evidence PR pairs.

    Legacy extractor issues encoded the pair in the title. Weekly severity
    issues encode source PRs in the body, so treat any two referenced source
    PRs as a duplicate-suppression pair.
    """
    pairs: set[tuple[int, int]] = set()
    for issue in issues:
        title = str(issue.get("title", ""))
        match = _ISSUE_TITLE_PR_PAIR_RE.search(title)
        if match:
            pairs.add((int(match.group(1)), int(match.group(2))))
            continue
        source_prs = sorted(
            {int(pr) for pr in _ISSUE_PR_REF_RE.findall(str(issue.get("body", "")))}
        )
        pairs.update(combinations(source_prs, 2))
    return pairs


def open_issue_source_refs(issues: list[dict[str, Any]]) -> set[str]:
    refs: set[str] = set()
    for issue in issues:
        refs.update(_ISSUE_SOURCE_REF_RE.findall(str(issue.get("body", ""))))
    return refs


def is_duplicate_group(
    group: EvidenceGroup,
    *,
    existing_prs: set[int],
    open_pairs: set[tuple[int, int]],
) -> bool:
    """A group is a duplicate if any of its PRs are already a gotcha's
    source_prs, or any of its exact fix-up pairs already has an open review
    issue."""
    if any(pr in existing_prs for pr in group.pr_numbers):
        return True
    return bool(group.pair_number_tuples & open_pairs)


def without_known_bridge_refs(group: EvidenceGroup, known_refs: set[str]) -> EvidenceGroup:
    """Remove consumed bridge rows while preserving newer incidents in the group."""
    return replace(
        group,
        bridge_evidence=tuple(
            record for record in group.bridge_evidence if record.source_ref not in known_refs
        ),
    )


def allocate_next_ids(existing_gotchas: list[dict[str, Any]], year: str, count: int) -> list[str]:
    """Return ``count`` sequential unused G-<year>-NNN ids, highest-used + 1."""
    max_seq = 0
    for entry in existing_gotchas:
        match = _ID_RE.match(str(entry.get("id", "")))
        if match and match.group(1) == year:
            max_seq = max(max_seq, int(match.group(2)))
    return [f"G-{year}-{seq:03d}" for seq in range(max_seq + 1, max_seq + 1 + count)]


def default_expiry(created: str, *, days: int = DEFAULT_GOTCHA_EXPIRY_DAYS) -> str:
    """Compute the mandatory review-by/expiry date for a retro-generated
    gotcha, ``days`` after its ``created`` date (both ISO 8601)."""
    return (date.fromisoformat(created) + timedelta(days=days)).isoformat()


def build_gotcha_entry(candidate: GotchaCandidate, gotcha_id: str) -> dict[str, Any]:
    return {
        "id": gotcha_id,
        "rule": candidate.rule,
        "workflow_kinds": candidate.workflow_kinds,
        "repo_paths": candidate.repo_paths,
        "surface_ids": candidate.surface_ids,
        "source_prs": candidate.source_prs,
        "source_refs": candidate.source_refs,
        "control_type": candidate.control_type,
        "control_pr": None,
        "control_path": candidate.control_path,
        "inject_into_briefer": candidate.inject_into_briefer,
        "created": candidate.created,
        # Never null -- every retro-generated candidate gets a mandatory
        # review-by date so it cannot silently persist unreviewed forever.
        "expires": default_expiry(candidate.created),
    }


def assign_sequential_entries(
    candidates: list[GotchaCandidate],
    existing_gotchas: list[dict[str, Any]],
    year: str,
) -> list[dict[str, Any]]:
    """Turn accepted candidates into gotcha entries with freshly allocated ids."""
    ids = allocate_next_ids(existing_gotchas, year, len(candidates))
    return [
        build_gotcha_entry(candidate, gotcha_id)
        for candidate, gotcha_id in zip(candidates, ids, strict=True)
    ]


def load_gotchas_file(path: Path) -> dict[str, Any]:
    """Load the registry payload; a missing file is an empty registry."""
    if not path.exists():
        return {"gotchas": []}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("gotchas"), list):
        raise ValueError(f"{path} is not a valid gotchas.json payload")
    return payload


def validate_gotchas_payload(payload: dict[str, Any]) -> list[str]:
    """Return a list of validation error messages (empty means valid).

    Checks both JSON Schema conformance (against the schema shipped in
    :mod:`corral.memory`) and id uniqueness, so a bad write is caught before
    it ever hits disk, not just on the next CI run.
    """
    errors = memory_registry.validate_payload(payload, memory_registry.GOTCHAS_SCHEMA_NAME)

    ids = [entry.get("id") for entry in payload.get("gotchas", [])]
    duplicates = sorted({gid for gid in ids if ids.count(gid) > 1})
    if duplicates:
        errors.append(f"duplicate gotcha ids: {duplicates}")
    return errors


def write_gotchas_file(path: Path, payload: dict[str, Any]) -> None:
    """Validate then write; raises ValueError rather than writing an invalid file."""
    errors = validate_gotchas_payload(payload)
    if errors:
        raise ValueError("Refusing to write invalid gotchas.json:\n" + "\n".join(errors))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def build_severity_issue_title(
    candidate: GotchaCandidate, gotcha_id: str, *, label: str
) -> str:
    if len(candidate.source_prs) >= 2:
        prs = sorted(candidate.source_prs)
        evidence = f" from PR #{prs[0]} -> #{prs[1]}"
    else:
        evidence = ""
    return f"[{label}] {candidate.severity} candidate {gotcha_id}{evidence}: {candidate.rule[:80]}"


def build_severity_issue_body(candidate: GotchaCandidate, gotcha_id: str) -> str:
    prs = ", ".join(f"#{pr}" for pr in candidate.source_prs)
    refs = ", ".join(candidate.source_refs)
    return (
        f"Severe ({candidate.severity}) candidate gotcha `{gotcha_id}` was drafted by "
        "the weekly agent retrospective and is included in this week's gotcha-"
        "registry PR. Filed immediately (event-driven escalation) so it does not "
        "wait for the weekly PR review cycle.\n\n"
        f"## Rule\n{candidate.rule}\n\n"
        f"## Evidence PRs\n{prs or '(none)'}\n\n"
        f"## Evidence source refs\n{refs or '(none)'}\n\n"
        f"## Rationale\n{candidate.rationale}\n\n"
        f"## Confidence\n{candidate.confidence:.2f}\n"
    )


__all__ = [
    "DEFAULT_GOTCHA_EXPIRY_DAYS",
    "allocate_next_ids",
    "assign_sequential_entries",
    "build_gotcha_entry",
    "build_severity_issue_body",
    "build_severity_issue_title",
    "default_expiry",
    "existing_source_prs",
    "existing_source_refs",
    "is_duplicate_group",
    "load_gotchas_file",
    "open_issue_pr_pairs",
    "open_issue_source_refs",
    "validate_gotchas_payload",
    "without_known_bridge_refs",
    "write_gotchas_file",
]
