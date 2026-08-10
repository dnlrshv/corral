from __future__ import annotations

import pytest

from corral.retro.providers.base import SeatResult, SeatStatus
from corral.retro.retry import (
    LLMCallError,
    NonRetriableLLMError,
    RETRY_BACKOFF_SECONDS,
    RetriableLLMError,
    call_with_retry,
)


def result(status: SeatStatus, text: str = "", detail: str = "d") -> SeatResult:
    return SeatResult(text, "p", "m", status, detail, "seat")


def test_ok_returns_text_without_sleep() -> None:
    sleeps: list[float] = []
    text = call_with_retry(
        lambda prompt: result(SeatStatus.OK, text="hello"),
        "p",
        context="ctx",
        sleep=sleeps.append,
    )
    assert text == "hello"
    assert sleeps == []


def test_error_then_timeout_then_ok_retries_on_schedule() -> None:
    results = [result(SeatStatus.ERROR), result(SeatStatus.TIMEOUT), result(SeatStatus.OK, text="ok")]
    sleeps: list[float] = []
    text = call_with_retry(
        lambda prompt: results.pop(0),
        "p",
        context="ctx",
        sleep=sleeps.append,
        jitter=lambda: 0.0,
    )
    assert text == "ok"
    assert sleeps == [RETRY_BACKOFF_SECONDS[0], RETRY_BACKOFF_SECONDS[1]]


def test_injected_jitter_is_clamped_to_source_bound() -> None:
    results = [result(SeatStatus.ERROR), result(SeatStatus.OK, text="ok")]
    sleeps: list[float] = []
    call_with_retry(
        lambda prompt: results.pop(0),
        "p",
        context="ctx",
        sleep=sleeps.append,
        jitter=lambda: 99.0,
    )
    assert sleeps == [RETRY_BACKOFF_SECONDS[0] + 3.0]


def test_exhausted_retries_raise_retriable() -> None:
    sleeps: list[float] = []
    with pytest.raises(RetriableLLMError) as excinfo:
        call_with_retry(
            lambda prompt: result(SeatStatus.ERROR),
            "p",
            context="drafting",
            sleep=sleeps.append,
            jitter=lambda: 0.0,
        )
    assert excinfo.value.attempts == 4
    assert excinfo.value.status == "error"
    assert "drafting" in str(excinfo.value)
    assert sleeps == list(RETRY_BACKOFF_SECONDS)


def test_unavailable_is_non_retriable() -> None:
    calls = []

    def complete(prompt: str) -> SeatResult:
        calls.append(prompt)
        return result(SeatStatus.UNAVAILABLE)

    with pytest.raises(NonRetriableLLMError) as excinfo:
        call_with_retry(complete, "p", context="ctx", sleep=lambda s: None)
    assert calls == ["p"]
    assert excinfo.value.status == "unavailable"


def test_unexpected_exception_is_non_retriable_and_llm_errors_pass_through() -> None:
    def boom(prompt: str) -> SeatResult:
        raise RuntimeError("transport exploded")

    with pytest.raises(NonRetriableLLMError):
        call_with_retry(boom, "p", context="ctx", sleep=lambda s: None)

    terminal = RetriableLLMError("already terminal", status="error", attempts=4)
    with pytest.raises(LLMCallError) as excinfo:
        call_with_retry(lambda prompt: (_ for _ in ()).throw(terminal), "p", context="ctx")
    assert excinfo.value is terminal


def test_max_attempts_bounds() -> None:
    with pytest.raises(ValueError):
        call_with_retry(lambda prompt: result(SeatStatus.OK), "p", context="ctx", max_attempts=0)
    with pytest.raises(ValueError):
        call_with_retry(lambda prompt: result(SeatStatus.OK), "p", context="ctx", max_attempts=5)
