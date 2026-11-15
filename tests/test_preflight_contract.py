"""The model brief contract uses a portable JSON-schema subset."""
import importlib.util
import inspect
import json

import pytest

from corral.preflight import contract, parser
from corral.preflight.contract import BRIEF_SCHEMA, LIST_FIELD_CAPS, BriefResponseError, build_prompt, normalize_model_brief


def test_brief_schema_matches_contract():
    fields = ["files_to_touch", "files_to_read_only", "surfaces_in_scope", "cross_cutting_concerns", "recent_related_prs", "invariants_to_preserve", "test_files"]
    assert BRIEF_SCHEMA == {
        "type": "object",
        "properties": {**{field: {"type": "array", "items": {"type": "string"}} for field in fields},
                       "estimated_blast_radius": {"type": "string", "enum": ["low", "medium", "high"]}},
        "required": [*fields, "estimated_blast_radius"],
        "additionalProperties": False,
    }


def test_brief_schema_uses_only_supported_keywords():
    forbidden = {"maxItems", "minItems", "minLength", "maxLength", "minimum", "maximum", "multipleOf", "pattern"}
    def visit(value):
        if isinstance(value, dict):
            assert not forbidden.intersection(value)
            if value.get("type") == "object":
                assert value["additionalProperties"] is False
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
    visit(BRIEF_SCHEMA)


def test_prompt_caps_come_from_list_field_caps():
    prompt = build_prompt({}, "")
    for field, cap in LIST_FIELD_CAPS.items():
        assert f"{field}: up to {cap} " in prompt
    assert "surfaces_in_scope: up to 8 " in prompt


def test_prompt_has_no_format_forcing_text():
    prompt = build_prompt({}, "")
    for text in ["ONLY", "YAML", "code fence", "```", "wrapper", "do_not_touch"]:
        assert text not in prompt


def test_normalize_truncates_over_cap_lists():
    for field, cap in LIST_FIELD_CAPS.items():
        items = [f"item-{i}" for i in range(cap + 1)]
        brief = normalize_model_brief({field: items, "estimated_blast_radius": "low"})
        assert brief[field] == items[:cap]
    assert brief["files_to_touch"] == []


def test_normalize_drops_non_string_items():
    brief = normalize_model_brief({"files_to_touch": [None, 5, "a", {}, "b"], "estimated_blast_radius": "low"})
    assert brief["files_to_touch"] == ["a", "b"]


def test_normalize_drops_unknown_keys():
    brief = normalize_model_brief({"estimated_blast_radius": "high", "preflight_status": "general", "agent_gotchas": ["invented"], "do_not_touch": ["invented"]})
    assert set(brief) == set(contract.MODEL_BRIEF_FIELDS)


@pytest.mark.parametrize("raw", [[], None, 1, {"estimated_blast_radius": "huge"}, {"estimated_blast_radius": []}, {"estimated_blast_radius": "low", "files_to_touch": "a.py"}])
def test_normalize_rejects_bad_radius_and_non_object(raw):
    with pytest.raises(BriefResponseError):
        normalize_model_brief(raw)


def test_yaml_salvage_helpers_are_removed():
    assert importlib.util.find_spec("corral.preflight." + "retry") is None
    for name in ["parse_" + "brief", "FENCE_RE", "_prose_" + "yaml_blocks"]:
        assert not hasattr(parser, name)


def test_installed_sdk_accepts_output_config():
    anthropic = pytest.importorskip("anthropic")
    assert "output_config" in inspect.signature(anthropic.resources.messages.Messages.create).parameters


@pytest.mark.parametrize("text", ["", "not JSON", "```json\n{}\n```"])
def test_parse_json_rejects_invalid_output(text):
    with pytest.raises(BriefResponseError, match="response is not valid JSON"):
        parser.parse_brief_json(text)


def test_parse_json_preserves_object():
    payload = {"estimated_blast_radius": "low"}
    assert parser.parse_brief_json(json.dumps(payload)) == payload
