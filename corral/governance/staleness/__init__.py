"""Deterministic quarterly instruction-staleness analysis and reporting."""

from .model import (
    AnalysisResult,
    ExemptionContext,
    Rule,
    RuleVerdict,
    Selectors,
    Session,
    WindowStats,
    analyze,
)
from .report import render_report_markdown
from .sources import StalenessRun, UnknownSurfaceError, run_staleness

__all__ = [
    "AnalysisResult",
    "ExemptionContext",
    "Rule",
    "RuleVerdict",
    "Selectors",
    "Session",
    "StalenessRun",
    "UnknownSurfaceError",
    "WindowStats",
    "analyze",
    "render_report_markdown",
    "run_staleness",
]
