"""Small, dependency-free loader for the demo's flat YAML settings."""

from __future__ import annotations

from pathlib import Path


def load_pipeline_config() -> dict[str, str]:
    """Read the top-level scalar settings from ``pipeline.yaml``."""
    path = Path(__file__).resolve().parent.parent / "pipeline.yaml"
    settings: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line or raw_line[0].isspace() or raw_line.lstrip().startswith("#"):
            continue
        key, separator, value = raw_line.partition(":")
        if separator and value.strip():
            settings[key.strip()] = value.strip()
    return settings
