"""Retry orchestration for preflight LLM briefs."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import yaml

from corral.preflight.auth import PreflightLLMResponse

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


# The prompt intentionally never names the brief schema: an earlier wording
# parenthetically mentioned a schema name, and models often read that aside as
# an instruction to nest every field under a wrapper key bearing that name.
# That is valid YAML but leaves every field missing once validation looks for
# top-level keys. Observed failures all had stop_reason=end_turn, never
# max_tokens, so this was prompt ambiguity rather than truncation. The prompt
# therefore forbids an enclosing key without naming one.
PROMPT_TEMPLATE = """\
You are a senior engineer preflight assistant. Given a GitHub issue and a code-map \
summary, produce a terse preflight brief.

## Issue
{issue_text}

## Code Map (surfaces.yaml excerpt)
{code_map_yaml}

Output ONLY valid YAML (no markdown code fences, no prose, no surrounding \
commentary). The document's top-level mapping must contain exactly these keys \
and no others -- do not add any enclosing/wrapper key around them:
files_to_touch:       # list of <=10 files that will need to be modified
files_to_read_only:   # list of <=10 files to read for context only
surfaces_in_scope:    # list of <=5 surface IDs from the code map that are relevant
cross_cutting_concerns: # list of <=3 project-wide concerns to keep in mind
recent_related_prs:   # list of <=5 PR or issue references (e.g. "#1023")
invariants_to_preserve: # list of <=5 invariants that must not be broken
test_files:           # list of <=5 test files to create or update
estimated_blast_radius: # one of: low / medium / high
do_not_touch: []      # leave empty -- populated by the script from surfaces.yaml \
needs_human:true entries (do not infer)
"""

RETRY_TEMPLATE = """\
Your previous reply failed validation with this error:
{error}

Re-emit ONE corrected YAML document for the same brief. Requirements, repeated \
because the previous reply violated at least one of them:
- The document's top-level mapping must contain exactly these keys and no \
others -- files_to_touch, files_to_read_only, surfaces_in_scope, \
cross_cutting_concerns, recent_related_prs, invariants_to_preserve, test_files, \
estimated_blast_radius. Do not add any enclosing/wrapper key around them.
- No markdown code fences, no prose before or after the YAML.
"""


class BriefResponseError(ValueError):
    """Raised when the model's response still fails to parse/validate after the retry.

    Distinct from the plain ``ValueError``/``yaml.YAMLError`` raised by
    parsing/validation so the caller can attribute the resulting fallback to
    ``fallback_reason: llm_response_invalid`` instead of a generic cause.
    """


def issue_text(issue: dict[str, Any]) -> str:
    return f"Title: {issue.get('title', '')}\n\n{issue.get('body', '')}"


def build_prompt(issue: dict[str, Any], code_map_yaml: str) -> str:
    return PROMPT_TEMPLATE.format(issue_text=issue_text(issue), code_map_yaml=code_map_yaml)


def validate_brief(brief: dict[str, Any]) -> None:
    missing = [f for f in BRIEF_FIELDS if f not in brief]
    if missing:
        raise ValueError(f"Missing required fields: {missing}")
    valid_radii = {"low", "medium", "high"}
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


def _parse_and_validate(
    raw: str,
    code_map_yaml: str,
    *,
    parse_brief: Callable[[str], dict[str, Any]],
    extract_needs_human_paths: Callable[[str], list[str]],
    validate_brief: Callable[[dict[str, Any]], None],
) -> dict[str, Any]:
    brief = parse_brief(raw)
    brief["do_not_touch"] = extract_needs_human_paths(code_map_yaml)
    validate_brief(brief)
    return brief


def generate_brief_with_retry(
    *,
    issue: dict[str, Any],
    code_map_yaml: str,
    max_tokens: int,
    call_llm_with_meta: Callable[..., PreflightLLMResponse],
    parse_brief: Callable[[str], dict[str, Any]],
    extract_needs_human_paths: Callable[[str], list[str]],
) -> tuple[dict[str, Any], str | None]:
    """Generate an LLM brief, with one retry that feeds validation errors back.

    The main prompt avoids naming any wrapper key; this bounded retry gives
    one extra chance for residual drift, replaying the model's own bad reply
    as conversation history so the correction happens in context.
    """
    prompt = build_prompt(issue, code_map_yaml)
    response = call_llm_with_meta(prompt, max_tokens)
    stop_reason = response.stop_reason
    try:
        brief = _parse_and_validate(
            response.text,
            code_map_yaml,
            parse_brief=parse_brief,
            extract_needs_human_paths=extract_needs_human_paths,
            validate_brief=validate_brief,
        )
    except (ValueError, yaml.YAMLError) as first_exc:
        retry_response = call_llm_with_meta(
            RETRY_TEMPLATE.format(error=first_exc),
            max_tokens,
            history=[
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": response.text},
            ],
        )
        stop_reason = retry_response.stop_reason
        try:
            brief = _parse_and_validate(
                retry_response.text,
                code_map_yaml,
                parse_brief=parse_brief,
                extract_needs_human_paths=extract_needs_human_paths,
                validate_brief=validate_brief,
            )
        except (ValueError, yaml.YAMLError) as second_exc:
            raise BriefResponseError(str(second_exc)) from second_exc
    return brief, stop_reason
