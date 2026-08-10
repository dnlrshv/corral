"""Fail-closed verification for retrospective document/skill proposals."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from corral.retro.providers.base import SeatStatus
from corral.retro.seats import SeatRegistry
from corral.retro.verification import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_VERIFICATION_TIMEOUT_S,
    RunnerFactory,
    _extract_json_object,
    invoke_verifier_with_fallback,
)

Completer = Callable[[str], str]
_UNREACHABLE_VERDICT = "refute"


@dataclass(frozen=True)
class VerificationResult:
    """Independent verifier verdict on one drafted proposal."""

    verdict: str
    reason: str
    confidence: float
    verifier_seat: str = ""
    provider: str = ""
    model: str = ""
    status: SeatStatus = SeatStatus.OK

    @property
    def confirmed(self) -> bool:
        return self.verdict == "confirm"


def build_verification_prompt(*, subject: str, evidence: str, contract: str) -> str:
    """Prompt an independent verifier to confirm/refute a drafted proposal."""
    return (
        "You are an INDEPENDENT verifier for a weekly agent-instruction "
        "retrospective. A drafter model proposed a behaviour-changing edit to an "
        "agent instruction file from the evidence below. Your job is to CONFIRM the "
        "proposal only if the evidence genuinely supports it and it respects the "
        "governance contract, or REFUTE it otherwise. Refuting is the safe default: "
        "a zero-proposal week is a successful outcome, so refute anything that is "
        "weakly evidenced, a near-duplicate of an existing rule, mis-placed on the "
        "tier ladder, or not actually actionable.\n\n"
        f"## Governance contract the proposal must satisfy\n{contract}\n\n"
        f"## Drafted proposal\n{subject}\n\n"
        f"## Evidence the drafter saw\n{evidence}\n\n"
        "Return ONLY a JSON object:\n"
        '- verdict: "confirm" or "refute"\n'
        "- reason: one or two sentences citing the specific evidence or contract "
        "clause behind your verdict\n"
        "- confidence: float 0.0-1.0\n\n"
        "Do not invent facts not present in the evidence above."
    )


def _coerce_confidence(value: Any) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return 0.0
    return max(0.0, min(1.0, float(value)))


def parse_verification_response(response: str) -> VerificationResult:
    """Parse a confirm/refute JSON object, refuting every malformed response."""
    payload = _extract_json_object(response)
    if not isinstance(payload, dict):
        return VerificationResult(
            _UNREACHABLE_VERDICT,
            "verifier response was not a JSON object",
            0.0,
            status=SeatStatus.ERROR,
        )
    verdict = str(payload.get("verdict", "")).strip().lower()
    if verdict not in ("confirm", "refute"):
        return VerificationResult(
            _UNREACHABLE_VERDICT,
            f"verifier returned an invalid verdict {verdict!r}",
            0.0,
            status=SeatStatus.ERROR,
        )
    return VerificationResult(
        verdict=verdict,
        reason=str(payload.get("reason", "")).strip(),
        confidence=_coerce_confidence(payload.get("confidence")),
    )


def verify(
    *,
    subject: str,
    evidence: str,
    contract: str,
    registry: SeatRegistry | None = None,
    config: object | None = None,
    config_path: Path | str | None = None,
    drafter_seat: str | None = None,
    verifier_seats: Sequence[str] | None = None,
    require_distinct_provider: bool | None = None,
    timeout_s: float | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    runner_factory: RunnerFactory | None = None,
    verifier_complete: Completer | None = None,
) -> VerificationResult:
    """Run one independent pass; any call or parse failure refutes.

    ``verifier_complete`` retains the source module's injection seam for
    lightweight callers.  Normal runtime invocation resolves and executes a
    :class:`~corral.retro.providers.base.SeatRunner` from the seat registry.
    """
    prompt = build_verification_prompt(subject=subject, evidence=evidence, contract=contract)
    if verifier_complete is not None:
        try:
            response = verifier_complete(prompt)
        except Exception as exc:
            return VerificationResult(
                _UNREACHABLE_VERDICT,
                f"verifier unreachable: {exc}",
                0.0,
                status=SeatStatus.UNAVAILABLE,
            )
        return parse_verification_response(response)

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
        return VerificationResult(
            _UNREACHABLE_VERDICT,
            outcome.failure_reason or "verifier unavailable",
            0.0,
            status=outcome.status,
        )
    parsed = parse_verification_response(outcome.raw_output)
    return VerificationResult(
        parsed.verdict,
        parsed.reason,
        parsed.confidence,
        verifier_seat=outcome.seat.name,
        provider=outcome.result.provider if outcome.result else outcome.seat.provider,
        model=outcome.result.model if outcome.result else outcome.seat.model,
        status=parsed.status,
    )


__all__ = [
    "Completer",
    "VerificationResult",
    "build_verification_prompt",
    "parse_verification_response",
    "verify",
]
