"""Budget caps for gotchas injected into preflight briefs."""

from __future__ import annotations

import json
from typing import Any

MAX_BRIEFER_GOTCHAS = 5
MAX_BRIEFER_GOTCHA_TOKENS = 1200
GOTCHA_TOKEN_CHARS_PER_TOKEN = 4


def estimate_gotcha_tokens(entry: dict[str, Any]) -> int:
    text = json.dumps(entry, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return (len(text) + GOTCHA_TOKEN_CHARS_PER_TOKEN - 1) // GOTCHA_TOKEN_CHARS_PER_TOKEN


def cap_briefer_gotchas(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    total_tokens = 0
    for entry in entries:
        if len(selected) >= MAX_BRIEFER_GOTCHAS:
            break
        tokens = estimate_gotcha_tokens(entry)
        if total_tokens + tokens > MAX_BRIEFER_GOTCHA_TOKENS:
            break
        selected.append(entry)
        total_tokens += tokens
    return selected
