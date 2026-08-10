"""Candidate drafting helpers for the weekly retrospective.

The drafter callable is supplied by the caller (the pipeline builds one from
:meth:`SeatRunner.complete` on ``retro.drafter_seat`` wrapped in
:mod:`corral.retro.retry`). This module only handles returned-output
validation: parse the JSON payload, normalize it into a candidate, and retry
ONCE with the validation error fed back to the model before giving up on the
evidence group.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import date

from corral.retro.json_util import extract_json_payload
from corral.retro.mining import (
    DEFAULT_SEVERITIES,
    EvidenceGroup,
    GotchaCandidate,
    build_prompt,
    normalize_candidate,
)


def extract_candidate(
    group: EvidenceGroup,
    excerpts: Mapping[int, str],
    llm_complete: Callable[[str], str],
    *,
    created_on: date | None = None,
    allowed_severities: Sequence[str] = DEFAULT_SEVERITIES,
) -> GotchaCandidate:
    """Call ``llm_complete(prompt) -> str`` and normalize its JSON response."""
    response = llm_complete(build_prompt(group, excerpts, allowed_severities=allowed_severities))
    return _parse_candidate_response(
        response, group, created_on=created_on, allowed_severities=allowed_severities
    )


def _parse_candidate_response(
    response: str,
    group: EvidenceGroup,
    *,
    created_on: date | None,
    allowed_severities: Sequence[str],
) -> GotchaCandidate:
    payload = extract_json_payload(response)
    if not isinstance(payload, dict):
        raise ValueError("seat response did not contain a JSON object")
    return normalize_candidate(
        payload, group, created_on=created_on, allowed_severities=allowed_severities
    )


def build_retry_prompt(original_prompt: str, first_response: str, error: str) -> str:
    """Feed a failed first response and validation error back to the model once."""
    return (
        f"{original_prompt}\n\n"
        "--- RETRY: your previous response failed schema validation ---\n"
        f"Previous response:\n{first_response}\n\n"
        f"Validation error: {error}\n\n"
        "Return ONLY the corrected JSON object matching the schema above. Do not "
        "include any explanation, markdown fences, or text outside the JSON object."
    )


def extract_candidate_with_retry(
    group: EvidenceGroup,
    excerpts: Mapping[int, str],
    llm_complete: Callable[[str], str],
    *,
    created_on: date | None = None,
    allowed_severities: Sequence[str] = DEFAULT_SEVERITIES,
    severe_severities: Sequence[str] = (),
) -> GotchaCandidate:
    """Draft a candidate, retrying once on JSON/schema validation failure."""
    prompt = build_prompt(
        group,
        excerpts,
        allowed_severities=allowed_severities,
        severe_severities=severe_severities,
    )
    response = llm_complete(prompt)
    try:
        return _parse_candidate_response(
            response, group, created_on=created_on, allowed_severities=allowed_severities
        )
    except ValueError as first_error:
        retry_prompt = build_retry_prompt(prompt, response, str(first_error))
        retry_response = llm_complete(retry_prompt)
        try:
            return _parse_candidate_response(
                retry_response, group, created_on=created_on, allowed_severities=allowed_severities
            )
        except ValueError as retry_error:
            raise ValueError(
                f"seat response invalid after one retry (first error: {first_error}; "
                f"retry error: {retry_error})"
            ) from retry_error


__all__ = [
    "build_retry_prompt",
    "extract_candidate",
    "extract_candidate_with_retry",
]
