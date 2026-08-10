"""Drafter-payload normalization and deterministic governance preconditions.

Every check here either IS governance policy supplied by the C3 modules
(``corral.governance.config``/``proposals``/``registry``) or a structural
precondition the gate would enforce anyway; the retro refuses to generate an
edit its judge would reject.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any

from corral.governance.config import GovernanceConfig
from corral.governance.proposals import path_matches
from corral.governance.registry import selectors_overlap
from corral.retro.types import EvidenceGroup

from .models import ANCHOR_MIN_CHARS, DocProposal, ProposalRejectedError

#: Operations this drafter can express. The effective set is intersected with
#: ``governance.proposals.operations``.
DRAFTABLE_OPERATIONS: frozenset[str] = frozenset({"add_rule", "sharpen", "add_skill"})

#: Tiers this drafter can actually WRITE a diff for. ``executable`` (a
#: hook/lint) and ``gotcha`` (owned by the gotcha pass) are intentionally
#: excluded from doc-proposal drafting. The effective set is intersected with
#: ``governance.proposals.tiers``.
WRITABLE_TIERS: frozenset[str] = frozenset(
    {"core", "workflow_prompt", "topic_file", "skill"}
)

_RETRO_RULE_ID_RE = re.compile(r"^R-RETRO-(\d{4})$")


def _coerce_selectors(value: Any) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, list[str]] = {}
    for kind in ("paths", "workflows", "surfaces"):
        raw = value.get(kind)
        if isinstance(raw, list):
            cleaned = [str(x).strip() for x in raw if str(x).strip()]
            if cleaned:
                out[kind] = cleaned
    return out


def _coerce_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(x).strip() for x in value if str(x).strip()]


def _confidence(value: Any) -> float:
    if not isinstance(value, int | float) or isinstance(value, bool):
        return 0.0
    return max(0.0, min(1.0, float(value)))


def normalize_proposal(
    payload: Mapping[str, Any],
    group: EvidenceGroup,
    *,
    base_registry: Mapping[str, Mapping[str, Any]],
    allocate_rule_id: Callable[[], str],
    governance: GovernanceConfig,
    target_globs: list[str],
    reviewer: str | None,
    min_incidents: int,
) -> DocProposal | None:
    """Turn a drafter JSON payload into a validated ``DocProposal``.

    Returns ``None`` when the drafter declined. Raises
    ``ProposalRejectedError`` when the payload is structurally invalid or
    violates a governance precondition (unsupported op/tier, bad anchor,
    unknown sharpen target, protected or out-of-ladder target).
    """
    if not payload.get("should_propose", False):
        return None
    operations = DRAFTABLE_OPERATIONS & set(governance.proposals.operations)
    writable_tiers = WRITABLE_TIERS & set(governance.proposals.tiers)

    operation = str(payload.get("operation", "")).strip()
    if operation not in operations:
        raise ProposalRejectedError(f"unsupported operation {operation!r}")
    tier = str(payload.get("target_tier", "")).strip()
    if tier not in writable_tiers:
        raise ProposalRejectedError(f"untargetable tier {tier!r}")
    modality = str(payload.get("modality", "")).strip().upper()
    if modality not in set(governance.modalities):
        raise ProposalRejectedError(f"invalid modality {modality!r}")
    concern_key = str(payload.get("concern_key", "")).strip()
    if not concern_key:
        raise ProposalRejectedError("missing concern_key")

    statement = str(payload.get("statement", "")).strip()
    anchor = str(payload.get("anchor", "")).strip()
    if operation != "add_skill":
        if len(anchor) < ANCHOR_MIN_CHARS:
            raise ProposalRejectedError(f"anchor too short (<{ANCHOR_MIN_CHARS} chars)")
        if anchor not in statement:
            raise ProposalRejectedError("anchor is not a verbatim substring of statement")

    incidents = group.root_incident_labels
    if len(incidents) < min_incidents:
        raise ProposalRejectedError(
            f"only {len(incidents)} distinct root incident(s); need >={min_incidents}"
        )

    target_file = str(payload.get("target_file", "")).strip()
    selectors = _coerce_selectors(payload.get("selectors"))
    supersedes = _coerce_str_list(payload.get("supersedes"))
    old_anchor: str | None = None
    skill_slug: str | None = None
    skill_body: str | None = None

    if operation == "sharpen":
        rule_id = str(payload.get("sharpen_rule_id", "")).strip()
        base_rule = base_registry.get(rule_id)
        if not base_rule:
            raise ProposalRejectedError(f"sharpen target {rule_id!r} not in the registry")
        # The file + old anchor are taken from the registry (authoritative),
        # not the drafter -- the edit must land on the real rule text.
        target_file = str(base_rule.get("file", "")).strip()
        old_anchor = str(base_rule.get("anchor", "")).strip()
        if not old_anchor:
            raise ProposalRejectedError(f"sharpen target {rule_id!r} has no anchor")
        if anchor == old_anchor:
            raise ProposalRejectedError(
                "sharpen must change the anchor (a no-op sharpen would not register "
                "as a normative change for the gate)"
            )
        concern_key = str(base_rule.get("concern_key") or concern_key)
    elif operation == "add_skill":
        skill_slug = str(payload.get("skill_slug", "")).strip()
        skill_body = str(payload.get("skill_body", "")).strip()
        if not skill_slug or not re.fullmatch(r"[a-z][a-z0-9-]*", skill_slug):
            raise ProposalRejectedError(f"invalid skill slug {skill_slug!r}")
        if len(anchor) < ANCHOR_MIN_CHARS or anchor not in (skill_body or ""):
            raise ProposalRejectedError(
                "skill anchor must be a >=8-char substring of the body"
            )
        if not target_file:
            raise ProposalRejectedError("add_skill requires target_file")
        rule_id = allocate_rule_id()
    else:  # add_rule
        if not target_file:
            raise ProposalRejectedError("add_rule requires target_file")
        rule_id = allocate_rule_id()

    # Sharpen targets come from the registry rather than the drafter, but they
    # are still prospective instruction-file edits and must obey the exact same
    # adopter-owned path boundary as additions.
    _reject_untargetable(target_file, governance.protected_paths, target_globs)

    control_type = str(payload.get("control_type", tier)).strip() or tier
    if control_type not in set(governance.proposals.tiers):
        control_type = tier

    review_by = (reviewer or "").strip()
    if not review_by:
        raise ProposalRejectedError(
            "no reviewer available: set governance.reviewer before enabling proposals"
        )
    drafted_reviewer = str(payload.get("review_by") or "").strip()
    if drafted_reviewer and drafted_reviewer != review_by:
        raise ProposalRejectedError(
            f"drafted review_by {drafted_reviewer!r} does not match configured "
            f"governance.reviewer {review_by!r}"
        )

    return DocProposal(
        operation=operation,
        rule_id=rule_id,
        concern_key=concern_key,
        target_tier=tier,
        target_file=target_file,
        modality=modality,
        anchor=anchor,
        statement=statement,
        selectors=selectors,
        review_by=review_by,
        supersedes=supersedes,
        existing_rules_considered=_coerce_str_list(payload.get("existing_rules_considered")),
        why_sharpen_is_insufficient=(
            str(payload.get("why_sharpen_is_insufficient", "")).strip() or None
        ),
        control_type=control_type,
        control_path=target_file,
        evidence_incidents=incidents,
        replay_cases=_coerce_str_list(payload.get("replay_cases")),
        rationale=str(payload.get("rationale", "")).strip(),
        confidence=_confidence(payload.get("confidence")),
        evidence_key=group.key,
        old_anchor=old_anchor,
        skill_slug=skill_slug,
        skill_body=skill_body,
    )


def _reject_untargetable(
    path: str, protected_paths: list[str], target_globs: list[str]
) -> None:
    """Belt-and-braces path guard: refuse protected paths and anything off the
    adopter-configured target ladder BEFORE generating an edit for it."""
    if not path:
        raise ProposalRejectedError("missing target_file")
    if any(path_matches(path, pattern) for pattern in protected_paths):
        raise ProposalRejectedError(f"refusing to target a protected governance path: {path!r}")
    if not target_globs:
        raise ProposalRejectedError(
            "retro.proposals.target_globs is empty; declare the instruction-file "
            "targets this pass may edit"
        )
    if not any(path_matches(path, pattern) for pattern in target_globs):
        raise ProposalRejectedError(f"target is not an editable instruction file: {path!r}")


def sharpen_first_violation(
    proposal: DocProposal, base_registry: Mapping[str, Mapping[str, Any]]
) -> str | None:
    """For an ``add_rule``, return a drop reason if a base rule shares the
    ``concern_key`` AND an overlapping selector and is not superseded -- else
    ``None``.

    Overlap is decided by the gate's own ``selectors_overlap`` (C3 reuse), so
    the retro never emits an add the gate would reject as a near-duplicate.
    """
    if proposal.operation != "add_rule":
        return None
    for other_id, other in base_registry.items():
        if other.get("concern_key") != proposal.concern_key:
            continue
        if not selectors_overlap(proposal.selectors, other.get("selectors", {}) or {}):
            continue
        if other_id in proposal.supersedes:
            continue
        return (
            f"sharpen-first: concern_key {proposal.concern_key!r} already covered by "
            f"{other_id} with an overlapping selector; sharpen it or declare supersedes"
        )
    return None


def make_rule_id_allocator(base_registry: Mapping[str, Any]) -> Callable[[], str]:
    """Return a stateful allocator handing out fresh ``R-RETRO-NNNN`` ids."""
    seq = 0
    for rid in base_registry:
        m = _RETRO_RULE_ID_RE.match(str(rid))
        if m:
            seq = max(seq, int(m.group(1)))
    counter = {"n": seq}

    def allocate() -> str:
        counter["n"] += 1
        return f"R-RETRO-{counter['n']:04d}"

    return allocate


__all__ = [
    "DRAFTABLE_OPERATIONS",
    "WRITABLE_TIERS",
    "make_rule_id_allocator",
    "normalize_proposal",
    "sharpen_first_violation",
]
