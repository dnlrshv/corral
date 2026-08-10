"""Anthropic Messages API seat adapter with explicit credential isolation."""

from __future__ import annotations

import os
from typing import Any

from corral.retro.providers.base import (
    Availability,
    SeatResult,
    SeatRunner,
    SeatStatus,
    availability,
    result,
)
from corral.retro.seats import Seat


def _auth(seat: Seat) -> str | None:
    # Do not instantiate the SDK without an explicit token: doing so would let
    # it discover ambient ANTHROPIC_API_KEY/ANTHROPIC_AUTH_TOKEN variables.
    if seat.auth_env is None:
        return None
    return os.environ.get(seat.auth_env) or None


def _is_timeout(exc: BaseException, anthropic: Any | None = None) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    timeout_type = getattr(anthropic, "APITimeoutError", None) if anthropic else None
    return bool(timeout_type and isinstance(exc, timeout_type))


class AnthropicSeatRunner(SeatRunner):
    """Invoke a seat through the optional ``anthropic`` package."""

    def probe(self, seat: Seat) -> Availability:
        if seat.auth_env is None:
            return availability(seat, SeatStatus.UNAVAILABLE, "auth_env is not configured")
        if _auth(seat) is None:
            return availability(
                seat,
                SeatStatus.UNAVAILABLE,
                f"credential environment variable {seat.auth_env} is not set",
            )
        try:
            import anthropic  # noqa: F401 - deliberately lazy optional dependency
        except ImportError:
            return availability(
                seat,
                SeatStatus.UNAVAILABLE,
                "anthropic package is not installed; install corral[preflight]",
            )
        except Exception as exc:
            return availability(seat, SeatStatus.UNAVAILABLE, f"SDK probe failed: {exc}")
        return availability(seat, SeatStatus.OK)

    def complete(
        self,
        seat: Seat,
        prompt: str,
        *,
        timeout: float,
        max_tokens: int,
    ) -> SeatResult:
        probe = self.probe(seat)
        if not probe.available:
            return result(seat, SeatStatus.UNAVAILABLE, detail=probe.detail)

        import anthropic  # lazy; probe established that the extra is installed

        token = _auth(seat)
        if token is None:  # credential could have been removed after probe
            return result(seat, SeatStatus.UNAVAILABLE, detail="configured credential disappeared")
        try:
            client = anthropic.Anthropic(api_key=token, timeout=timeout)
            message = client.messages.create(
                model=seat.model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            blocks = getattr(message, "content", None) or []
            parts: list[str] = []
            for block in blocks:
                if isinstance(block, dict):
                    if block.get("type", "text") == "text" and isinstance(block.get("text"), str):
                        parts.append(block["text"])
                elif getattr(block, "type", "text") == "text":
                    parts.append(getattr(block, "text", ""))
            text = "".join(parts).strip()
            return result(seat, SeatStatus.OK, text=text)
        except Exception as exc:
            if _is_timeout(exc, anthropic):
                return result(seat, SeatStatus.TIMEOUT, detail=f"request timed out after {timeout}s")
            return result(seat, SeatStatus.ERROR, detail=f"Anthropic request failed: {exc}")


__all__ = ["AnthropicSeatRunner"]
