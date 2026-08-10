"""Config-loader helpers for the demo package."""

from __future__ import annotations

from pathlib import Path

import yaml


def load_app_config() -> dict:
    """Load the application config YAML shipped next to this module."""
    path = Path(__file__).resolve().parent / "app.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
