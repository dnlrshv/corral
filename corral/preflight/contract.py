"""Brief fields, caps, JSON schema and prompt for preflight generation."""

from __future__ import annotations

from typing import Any

BRIEF_FIELDS = (
    "files_to_touch",
    "files_to_read_only",
    "surfaces_in_scope",
    "cross_cutting_concerns",
    "recent_related_prs",
    "invariants_to_preserve",
    "test_files",
    "estimated_blast_radius",
    "do_not_touch",
)

LIST_FIELD_CAPS: dict[str, int] = {
    "files_to_touch": 10,
    "files_to_read_only": 10,
    "surfaces_in_scope": 8,
    "cross_cutting_concerns": 3,
    "recent_related_prs": 5,
    "invariants_to_preserve": 5,
    "test_files": 5,
}


MODEL_BRIEF_FIELDS = tuple(f for f in BRIEF_FIELDS if f != "do_not_touch")
BLAST_RADII = ("low", "medium", "high")
FIELD_GUIDANCE = {
    "files_to_touch": "repository-relative paths of existing files the task will modify",
    "files_to_read_only": "existing files to read for context without modifying them",
    "surfaces_in_scope": "surface IDs from the registry above that the task touches",
    "cross_cutting_concerns": "project-wide concerns to keep in mind, one sentence each",
    "recent_related_prs": 'PR or issue references the task text mentions, written as "#1023"',
    "invariants_to_preserve": "behaviours that must stay unchanged, one sentence each",
    "test_files": "test files to create or update",
    "estimated_blast_radius": "low, medium or high",
}


def _brief_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            field: ({"type": "array", "items": {"type": "string"}}
                    if field in LIST_FIELD_CAPS
                    else {"type": "string", "enum": list(BLAST_RADII)})
            for field in MODEL_BRIEF_FIELDS
        },
        "required": list(MODEL_BRIEF_FIELDS),
        "additionalProperties": False,
    }


BRIEF_SCHEMA = _brief_schema()
PROMPT_TEMPLATE = """\
You are writing a preflight brief for a coding agent that is about to work on the task below in
this repository. The agent reads the brief before it opens any file, so the brief should point it
at the files, surfaces, invariants and tests that matter for this task.

## Task
{issue_text}

## Surfaces registry (surfaces.yaml)
{code_map_yaml}

The response format is set by a JSON schema. Fill the fields as follows, most relevant entry first,
and leave a list empty when nothing fits:
""" + "\n".join(
    f"- {field}: up to {LIST_FIELD_CAPS[field]} {FIELD_GUIDANCE[field]}"
    if field in LIST_FIELD_CAPS else f"- {field}: {FIELD_GUIDANCE[field]}"
    for field in MODEL_BRIEF_FIELDS
) + """

Paths that do not exist in the repository are removed after generation, and when most proposed
paths are removed the whole brief is replaced by a deterministic fallback. List only paths the task
text or the registry gives you a reason to expect.
"""


class BriefResponseError(ValueError):
    """The model's response could not be turned into a brief."""


class BriefRefusalError(BriefResponseError):
    """The model or a safety classifier declined the request."""

    def __init__(self, category: str | None):
        super().__init__(f"model refused (category: {category or 'unknown'})")


class BriefTruncatedError(BriefResponseError):
    """Output ended at the token limit before the brief was complete."""


def issue_text(issue: dict[str, Any]) -> str:
    return f"Title: {issue.get('title', '')}\n\n{issue.get('body', '')}"


def build_prompt(issue: dict[str, Any], code_map_yaml: str) -> str:
    return PROMPT_TEMPLATE.format(issue_text=issue_text(issue), code_map_yaml=code_map_yaml)


def normalize_model_brief(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise BriefResponseError(f"expected a JSON object, got {type(raw).__name__}")
    radius = raw.get("estimated_blast_radius")
    if radius not in BLAST_RADII:
        raise BriefResponseError(f"estimated_blast_radius must be one of {BLAST_RADII}, got {radius!r}")
    brief: dict[str, Any] = {}
    for field in MODEL_BRIEF_FIELDS:
        if field in LIST_FIELD_CAPS:
            value = raw.get(field, [])
            if not isinstance(value, list):
                raise BriefResponseError(f"Field {field!r} must be a list, got {type(value).__name__}")
            brief[field] = [item for item in value if isinstance(item, str)][:LIST_FIELD_CAPS[field]]
        else:
            brief[field] = radius
    return brief


def validate_brief(brief: dict[str, Any]) -> None:
    missing = [f for f in BRIEF_FIELDS if f not in brief]
    if missing:
        raise ValueError(f"Missing required fields: {missing}")
    valid_radii = set(BLAST_RADII)
    radius = brief.get("estimated_blast_radius")
    if radius not in valid_radii:
        raise ValueError(f"estimated_blast_radius must be one of {valid_radii}, got {radius!r}")
    for field, cap in LIST_FIELD_CAPS.items():
        value = brief[field]
        if not isinstance(value, list):
            raise ValueError(f"Field {field!r} must be a list, got {type(value).__name__}")
        if len(value) > cap:
            raise ValueError(f"Field {field!r} exceeds cap of {cap} entries (got {len(value)})")
    do_not_touch = brief["do_not_touch"]
    if not isinstance(do_not_touch, list):
        raise ValueError(f"Field 'do_not_touch' must be a list, got {type(do_not_touch).__name__}")
