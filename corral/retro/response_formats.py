"""Optional provider schemas rendered into the existing retrospective text contracts."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from corral.retro.mining import ALLOWED_CONTROL_TYPES


@dataclass(frozen=True)
class ResponseFormat:
    name: str
    schema: Mapping[str, Any]
    render: Callable[[Mapping[str, Any]], str]
    prompt_note: str = ""


def _object(properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


def _enum(values: Sequence[str]) -> dict[str, Any]:
    return {"type": "string", "enum": list(values)} if values else {"type": "string"}


def _strings() -> dict[str, Any]:
    return {"type": "array", "items": {"type": "string"}}


def _nullable_string() -> dict[str, Any]:
    return {"anyOf": [{"type": "string"}, {"type": "null"}]}


def _render_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True)


def gotcha_candidate_format(allowed_severities: Sequence[str]) -> ResponseFormat:
    return ResponseFormat("gotcha-candidate", _object({
        "rule": {"type": "string"},
        "workflow_kinds": _strings(),
        "repo_paths": _strings(),
        "surface_ids": _strings(),
        "control_type": _enum(sorted(ALLOWED_CONTROL_TYPES)),
        "control_path": _nullable_string(),
        "inject_into_briefer": {"type": "boolean"},
        "confidence": {"type": "number"},
        "rationale": {"type": "string"},
        "severity": _enum(allowed_severities),
    }), _render_json)


def doc_proposal_format(modalities: Sequence[str], tiers: Sequence[str]) -> ResponseFormat:
    return ResponseFormat("doc-proposal", _object({
        "should_propose": {"type": "boolean"},
        "operation": _enum(["add_rule", "sharpen", "add_skill"]),
        "target_tier": _enum(tiers),
        "target_file": {"type": "string"},
        "concern_key": {"type": "string"},
        "statement": {"type": "string"},
        "anchor": {"type": "string"},
        "rationale": {"type": "string"},
        "modality": _enum(modalities),
        "selectors": _object({"paths": _strings(), "workflows": _strings(), "surfaces": _strings()}),
        "sharpen_rule_id": _nullable_string(),
        "why_sharpen_is_insufficient": _nullable_string(),
        "skill_slug": _nullable_string(),
        "skill_body": _nullable_string(),
        "supersedes": _strings(),
        "existing_rules_considered": _strings(),
        "confidence": {"type": "number"},
    }), _render_json)


def _render_verdict(payload: Mapping[str, Any]) -> str:
    reasoning = " ".join(str(payload["reasoning"]).split())
    sharpened = payload.get("sharpened")
    sharpened_text = " ".join(str(sharpened).split()) if sharpened else "NONE"
    return f"VERDICT: {payload['verdict']}\nREASONING: {reasoning}\nSHARPENED: {sharpened_text}"


GOTCHA_VERDICT_FORMAT = ResponseFormat(
    "gotcha-verdict",
    _object({"verdict": _enum(["CONFIRM", "REFUTE"]),
             "reasoning": {"type": "string"}, "sharpened": _nullable_string()}),
    _render_verdict,
    "Your reply is returned through a JSON schema: put the verdict, the reasoning "
    "and the sharpened wording (null for NONE) in the matching JSON properties "
    "instead of labeled lines.",
)
DOC_VERDICT_FORMAT = ResponseFormat(
    "doc-verdict",
    _object({"verdict": _enum(["confirm", "refute"]),
             "reason": {"type": "string"}, "confidence": {"type": "number"}}),
    _render_json,
)
