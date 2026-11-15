"""Optional schemas preserve the provider-independent retro text contracts."""
from __future__ import annotations

import json

import pytest

from corral.retro import doc_verification, mining
from corral.retro.proposals.normalize import normalize_proposal
from corral.retro.response_formats import (
    DOC_VERDICT_FORMAT,
    GOTCHA_VERDICT_FORMAT,
    doc_proposal_format,
    gotcha_candidate_format,
)
from corral.retro.verification import parse_verdict
from tests.retro_support import candidate_json
from tests.test_retro_proposals import PROPOSAL_PAYLOAD, make_group, make_norm_args, make_repo


@pytest.mark.parametrize("verdict,sharpened", [("CONFIRM", "Check the boundary\nbecause it protects state."), ("REFUTE", None)])
def test_verdict_render_round_trips_through_parse_verdict(verdict, sharpened):
    text = GOTCHA_VERDICT_FORMAT.render({
        "verdict": verdict, "reasoning": "Repeated evidence\n supports this.\n", "sharpened": sharpened,
    })
    parsed = parse_verdict(text)
    assert parsed.verdict == verdict
    assert parsed.reasoning == "Repeated evidence supports this."
    assert parsed.sharpened_rule == ("Check the boundary because it protects state." if sharpened else None)
    assert text.splitlines()[0] == f"VERDICT: {verdict}"
    assert len(text.splitlines()) == 3


def test_json_formats_round_trip_through_normalizers(tmp_path):
    group = make_group()
    candidate_payload = json.loads(candidate_json(rule="Check the boundary because it protects state."))
    gotcha_format = gotcha_candidate_format(["info", "P1"])
    candidate = mining.normalize_candidate(json.loads(gotcha_format.render(candidate_payload)), group)
    assert candidate.rule == candidate_payload["rule"]
    assert candidate.severity == "info"
    assert candidate.control_path is None

    root = make_repo(tmp_path)
    payload = dict(PROPOSAL_PAYLOAD, sharpen_rule_id=None, skill_slug=None, skill_body=None)
    payload["selectors"] = {"paths": ["src/orders.py"], "workflows": [], "surfaces": []}
    proposal_format = doc_proposal_format(["MUST", "READ"], ["core", "skill"])
    proposal = normalize_proposal(json.loads(proposal_format.render(payload)), group, **make_norm_args(root))
    assert proposal is not None
    assert proposal.rule_text == "- " + payload["statement"]
    assert proposal.modality == "MUST"
    declined = normalize_proposal(json.loads(proposal_format.render({"should_propose": False})), group, **make_norm_args(root))
    assert declined is None

    verdict = doc_verification.parse_verification_response(DOC_VERDICT_FORMAT.render({
        "verdict": "confirm", "reason": "Two incidents support it.", "confidence": 0.9,
    }))
    assert verdict.confirmed and verdict.confidence == 0.9
    assert verdict.reason == "Two incidents support it."


def test_schemas_are_structured_output_compatible():
    formats = [gotcha_candidate_format(["info", "P1"]), doc_proposal_format(["REQUIRED", "FORBIDDEN"], ["core", "skill"]), GOTCHA_VERDICT_FORMAT, DOC_VERDICT_FORMAT]
    forbidden = {"maxItems", "minItems", "minLength", "maxLength", "minimum", "maximum", "multipleOf", "pattern"}
    def visit(value):
        if isinstance(value, dict):
            assert not forbidden.intersection(value)
            assert not isinstance(value.get("type"), list)
            if value.get("type") == "object":
                assert value["additionalProperties"] is False
                assert set(value["required"]) == set(value["properties"])
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
    for response_format in formats:
        visit(response_format.schema)
    gotcha, doc = formats[:2]
    assert gotcha.schema["properties"]["severity"]["enum"] == ["info", "P1"]
    assert gotcha.schema["properties"]["control_type"]["enum"] == sorted(mining.ALLOWED_CONTROL_TYPES)
    assert doc.schema["properties"]["modality"]["enum"] == ["REQUIRED", "FORBIDDEN"]
    assert doc.schema["properties"]["target_tier"]["enum"] == ["core", "skill"]
    assert set(doc.schema["properties"]["selectors"]["required"]) == {"paths", "workflows", "surfaces"}
    assert gotcha.schema["properties"]["control_path"] == {"anyOf": [{"type": "string"}, {"type": "null"}]}
    assert GOTCHA_VERDICT_FORMAT.schema["properties"]["sharpened"] == {"anyOf": [{"type": "string"}, {"type": "null"}]}


def test_empty_tier_ladder_uses_plain_string_schema():
    assert doc_proposal_format(["MUST"], []).schema["properties"]["target_tier"] == {"type": "string"}
