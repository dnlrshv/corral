"""Data model for retrospective instruction-file (doc/skill) proposals.

Beyond drafting gotcha candidates (the gotcha pass, see ``corral.retro.mining``),
the weekly retrospective can PROPOSE concrete edits to agent instruction files:
a convention agents keep violating, a stale claim, a recurring workflow gap.
Proposals are drafted on the configured drafter seat, independently verified,
gate-validated against ``corral.governance`` modules, and rendered into the
weekly summary as a SEPARATE human-review-only section. ``corral retro run``
never auto-applies them.

Binding constraints implemented across this package:

* **Machine-readable proposal contract.** Accepted proposals render into the
  fenced ``yaml`` block that the governance gate's own ``parse_proposal_block``
  + ``validate_proposal_contract`` accept; rendering runs those functions on
  its own output and raises on self-check failure (a caller bug, not a runtime
  input error).
* **>=2 distinct ROOT incidents per proposal** (configurable via
  ``retro.proposals.min_incidents``), reusing ``EvidenceGroup``'s
  ``root_incident_labels`` machinery (the same bar the gotcha pass uses).
* **Sharpen-first.** If a base-registry rule shares the ``concern_key`` and an
  overlapping selector (the gate's own ``selectors_overlap``), an ``add_rule``
  is dropped unless it declares ``supersedes``.
* **Placement ladder.** The tier order comes from ``governance.proposals.tiers``
  and the editable target files from ``retro.proposals.target_globs``; corral
  assumes no private instruction-file ladder.
* **Weekly cap.** At most ``retro.proposals.max`` (hard cap 3) doc/skill
  proposals ON TOP of the gotcha cap; highest-evidence groups first.
* **Skills are procedures**, gated on >=3 completed tasks over >=2 weeks with a
  stable trigger (``check_skill_eligibility``). A single weekly window cannot
  establish a >=2-week span, so skill proposals stay gated off until
  multi-week telemetry supplies the span -- intentional, not a bug.
* **Budget fit.** A proposal's edits are token-budget-checked by reusing the
  real ``corral.governance`` manifest evaluator over the PROSPECTIVE file text;
  an over-budget proposal is dropped. The retro NEVER self-grants budget debt.

Anti-Goodhart: zero proposals is a successful week. Every drop is recorded
with a reason; nothing is silently discarded and no bar is loosened to
manufacture output.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

#: Cap on doc/skill proposals per weekly run, ON TOP of the gotcha cap. Kept
#: small so the combined PR stays reviewable.
DEFAULT_MAX_DOC_PROPOSALS = 3

#: Minimum DISTINCT root incidents per proposal.
DEFAULT_MIN_ROOT_INCIDENTS = 2

#: Skills are procedures, not knowledge buckets: only propose one once it is a
#: repeatedly-exercised, stable workflow.
SKILL_MIN_COMPLETED_TASKS = 3
SKILL_MIN_DISTINCT_WEEKS = 2

#: Mirror of the gate registry's anchor floor (distinctive verbatim substring).
ANCHOR_MIN_CHARS = 8


@dataclass
class DocProposal:
    """One verified doc/skill proposal, ready to render into the summary."""

    operation: str  # add_rule | sharpen | add_skill
    rule_id: str  # R-RETRO-NNNN (add) or an existing id (sharpen)
    concern_key: str
    target_tier: str
    target_file: str
    modality: str
    anchor: str  # verbatim >=8-char substring of the inserted statement
    statement: str  # the rule sentence (contains anchor)
    selectors: dict[str, list[str]]
    review_by: str
    supersedes: list[str]
    existing_rules_considered: list[str]
    why_sharpen_is_insufficient: str | None
    control_type: str
    control_path: str | None
    evidence_incidents: list[str]  # DISTINCT "#NNN" root-incident refs
    replay_cases: list[str]
    rationale: str
    confidence: float
    evidence_key: str
    # sharpen-only
    old_anchor: str | None = None
    # skill-only
    skill_slug: str | None = None
    skill_body: str | None = None

    @property
    def rule_text(self) -> str:
        """The single structured, normative markdown line inserted into a prose file.

        Starts with ``-`` and carries a bold modality marker so the gate's
        normative-line detector recognises it and requires a registry entry
        (which this proposal supplies) -- and contains the verbatim anchor.
        """
        return f"- **{self.modality}** {self.statement}"


@dataclass(frozen=True)
class PlannedEdit:
    """A prospective full-file write for an accepted proposal.

    Rendered in the weekly summary as the human-review commit plan; corral
    never writes these files itself.
    """

    path: str
    new_text: str
    is_new_file: bool = False


@dataclass
class DocProposalRun:
    """Outcome of one doc/skill proposal pass (never auto-applied)."""

    accepted: list[DocProposal] = field(default_factory=list)
    planned_edits: list[PlannedEdit] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    proposal_block: str = ""
    new_registry_text: str = ""
    pass_failure: str | None = None


class ProposalRejectedError(Exception):
    """A drafted proposal failed a structural/governance precondition; the
    caller records the reason and moves on (never aborts the run)."""


def tier_rank(tier: str, tiers: Sequence[str]) -> int:
    """Ladder index (lower = stronger control) within the configured tiers.

    The ladder is ``governance.proposals.tiers`` -- corral carries no private
    tier order. Unknown tiers sort last.
    """
    ladder = list(tiers)
    try:
        return ladder.index(tier)
    except ValueError:
        return len(ladder)


def check_skill_eligibility(
    *, completed_tasks: int, distinct_weeks: int, stable_trigger: str
) -> tuple[bool, str]:
    """A skill proposal is eligible only as a repeatedly-exercised procedure.

    Returns ``(ok, reason)``. Enforces >=3 completed tasks over >=2 distinct
    weeks with a non-empty stable trigger.
    """
    if not stable_trigger.strip():
        return False, "skill needs a stable, non-empty trigger"
    if completed_tasks < SKILL_MIN_COMPLETED_TASKS:
        return False, (
            f"skill needs >={SKILL_MIN_COMPLETED_TASKS} completed tasks (saw {completed_tasks})"
        )
    if distinct_weeks < SKILL_MIN_DISTINCT_WEEKS:
        return False, (
            f"skill needs >={SKILL_MIN_DISTINCT_WEEKS} distinct weeks of use "
            f"(saw {distinct_weeks})"
        )
    return True, "eligible"


__all__ = [
    "ANCHOR_MIN_CHARS",
    "DEFAULT_MAX_DOC_PROPOSALS",
    "DEFAULT_MIN_ROOT_INCIDENTS",
    "SKILL_MIN_COMPLETED_TASKS",
    "SKILL_MIN_DISTINCT_WEEKS",
    "DocProposal",
    "DocProposalRun",
    "PlannedEdit",
    "ProposalRejectedError",
    "check_skill_eligibility",
    "tier_rank",
]
