"""Trusted completion read and per-invocation usage publication for one dispatch.

Extracted verbatim from the dispatch path so that one generation of a task and a later
continuation generation read their structured completion and publish their native counters
through exactly the same code. Nothing here infers success: the structured completion comes
only from the trusted channel for the dispatch kind, and usage counters keep their declared
provenance and cumulative/delta semantics.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .adapter import ADAPTER_RESULT
from .usage import summarize
from .workspace import safe_path


def is_native(spec: dict) -> bool:
    """A synthetic harness is an offline fixture, not a native provider dispatch."""
    harness = ((spec.get("selection") or {}).get("profile") or {}).get("harness")
    return bool(harness) and harness != "synthetic"


def structured(output, workspace, spec: dict, native_run: bool):
    """Read the structured completion from the trusted channel for this dispatch kind."""
    if native_run:
        path = Path(output) / ADAPTER_RESULT
        if not path.is_file():
            return None, None
        try:
            payload = json.loads(path.read_text())
        except ValueError:
            return None, None
        return payload.get("structured"), payload
    result_file = safe_path(workspace, spec.get("result_file", "result.json"))
    if not result_file.is_file():
        return None, None
    try:
        return json.loads(result_file.read_text()), None
    except ValueError:
        return None, None


def publish_usage(spool, store, *, output, spec: dict, attempt: str, telemetry_errors) -> dict:
    """Flush durable native events, then summarize exactly this invocation's counters."""
    try:
        spool.flush(store)
        publication = "published"
    except (OSError, ConnectionError, sqlite3.Error):
        publication = "spooled"
    usage = summarize(list(spool.store.records("event").values()),
                      list(spool.store.records("conflict").values()),
                      registered=[attempt, "desktop-root"])
    usage["publication"] = publication
    if telemetry_errors:
        usage["unknown"].append({"reason": "native event read/ingest errors",
                                 "classes": sorted(set(telemetry_errors))})
    usage["soft_thresholds"] = [name for name, threshold in spec.get("soft_thresholds", {}).items()
        if usage["observed_fields"].get(name, 0) > threshold]
    usage["mode"] = "observe"
    (Path(output) / "usage.json").write_text(json.dumps(usage, indent=2))
    return usage
