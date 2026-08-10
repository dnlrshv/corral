"""Prompting and validation-retry coordination for doc/skill proposals.

The drafter runs on the configured ``retro.drafter_seat`` through the C1 seat
runtime. Returned output that fails validation is fed back to the drafter for
exactly ONE correction retry (source semantics); transport/API failures are
raised by the seat completer and propagate immediately from either call.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Any, TypeVar

from corral.retro.bridge.readers import render_bridge_evidence
from corral.retro.json_util import extract_json_payload
from corral.retro.types import EvidenceGroup

logger = logging.getLogger(__name__)

T = TypeVar("T")


def build_doc_proposal_prompt(
    group: EvidenceGroup,
    excerpts: Mapping[int, str],
    *,
    candidate_rules: Sequence[Mapping[str, Any]],
    tiers: Sequence[str],
) -> str:
    """Prompt the drafter to propose one instruction-file diff, or decline.

    The placement ladder is rendered from the configured governance tiers; no
    private instruction-file hierarchy is assumed.
    """
    pair_blocks = [
        f"- Original PR #{pair.original_pr} ({pair.original_title or 'no title'}) "
        f"-> fix-up PR #{pair.fixup_pr} ({pair.fixup_title or 'no title'}), "
        f"shared files: {', '.join(pair.shared_files) or '(none)'}\n"
        f"  excerpt: {excerpts.get(pair.original_pr, '') or '(unavailable)'}"
        for pair in group.pairs
    ]
    existing = (
        "\n".join(
            f"- {rule.get('id')}: concern_key={rule.get('concern_key')} "
            f"in {rule.get('file')} (anchor: {rule.get('anchor')!r})"
            for rule in candidate_rules
        )
        or "(none obviously related)"
    )
    ladder = " > ".join(tiers) if tiers else "(no tiers configured)"
    return (
        "You maintain agent instruction files for this repository. From the "
        "repeated fix-up evidence below (already past a >=2-distinct-root-incident "
        "bar), decide whether agents keep MISREADING or LACKING a written rule, and "
        "if so propose ONE concrete instruction-file diff. Prefer SHARPENING an "
        "existing rule (below) over adding a near-duplicate. Respect the placement "
        f"ladder (strongest control first): {ladder}. A lower tier is only chosen "
        "when a higher one is not applicable. This job is not scored by output "
        "volume; if no clear written-guidance gap exists, decline "
        "(should_propose=false).\n\n"
        f"Agent: `{group.agent}` | Area: `{group.area}`\n\n"
        "## Fix-up evidence\n" + ("\n".join(pair_blocks) or "(none)") + "\n\n"
        "## Sanitized file-backed bridge evidence\n"
        + render_bridge_evidence(group.bridge_evidence)
        + "\n\n"
        f"## Existing registry rules to consider sharpening\n{existing}\n\n"
        "Return ONLY a JSON object:\n"
        "- should_propose: boolean\n"
        "- operation: one of add_rule, sharpen, add_skill\n"
        "- target_tier: the tier this edit belongs to (from the ladder above)\n"
        "- target_file: repo-relative instruction file to edit (for sharpen, the "
        "file the existing rule lives in; for add_skill, the new skill file)\n"
        "- concern_key: kebab-case slug for the concern this rule governs\n"
        "- modality: one of MUST, MUST NOT, ASK, READ\n"
        "- statement: the rule sentence (imperative, concrete); MUST contain the "
        "anchor verbatim\n"
        "- anchor: a distinctive >=8-char verbatim substring of statement\n"
        "- selectors: object with optional paths/workflows/surfaces arrays\n"
        "- sharpen_rule_id: (sharpen only) the existing rule id being sharpened\n"
        "- supersedes: (add_rule only) existing rule ids this replaces, else []\n"
        "- existing_rules_considered: (add_rule only) rule ids you reviewed\n"
        "- why_sharpen_is_insufficient: (add_rule only) one sentence\n"
        "- skill_slug / skill_body: (add_skill only) kebab slug + procedure body "
        "containing the anchor\n"
        "- confidence: float 0.0-1.0\n"
        "- rationale: one or two sentences citing the specific evidence above\n\n"
        "Do not invent facts not present in the evidence above."
    )


def build_retry_prompt(original_prompt: str, first_response: str, error: str) -> str:
    """Feed one invalid response and its validation error back to the drafter."""
    return (
        f"{original_prompt}\n\n"
        "--- RETRY: your previous response failed validation ---\n"
        f"Previous response:\n{first_response}\n\n"
        f"Validation error: {error}\n\n"
        'Reminder: if `operation` is "sharpen", `sharpen_rule_id` MUST be set '
        'to exactly one of the existing rule ids listed above under "Existing '
        'registry rules to consider sharpening" -- never null, never omitted, '
        "and never an invented id. If none of those rules actually fit, use "
        '`"operation": "add_rule"` instead of `"sharpen"`. Return ONLY the '
        "corrected JSON object matching the schema above. Do not include any "
        "explanation, markdown fences, or text outside the JSON object."
    )


def draft_and_normalize_with_retry(
    prompt: str,
    *,
    group_key: str,
    drafter_complete: Callable[[str], str],
    normalize_payload: Callable[[Mapping[str, Any]], T],
    validation_errors: tuple[type[Exception], ...],
    final_error_type: type[Exception],
) -> T:
    """Draft once, then retry once only when returned output fails validation.

    Transport/API failures must be raised by ``drafter_complete``. They are not
    validation responses and therefore propagate immediately from either call.
    """

    def normalize_response(response: str) -> T:
        payload = extract_json_payload(response)
        if not isinstance(payload, dict):
            raise ValueError("drafter response was not a JSON object")
        return normalize_payload(payload)

    response = drafter_complete(prompt)
    try:
        return normalize_response(response)
    except validation_errors as first_error:
        logger.warning(
            "Doc-proposal draft for group %s failed validation on the first attempt "
            "(%s); retrying once with the error fed back to the drafter.",
            group_key,
            first_error,
        )
        retry_prompt = build_retry_prompt(prompt, response, str(first_error))
        retry_response = drafter_complete(retry_prompt)
        try:
            return normalize_response(retry_response)
        except validation_errors as retry_error:
            raise final_error_type(
                f"drafter output invalid after one retry (first error: {first_error}; "
                f"retry error: {retry_error})"
            ) from retry_error


__all__ = [
    "build_doc_proposal_prompt",
    "build_retry_prompt",
    "draft_and_normalize_with_retry",
]
