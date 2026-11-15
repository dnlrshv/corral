"""Anthropic Messages API seat adapter with explicit credential isolation."""

from __future__ import annotations

import json
import os
from typing import Any

from corral.retro.providers.base import (
    Availability,
    SeatResult,
    SeatRunner,
    SeatStatus,
    _seat_max_tokens,
    availability,
    result,
)
from corral.retro.seats import Seat
from corral.retro.response_formats import ResponseFormat


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
        return self._create(seat, prompt, timeout=timeout, max_tokens=max_tokens)

    def complete_structured(
        self, seat: Seat, prompt: str, *, timeout: float, max_tokens: int,
        response_format: ResponseFormat,
    ) -> SeatResult:
        """Use an opt-in schema and render back into the shared text contract."""
        if seat.options.get("structured_output") is not True:
            return self.complete(seat, prompt, timeout=timeout, max_tokens=max_tokens)
        if response_format.prompt_note:
            prompt += "\n\n" + response_format.prompt_note
        return self._create(seat, prompt, timeout=timeout, max_tokens=max_tokens,
                            response_format=response_format)

    def _create(
        self, seat: Seat, prompt: str, *, timeout: float, max_tokens: int,
        response_format: ResponseFormat | None = None,
    ) -> SeatResult:
        max_tokens = _seat_max_tokens(seat, max_tokens)
        probe = self.probe(seat)
        if not probe.available:
            return result(seat, SeatStatus.UNAVAILABLE, detail=probe.detail)

        import anthropic  # lazy; probe established that the extra is installed

        token = _auth(seat)
        if token is None:  # credential could have been removed after probe
            return result(seat, SeatStatus.UNAVAILABLE, detail="configured credential disappeared")
        try:
            client = anthropic.Anthropic(api_key=token, timeout=timeout)
            create_kwargs: dict[str, Any] = {
                "model": seat.model,
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            }
            output_config: dict[str, Any] = {}
            if "effort" in seat.options:
                output_config["effort"] = seat.options["effort"]
            if response_format is not None:
                output_config["format"] = {
                    "type": "json_schema", "schema": dict(response_format.schema),
                }
            if output_config:
                create_kwargs["output_config"] = output_config
            message = client.messages.create(**create_kwargs)
            stop_reason = getattr(message, "stop_reason", None)
            if stop_reason == "refusal":
                details = getattr(message, "stop_details", None)
                category = getattr(details, "category", None) or "unspecified"
                return result(seat, SeatStatus.INCOMPLETE,
                              detail=f"refusal (category={category})")
            if stop_reason == "max_tokens":
                return result(seat, SeatStatus.INCOMPLETE, detail=(
                    f"stopped at the output limit (max_tokens={max_tokens}) before the "
                    "reply finished; raise retro.max_tokens or the seat's "
                    "options.max_tokens"))
            blocks = getattr(message, "content", None) or []
            parts: list[str] = []
            for block in blocks:
                if isinstance(block, dict):
                    if block.get("type", "text") == "text" and isinstance(block.get("text"), str):
                        parts.append(block["text"])
                elif getattr(block, "type", "text") == "text":
                    parts.append(getattr(block, "text", ""))
            text = "".join(parts).strip()
            if not text and stop_reason == "end_turn":
                return result(seat, SeatStatus.INCOMPLETE, detail="reply contained no text block")
            if response_format is not None:
                try:
                    payload = json.loads(parts[0] if parts else "")
                    if not isinstance(payload, dict):
                        raise ValueError("expected a JSON object")
                    for key in response_format.schema.get("required", []):
                        if key not in payload:
                            raise KeyError(key)
                    text = response_format.render(payload)
                except (ValueError, KeyError, TypeError) as exc:
                    return result(seat, SeatStatus.ERROR, detail=(
                        f"structured output did not match the response schema: {exc}"))
            return result(seat, SeatStatus.OK, text=text)
        except Exception as exc:
            if _is_timeout(exc, anthropic):
                return result(seat, SeatStatus.TIMEOUT, detail=f"request timed out after {timeout}s")
            return result(seat, SeatStatus.ERROR, detail=f"Anthropic request failed: {exc}")


__all__ = ["AnthropicSeatRunner"]
