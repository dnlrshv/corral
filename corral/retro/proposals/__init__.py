"""Instruction-file (doc/skill) proposal pass for the weekly retrospective.

Verified proposals render into the weekly summary as a SEPARATE
human-review-only section/commit plan; ``corral retro run`` never auto-applies
them. Activation is opt-in via ``retro.proposals.enabled: true``.
"""

from .models import (
    DocProposal,
    DocProposalRun,
    PlannedEdit,
    ProposalRejectedError,
    check_skill_eligibility,
    tier_rank,
)
from .plan import draft_and_verify_proposals, render_proposal_block

__all__ = [
    "DocProposal",
    "DocProposalRun",
    "PlannedEdit",
    "ProposalRejectedError",
    "check_skill_eligibility",
    "draft_and_verify_proposals",
    "render_proposal_block",
    "tier_rank",
]
