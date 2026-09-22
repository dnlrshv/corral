"""Explicit repository policy sources; no repository or runner defaults."""
from __future__ import annotations

import json
from pathlib import Path, PurePosixPath


def normalize(value: dict | None) -> dict:
    if value is None:
        value = {}
    if not isinstance(value, dict) or set(value) - {"required_sources", "selected_workflow", "runner"}:
        raise ValueError("invalid repository policy inputs")
    paths = value.get("required_sources", [])
    if not isinstance(paths, (list, tuple)) or any(
        not isinstance(path, str) or not path or PurePosixPath(path).is_absolute()
        or any(part in ("", ".", "..") for part in path.split("/"))
        or any(char in path for char in "?#\\\n\r") for path in paths
    ):
        raise ValueError("policy sources must be relative repository paths")
    selected = value.get("selected_workflow")
    if selected is not None and (not isinstance(selected, str) or selected not in paths):
        raise ValueError("selected workflow must be an explicit required source")
    runner = value.get("runner", {})
    if not isinstance(runner, dict):
        raise TypeError("runner policy must be a dictionary")
    # This records configured policy, not an observation of the installed runner.
    return {"required_sources": sorted(set(paths)), "selected_workflow": selected,
            "runner": json.loads(json.dumps(runner, allow_nan=False))}


def load(path: str | Path | None) -> dict:
    return normalize(json.loads(Path(path).read_text()) if path else None)
