"""Stdlib HTTP adapter for OpenAI-compatible endpoints."""

from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
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


def _endpoint(seat: Seat) -> str | None:
    env_name = str(seat.options.get("base_url_env", ""))
    return os.environ.get(env_name) or None


def _auth(seat: Seat) -> str | None:
    return os.environ.get(seat.auth_env) or None if seat.auth_env else None


def _url(base_url: str, protocol: str) -> str:
    suffix = "/chat/completions" if protocol == "chat-completions" else "/responses"
    base = base_url.rstrip("/")
    return base if base.endswith(suffix) else base + suffix


def _chat_text(payload: MappingLike) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("response has no choices")
    choice = choices[0]
    if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
        raise ValueError("response choice has no message")
    content = choice["message"].get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts).strip()
    raise ValueError("response message has no text content")


def _responses_text(payload: MappingLike) -> str:
    output_text = payload.get("output_text")
    if isinstance(output_text, str):
        return output_text.strip()
    output = payload.get("output")
    if not isinstance(output, list):
        raise ValueError("response has no output text")
    parts: list[str] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
    if not parts:
        raise ValueError("response has no output text")
    return "".join(parts).strip()


MappingLike = dict[str, Any]


def _timed_out(exc: BaseException) -> bool:
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return True
    return isinstance(exc, urllib.error.URLError) and isinstance(
        exc.reason, (TimeoutError, socket.timeout)
    )


class OpenAICompatibleSeatRunner(SeatRunner):
    """Invoke chat-completions or responses without an OpenAI dependency."""

    def probe(self, seat: Seat) -> Availability:
        base_env = str(seat.options.get("base_url_env", ""))
        if not _endpoint(seat):
            return availability(
                seat,
                SeatStatus.UNAVAILABLE,
                f"base URL environment variable {base_env} is not set",
            )
        if seat.auth_env is None:
            return availability(seat, SeatStatus.UNAVAILABLE, "auth_env is not configured")
        if _auth(seat) is None:
            return availability(
                seat,
                SeatStatus.UNAVAILABLE,
                f"credential environment variable {seat.auth_env} is not set",
            )
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

        protocol = str(seat.options["protocol"])
        if protocol == "chat-completions":
            body: dict[str, Any] = {
                "model": seat.model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens,
            }
        else:
            body = {"model": seat.model, "input": prompt, "max_output_tokens": max_tokens}
        headers = {
            "Authorization": f"Bearer {_auth(seat)}",
            "Content-Type": "application/json",
        }
        request = urllib.request.Request(
            _url(_endpoint(seat) or "", protocol),
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                decoded = json.loads(response.read().decode("utf-8"))
            if not isinstance(decoded, dict):
                raise ValueError("response JSON is not an object")
            text = _chat_text(decoded) if protocol == "chat-completions" else _responses_text(decoded)
            return result(seat, SeatStatus.OK, text=text)
        except Exception as exc:
            if _timed_out(exc):
                return result(seat, SeatStatus.TIMEOUT, detail=f"request timed out after {timeout}s")
            return result(seat, SeatStatus.ERROR, detail=f"endpoint request failed: {exc}")


__all__ = ["OpenAICompatibleSeatRunner"]
