"""Render a reviewable reverse patch for one retrospective refinement record.

This helper intentionally has no apply option. Save or pipe its output into a
separate, human-reviewed follow-up PR workflow.
"""

from __future__ import annotations

from pathlib import Path

from corral.retro.refinements import find_record, load_records, render_revert_diff


def render_revert_patch(ledger_path: Path, refinement_id: str) -> str:
    """Return the unified reverse diff for ``refinement_id`` from the ledger."""
    record = find_record(load_records(ledger_path), refinement_id)
    return render_revert_diff(record)


__all__ = ["render_revert_patch"]
