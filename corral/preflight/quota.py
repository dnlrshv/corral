"""Optional quota-snapshot refresh for preflight briefs.

The source implementation read a repo-local quota snapshot through a private
companion module that is not part of corral's reference set, so that
integration is excised. corral instead reads an optional snapshot file
declared by the ``preflight.quota_status_file`` key in ``corral.yaml``:

- key unset, or the file absent -> quota gating is skipped silently;
- file present (YAML or JSON) -> rendered as a compact summary string and
  attached to the brief as ``brief["quota_status"]``.

All failures are swallowed: quota telemetry is cosmetic and must never block
brief generation.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml


def load_quota_snapshot(quota_file: Path | None) -> str | None:
    """Return a compact summary of the quota snapshot, or None to skip."""
    if quota_file is None or not quota_file.is_file():
        return None
    try:
        payload = yaml.safe_load(quota_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return None
    if payload is None:
        return None
    if isinstance(payload, dict):
        if not payload:
            return None
        parts = [f"{key}={value}" for key, value in payload.items()]
        return "Quota: " + ", ".join(parts)
    return f"Quota: {payload}"


def refresh_quota_status(brief: dict[str, Any], quota_file: Path | None) -> None:
    """Refresh dynamic quota telemetry without regenerating the static brief."""
    if quota_summary := load_quota_snapshot(quota_file):
        brief["quota_status"] = quota_summary
    else:
        brief.pop("quota_status", None)


def refresh_cached_quota_status(
    output_path: Path,
    fingerprint: str,
    format_output: Callable[[dict[str, Any], str], str],
    quota_file: Path | None,
) -> None:
    """Re-render a cached brief file so only its quota telemetry changes."""
    try:
        brief = yaml.safe_load(output_path.read_text())
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return
    if not isinstance(brief, dict):
        return
    try:
        refresh_quota_status(brief, quota_file)
        output_text = format_output(brief, fingerprint)
    except Exception:
        return
    try:
        output_path.write_text(output_text)
    except OSError:
        return
