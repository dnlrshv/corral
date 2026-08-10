"""Independent gotcha verification through configured model seats.

Gotcha verification is verification-preferred: by default an unavailable or
malformed verifier produces ``UNVERIFIED`` and the drafted candidate may
proceed.  Only an explicit ``REFUTE`` rejects it.  Deployments can opt into a
fail-closed gotcha policy, while document verification always fails closed.

Failure reasons are ``"<seat-status>: <detail>"`` strings whose first token
is a seat status (``unavailable`` | ``error`` | ``timeout``).  The verdict
parser accepts the source retrospective's labeled ``VERDICT:`` format first;
strict JSON is only an additive fallback for replies that would otherwise be
``UNVERIFIED``.
"""

from __future__ import annotations

import dataclasses
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from corral.retro.providers import runner_for_seat
from corral.retro.providers.base import SeatResult, SeatRunner, SeatStatus
from corral.retro.seats import Seat, SeatRegistry

DEFAULT_VERIFICATION_TIMEOUT_S = 300
DEFAULT_MAX_TOKENS = 1000
MAX_EXCERPT_CHARS_PER_PR = 1500

Verdict = Literal["CONFIRM", "REFUTE", "UNVERIFIED"]
RunnerFactory = Callable[[Seat], SeatRunner]

_VERDICT_RE = re.compile(r"^VERDICT:\s*(CONFIRM|REFUTE)\s*$", re.IGNORECASE | re.MULTILINE)
_REASONING_RE = re.compile(
    r"^REASONING:\s*(.+?)(?=^SHARPENED:|\Z)", re.IGNORECASE | re.MULTILINE | re.DOTALL
)
_SHARPENED_RE = re.compile(r"^SHARPENED:\s*(.+)\Z", re.IGNORECASE | re.MULTILINE | re.DOTALL)


@dataclass(frozen=True)
class GotchaCandidate:
    """Minimal candidate shape accepted by :func:`verify_candidate`."""

    rule: str
    rationale: str = ""
    source_prs: Sequence[int] = field(default_factory=tuple)


@dataclass(frozen=True)
class CandidateVerification:
    """Outcome of one independent verification call.

    The historic name describes the verdict shape, not a provider.  Provenance
    always identifies the configured seat that actually answered.
    """

    verdict: Verdict
    reasoning: str
    sharpened_rule: str | None = None
    unverified_reason: str | None = None
    raw_output: str = ""
    verifier_alias: str = ""
    verifier_provider: str = ""
    verifier_model: str = ""
    status: SeatStatus = SeatStatus.OK


@dataclass(frozen=True)
class VerifierOutcome:
    """Raw result of ordered verifier fallback."""

    raw_output: str | None
    seat: Seat | None
    failure_reason: str | None
    status: SeatStatus = SeatStatus.OK
    result: SeatResult | None = None
    runner: SeatRunner | None = field(default=None, repr=False, compare=False)


def build_verification_prompt(
    candidate: GotchaCandidate,
    excerpts: Mapping[int, str],
    *,
    bridge_evidence: str = "",
) -> str:
    """Build the source retrospective's strict CONFIRM/REFUTE prompt."""
    excerpt_blocks = []
    for pr_number in candidate.source_prs:
        excerpt = (excerpts.get(pr_number, "") or "").strip()[:MAX_EXCERPT_CHARS_PER_PR]
        excerpt_blocks.append(f"### PR #{pr_number}\n{excerpt or '(no excerpt available)'}")
    if bridge_evidence:
        excerpt_blocks.append(f"### Sanitized file-backed evidence\n{bridge_evidence}")
    excerpts_text = "\n\n".join(excerpt_blocks) or "(no evidence excerpts available)"
    return (
        "You are an independent verifier for an AI-agent gotcha rule proposed by "
        "another model from fix-up PR evidence in a weekly retrospective. Decide "
        "whether the evidence below actually supports this rule as a durable, "
        "reusable lesson. REFUTE coincidental, overreaching, or evidence-free "
        "rules rather than rubber-stamping them -- this job is judged on "
        "precision, not agreement.\n\n"
        f"## Proposed rule\n{candidate.rule}\n\n"
        f"## Drafter's rationale\n{candidate.rationale or '(none given)'}\n\n"
        f"## Evidence excerpts\n{excerpts_text}\n\n"
        "Respond in EXACTLY this format (plain text, no markdown fences, no extra "
        "sections, each label starting a new line):\n\n"
        "VERDICT: CONFIRM or REFUTE\n"
        "REASONING: one paragraph explaining your verdict, citing the evidence "
        "above.\n"
        "SHARPENED: a tightened, more precise wording of the rule if you can "
        "improve it, or the single word NONE if the original wording is already "
        "fine or you REFUTE the rule.\n"
    )


def _extract_json_object(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 3 and lines[-1].strip() == "```":
            stripped = "\n".join(lines[1:-1])
    decoder = json.JSONDecoder()
    for index, char in enumerate(stripped):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(stripped[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def parse_verdict(raw_output: str) -> CandidateVerification:
    """Parse the source's labeled verdict format; JSON is an additive fallback.

    The labeled format is tried FIRST so the added JSON support never changes
    the source's precedence: an output the source runtime would have parsed
    keeps exactly that verdict, and a JSON object (for example one quoted
    inside the REASONING paragraph) can only win when no labeled
    ``VERDICT:`` line exists at all.  An unparseable reply is ``UNVERIFIED``,
    never an implicit refutation.  JSON supports ``reasoning`` (or
    ``reason``) and ``sharpened`` (or ``sharpened_rule``), which makes the
    runtime tolerant of compatible HTTP endpoints that are configured for
    structured output.
    """
    verdict_match = _VERDICT_RE.search(raw_output)
    if verdict_match:
        verdict: Verdict = verdict_match.group(1).upper()  # type: ignore[assignment]
        reasoning_match = _REASONING_RE.search(raw_output)
        reasoning = reasoning_match.group(1).strip() if reasoning_match else ""
        sharpened_match = _SHARPENED_RE.search(raw_output)
        sharpened_raw = sharpened_match.group(1).strip() if sharpened_match else ""
        return CandidateVerification(
            verdict=verdict,
            reasoning=reasoning,
            sharpened_rule=(
                sharpened_raw if sharpened_raw and sharpened_raw.upper() != "NONE" else None
            ),
            raw_output=raw_output,
        )

    payload = _extract_json_object(raw_output)
    if payload is not None:
        verdict_raw = str(payload.get("verdict", "")).strip().upper()
        if verdict_raw in ("CONFIRM", "REFUTE"):
            sharpened_raw = payload.get("sharpened", payload.get("sharpened_rule"))
            sharpened = str(sharpened_raw).strip() if sharpened_raw is not None else ""
            return CandidateVerification(
                verdict=verdict_raw,  # type: ignore[arg-type]
                reasoning=str(payload.get("reasoning", payload.get("reason", ""))).strip(),
                sharpened_rule=(
                    sharpened if sharpened and sharpened.upper() != "NONE" else None
                ),
                raw_output=raw_output,
            )

    return CandidateVerification(
        verdict="UNVERIFIED",
        reasoning="",
        unverified_reason="verifier output did not contain a parseable verdict",
        raw_output=raw_output,
        status=SeatStatus.ERROR,
    )


def _status(value: SeatStatus | str) -> SeatStatus:
    try:
        return SeatStatus(value)
    except ValueError:
        return SeatStatus.ERROR


def _is_ok(value: SeatStatus | str) -> bool:
    return _status(value) is SeatStatus.OK


def invoke_verifier_with_fallback(
    prompt: str,
    *,
    registry: SeatRegistry,
    drafter_seat: str = "retro-drafter",
    verifier_seats: Sequence[str] = ("retro-verifier",),
    require_distinct_provider: bool = True,
    timeout_s: float = DEFAULT_VERIFICATION_TIMEOUT_S,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    runner_factory: RunnerFactory | None = None,
) -> VerifierOutcome:
    """Try verifier seats in configured order, advancing on every degradation."""
    factory = runner_factory or runner_for_seat
    drafter = registry.get(drafter_seat) if require_distinct_provider else None
    if require_distinct_provider and drafter is None:
        return VerifierOutcome(
            None,
            None,
            f"unavailable: drafter seat {drafter_seat!r} is not configured",
            SeatStatus.UNAVAILABLE,
        )

    last_reason = "unavailable: no verifier seats configured"
    last_status = SeatStatus.UNAVAILABLE
    for name in verifier_seats:
        seat = registry.get(name)
        if seat is None:
            last_reason = f"unavailable: verifier seat {name!r} is not configured"
            last_status = SeatStatus.UNAVAILABLE
            continue
        if (
            require_distinct_provider
            and drafter is not None
            and seat.provider.strip().casefold() == drafter.provider.strip().casefold()
        ):
            last_reason = (
                f"unavailable: verifier seat {name!r} has the same provider "
                f"as drafter seat {drafter_seat!r}"
            )
            last_status = SeatStatus.UNAVAILABLE
            continue

        try:
            runner = factory(seat)
            probe = runner.probe(seat)
        except Exception as exc:
            last_reason = f"unavailable: verifier seat {name!r} probe failed: {exc}"
            last_status = SeatStatus.UNAVAILABLE
            continue
        if not _is_ok(probe.status):
            last_status = _status(probe.status)
            if last_status is not SeatStatus.UNAVAILABLE:
                last_status = SeatStatus.UNAVAILABLE
            last_reason = f"unavailable: verifier seat {name!r}: {probe.detail}"
            continue

        try:
            completion = runner.complete(
                seat,
                prompt,
                timeout=timeout_s,
                max_tokens=max_tokens,
            )
        except Exception as exc:
            last_reason = f"error: verifier seat {name!r} raised: {exc}"
            last_status = SeatStatus.ERROR
            continue
        completion_status = _status(completion.status)
        if completion_status is not SeatStatus.OK:
            last_status = completion_status
            last_reason = f"{completion_status.value}: verifier seat {name!r}: {completion.detail}"
            continue
        return VerifierOutcome(
            completion.text,
            seat,
            None,
            SeatStatus.OK,
            result=completion,
            runner=runner,
        )
    return VerifierOutcome(None, None, last_reason, last_status)


def _failure(reason: str, status: SeatStatus, policy: str) -> CandidateVerification:
    if policy in {"fail-closed", "refute"}:
        return CandidateVerification(
            verdict="REFUTE",
            reasoning=reason,
            unverified_reason=reason,
            status=status,
        )
    return CandidateVerification(
        verdict="UNVERIFIED",
        reasoning="",
        unverified_reason=reason,
        status=status,
    )


def _retry_prompt(original_prompt: str, invalid_output: str) -> str:
    return (
        original_prompt
        + "\n\nYour previous response could not be parsed. Return exactly one valid verdict "
        "in the requested format, with no preamble. Previous response:\n"
        + invalid_output[:2000]
    )


def verify_candidate(
    candidate: GotchaCandidate,
    excerpts: Mapping[int, str],
    *,
    bridge_evidence: str = "",
    registry: SeatRegistry | None = None,
    config: object | None = None,
    config_path: Path | str | None = None,
    drafter_seat: str | None = None,
    verifier_seats: Sequence[str] | None = None,
    require_distinct_provider: bool | None = None,
    timeout_s: float | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    unavailable_policy: str | None = None,
    runner_factory: RunnerFactory | None = None,
) -> CandidateVerification:
    """Verify one gotcha candidate, retrying one malformed verdict once."""
    if config is None and registry is None:
        from corral.config import load_config

        config = load_config(config_path)
    if registry is None:
        registry = SeatRegistry.from_config(config)

    retro = getattr(config, "retro", None)
    drafter_name = drafter_seat or getattr(retro, "drafter_seat", "retro-drafter")
    verifier_names = list(
        verifier_seats
        if verifier_seats is not None
        else getattr(retro, "verifier_seats", ["retro-verifier"])
    )
    distinct = (
        require_distinct_provider
        if require_distinct_provider is not None
        else getattr(retro, "require_distinct_provider", True)
    )
    timeout = timeout_s or getattr(retro, "verification_timeout_s", DEFAULT_VERIFICATION_TIMEOUT_S)
    policy = unavailable_policy or getattr(
        retro, "gotcha_unavailable_policy", "proceed-unverified"
    )

    prompt = build_verification_prompt(candidate, excerpts, bridge_evidence=bridge_evidence)
    outcome = invoke_verifier_with_fallback(
        prompt,
        registry=registry,
        drafter_seat=drafter_name,
        verifier_seats=verifier_names,
        require_distinct_provider=distinct,
        timeout_s=timeout,
        max_tokens=max_tokens,
        runner_factory=runner_factory,
    )
    if outcome.raw_output is None or outcome.seat is None:
        return _failure(
            outcome.failure_reason or "unavailable: no verifier completed",
            outcome.status,
            policy,
        )

    verdict = parse_verdict(outcome.raw_output)
    provenance_result = outcome.result
    if verdict.verdict == "UNVERIFIED":
        try:
            retry = outcome.runner.complete(
                outcome.seat,
                _retry_prompt(prompt, outcome.raw_output),
                timeout=timeout,
                max_tokens=max_tokens,
            ) if outcome.runner is not None else None
        except Exception as exc:
            retry = SeatResult(
                "", outcome.seat.provider, outcome.seat.model, SeatStatus.ERROR, str(exc), outcome.seat.name
            )
        if retry is None or not _is_ok(retry.status):
            status = _status(retry.status) if retry is not None else SeatStatus.ERROR
            detail = retry.detail if retry is not None else "retry runner unavailable"
            return _failure(f"{status.value}: verdict retry failed: {detail}", status, policy)
        provenance_result = retry
        verdict = parse_verdict(retry.text)
        if verdict.verdict == "UNVERIFIED":
            return _failure(
                "error: verifier returned an invalid verdict after one retry",
                SeatStatus.ERROR,
                policy,
            )

    return dataclasses.replace(
        verdict,
        verifier_alias=outcome.seat.name,
        verifier_provider=(
            provenance_result.provider if provenance_result else outcome.seat.provider
        ),
        verifier_model=provenance_result.model if provenance_result else outcome.seat.model,
        status=SeatStatus.OK,
    )


__all__ = [
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_VERIFICATION_TIMEOUT_S",
    "CandidateVerification",
    "GotchaCandidate",
    "Verdict",
    "VerifierOutcome",
    "build_verification_prompt",
    "invoke_verifier_with_fallback",
    "parse_verdict",
    "verify_candidate",
]
