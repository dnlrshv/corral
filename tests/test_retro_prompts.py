"""Render every fixed retro prompt with neutral evidence to pin its register."""
import pytest

from corral.retro import doc_verification, drafting, mining, verification
from corral.retro.proposals import draft, plan
from corral.retro.types import EvidenceGroup
from tests.test_retro_summary import candidate

GROUP = EvidenceGroup("agent::src", "agent", "src", ())
BUILDERS = [
    lambda: mining.build_prompt(GROUP, {}),
    lambda: drafting.build_retry_prompt("task", "response", "error"),
    lambda: draft.build_doc_proposal_prompt(GROUP, {}, candidate_rules=[], tiers=["core"]),
    lambda: draft.build_retry_prompt("task", "response", "error"),
    lambda: verification.build_verification_prompt(candidate("Check the boundary because it protects state."), {}),
    lambda: doc_verification.build_verification_prompt(subject="edit", evidence="evidence", contract="contract"),
    lambda: plan._verifier_contract(2),
]


@pytest.mark.parametrize("builder", BUILDERS)
def test_prompts_have_no_grader_or_pressure_vocabulary(builder):
    prompt = builder()
    for text in ["judged on", "scored", "ALWAYS or", "Return ONLY", "EXACTLY", "INDEPENDENT", "MISREADING", "LACKING", "SHARPENING", "REPEATED", "MUST be set", "checked for", "returned to you"]:
        assert text not in prompt


def test_prompts_render_configured_root_incident_floor():
    prompts = [
        mining.build_prompt(GROUP, {}, min_root_incidents=3),
        draft.build_doc_proposal_prompt(GROUP, {}, candidate_rules=[], tiers=["core"], min_incidents=3),
        plan._verifier_contract(3),
    ]
    assert all("3-distinct-root-incident" in p or "at least 3 distinct root incidents" in p for p in prompts)
    assert all("2-distinct" not in p and "at least 2 distinct" not in p for p in prompts)


@pytest.mark.parametrize("extract", [drafting.extract_candidate, drafting.extract_candidate_with_retry])
def test_candidate_entry_points_forward_floor(extract):
    prompts = []
    def complete(prompt):
        prompts.append(prompt)
        return '{"rule": "Check the boundary because it protects state.", "confidence": 0.9}'
    extract(GROUP, {}, complete, min_root_incidents=3)
    assert "3-distinct-root-incident" in prompts[0]
