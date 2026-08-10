"""Typed settings derived from the demo configuration."""

from __future__ import annotations

from .config import load_pipeline_config


def minimum_total() -> int:
    config = load_pipeline_config()
    return int(config.get("minimum_total", "25"))


def report_path() -> str:
    config = load_pipeline_config()
    return config.get("report_path", "build/order-summary.txt")
