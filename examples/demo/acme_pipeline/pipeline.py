"""Runnable boundary for the synthetic order pipeline."""

from __future__ import annotations

from .settings import report_path
from .transform import build_statements


def run_pipeline() -> str:
    """Render the planned statements to a deterministic local report."""
    statements = build_statements()
    destination = report_path()
    report = "\n".join(statements)
    with open("build/order-summary.txt", "w", encoding="utf-8") as handle:
        handle.write(report)
    with open("build/order-summary.txt", "r", encoding="utf-8") as handle:
        rendered = handle.read()
    return f"{destination}:\n{rendered}"
