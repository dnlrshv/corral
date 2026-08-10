"""Configured-root discovery for the retrospective bridge.

Corral never guesses a host-specific corpus layout: bridge roots come only
from ``retro.bridge.memory_roots`` / ``retro.bridge.run_artifact_roots``
(both default EMPTY). Missing roots are skipped with notice, matching the
source's "absent paths skip" behavior.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path


def resolve_roots(configured: Sequence[str | Path]) -> list[Path]:
    """Expand and return the configured roots that exist as directories.

    Absent paths are dropped (the caller logs the skip); a bridge is an
    optional evidence source and must never fail the run.
    """
    roots: list[Path] = []
    for entry in configured:
        path = Path(str(entry)).expanduser()
        if path.is_dir():
            roots.append(path)
    return roots


__all__ = ["resolve_roots"]
