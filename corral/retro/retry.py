"""Bounded capacity-retry wrapper for retrospective seat completions.

Generalized from the source retrospective's Anthropic-specific retry layer:
the source retried on HTTP 429/529 (rate limit / overloaded) with a bounded,
minutes-scale backoff on top of the SDK's own quick internal retries, because
a sustained rate-limit window once zeroed an entire week's drafting output.

Corral seats fold transport failures into :class:`SeatResult` statuses, so
this layer maps STATUSES instead of parsing vendor error objects:

- ``SeatStatus.OK``        -> success, return the text.
- ``SeatStatus.ERROR`` and ``SeatStatus.TIMEOUT`` -> transient capacity-style
  failures: retried on the same bounded backoff schedule.
- ``SeatStatus.UNAVAILABLE`` -> configuration-level failure (missing seat,
  missing auth): never retried, wrapped in :class:`NonRetriableLLMError`.
- Any exception escaping the completion callable -> :class:`NonRetriableLLMError`.
- Existing :class:`LLMCallError` instances are re-raised immediately so a
  terminal failure can never start a fresh retry budget.

There is deliberately no vendor-status or retry-after header parsing here.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from math import isfinite
from typing import Protocol

from corral.retro.providers.base import SeatResult, SeatStatus

#: One initial attempt plus three retries (four total), on a fixed backoff
#: schedule tuned for a weekly batch job where cost/latency is a non-issue.
RETRY_MAX_ATTEMPTS = 4
RETRY_BACKOFF_SECONDS: tuple[float, ...] = (15.0, 60.0, 180.0)
RETRY_JITTER_MAX_SECONDS = 3.0
#: A scheduled delay is capped so a degraded seat cannot consume the whole
#: weekly workflow deadline. The four-attempt budget stays below ~16 minutes.
RETRY_MAX_DELAY_SECONDS = 300.0

#: Seat statuses treated as transient capacity failures worth retrying.
RETRIABLE_STATUSES = frozenset({SeatStatus.ERROR, SeatStatus.TIMEOUT})


class LLMCallError(RuntimeError):
    """Terminal failure from the coarse weekly-batch seat retry layer."""

    terminal = True

    def __init__(self, message: str, *, status: str | None, attempts: int) -> None:
        super().__init__(message)
        self.status = status
        self.attempts = attempts


class NonRetriableLLMError(LLMCallError):
    """A seat failure that must fail the affected batch without retrying.

    Wraps unavailable seats, unexpected exceptions, and any non-transient
    completion status so callers cannot confuse them with returned-output
    validation errors that are safe to isolate to one evidence group.
    """


class RetriableLLMError(LLMCallError):
    """A drafting call was still failing transiently after every attempt.

    This is a TERMINAL wrapper, not another retriable input: callers should
    catch it DISTINCTLY from other drafting failures so the weekly run summary
    can say "N groups failed on transient seat errors -- re-run recommended"
    instead of folding a capacity failure into the generic skip list.
    """


class SeatCompleter(Protocol):
    def __call__(self, prompt: str) -> SeatResult: ...


def _backoff_delay(
    attempt: int,
    *,
    jitter: Callable[[], float] | None = None,
) -> float:
    """Delay before retry ``attempt + 1`` on the fixed bounded schedule."""
    scheduled = RETRY_BACKOFF_SECONDS[min(attempt - 1, len(RETRY_BACKOFF_SECONDS) - 1)]
    raw_spread = jitter() if jitter is not None else random.uniform(0, RETRY_JITTER_MAX_SECONDS)
    spread = min(max(raw_spread, 0.0), RETRY_JITTER_MAX_SECONDS) if isfinite(raw_spread) else 0.0
    delay = scheduled + spread
    return min(delay, RETRY_MAX_DELAY_SECONDS)


def call_with_retry(
    complete: SeatCompleter,
    prompt: str,
    *,
    context: str,
    max_attempts: int = RETRY_MAX_ATTEMPTS,
    sleep: Callable[[float], None] | None = None,
    jitter: Callable[[], float] | None = None,
) -> str:
    """Call ``complete(prompt)``, retrying transient seat failures with bounded
    backoff; return the successful completion text.

    ``context`` is a short human-readable label (e.g. "gotcha candidate
    drafting") used only in diagnostics. Raises :class:`RetriableLLMError` if
    every attempt hits a retriable status; :class:`NonRetriableLLMError` for
    unavailable seats or unexpected exceptions, on first occurrence.
    """
    if not 1 <= max_attempts <= RETRY_MAX_ATTEMPTS:
        raise ValueError(f"max_attempts must be between 1 and {RETRY_MAX_ATTEMPTS}")
    sleep_fn = sleep if sleep is not None else time.sleep
    last_result: SeatResult | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            result = complete(prompt)
        except LLMCallError:
            raise
        except Exception as exc:
            raise NonRetriableLLMError(
                f"{context}: non-retriable seat failure on attempt {attempt}: {exc}",
                status=None,
                attempts=attempt,
            ) from exc
        if result.ok:
            return result.text
        status = SeatStatus(result.status)
        if status not in RETRIABLE_STATUSES:
            raise NonRetriableLLMError(
                f"{context}: non-retriable seat failure on attempt {attempt} "
                f"(status={status.value}): {result.detail}",
                status=status.value,
                attempts=attempt,
            )
        last_result = result
        if attempt >= max_attempts:
            break
        delay = _backoff_delay(attempt, jitter=jitter)
        sleep_fn(delay)
    assert last_result is not None
    raise RetriableLLMError(
        f"{context}: still failing after {max_attempts} attempt(s) "
        f"(last status={SeatStatus(last_result.status).value}: {last_result.detail})",
        status=SeatStatus(last_result.status).value,
        attempts=max_attempts,
    )


__all__ = [
    "LLMCallError",
    "NonRetriableLLMError",
    "RETRIABLE_STATUSES",
    "RETRY_BACKOFF_SECONDS",
    "RETRY_JITTER_MAX_SECONDS",
    "RETRY_MAX_ATTEMPTS",
    "RETRY_MAX_DELAY_SECONDS",
    "RetriableLLMError",
    "SeatCompleter",
    "call_with_retry",
]
