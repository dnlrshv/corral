"""End-to-end demo pipeline."""

from __future__ import annotations

from .queries import archive_orders
from .settings import get_threshold


class Reporter:
    """Renders pipeline results into a text report."""

    def render(self) -> str:
        threshold = get_threshold()
        return f"threshold={threshold}"


def run() -> str:
    """Execute the demo pipeline end to end."""
    reporter = Reporter()
    statement = archive_orders()
    with open("out/report.txt", "w", encoding="utf-8") as handle:
        handle.write(reporter.render() + "\n" + statement)
    with open("out/report.txt", "r", encoding="utf-8") as handle:
        return handle.read()
