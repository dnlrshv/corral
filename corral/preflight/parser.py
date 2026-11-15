"""Parsing helpers for preflight model output (JSON) and error redaction."""

from __future__ import annotations

import json

from corral.preflight.contract import BriefResponseError
from corral.redaction import redact_text


def parse_brief_json(text: str) -> object:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise BriefResponseError(f"response is not valid JSON: {exc.msg}") from exc


def sanitize_preflight_error(exc: Exception, max_length: int = 240) -> str:
    """Return a concise diagnostic that redacts obvious token-like secrets."""
    lines = [line.strip() for line in str(exc).splitlines() if line.strip()]
    summary = "; ".join(lines) if lines else repr(exc)
    message = f"{type(exc).__name__}: {summary}"
    message = redact_text(message, marker="[REDACTED]")
    if len(message) > max_length:
        return f"{message[: max_length - 3]}..."
    return message
