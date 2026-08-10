"""Tolerant JSON extraction from model output.

Model replies often wrap the requested JSON object in prose or markdown
fences; drafting treats a missing payload as a schema-validation failure
(eligible for the one-shot retry), so extraction must be tolerant but never
inventive.
"""

from __future__ import annotations

import json
import re
from typing import Any

_PAYLOAD_RE = re.compile(r"(\{.*\}|\[.*\])", flags=re.DOTALL)


def extract_json_payload(response: str) -> dict[str, Any] | list[Any] | None:
    """Return the JSON payload embedded in *response*, or ``None``.

    Tries the whole string first (the cheap, exact path), then falls back to
    the outermost brace/bracket span.  Invalid JSON yields ``None`` rather
    than raising so callers decide how to report the failure.
    """
    if not response:
        return None

    candidate = response.strip()
    if candidate.startswith("{") or candidate.startswith("["):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

    match = _PAYLOAD_RE.search(response)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


__all__ = ["extract_json_payload"]
