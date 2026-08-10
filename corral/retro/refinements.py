"""Immutable audit records and reverse-diff rendering for retro proposals.

The refinement ledger records an already human-gated instruction proposal's
primary target-file change. It is deliberately not an input to drafting or
application: records are an audit trail, and reverse patches must be reviewed
and applied in a separate follow-up PR by a human.

Records are validated against the refinement-ledger schema shipped in
:mod:`corral.memory`.
"""

from __future__ import annotations

import difflib
import json
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

from corral.memory import registry as memory_registry

_REFINEMENT_ID_PREFIX = "REF-"
_PENDING_HUMAN_REVIEW = "pending_human_review"


class RefinementProposal(Protocol):
    """The proposal shape the ledger needs; the full proposal type lands with
    the instruction-proposal pipeline."""

    target_file: str
    evidence_incidents: Sequence[str]


@dataclass(frozen=True)
class RefinementCapture:
    """Primary target-file snapshots captured when one proposal is accepted."""

    proposal: RefinementProposal
    before_snapshot: str
    after_snapshot: str
    before_exists: bool
    edit_snapshots: list["RefinementEditSnapshot"]


@dataclass(frozen=True)
class RefinementEditSnapshot:
    """One file-level change bundled into a proposal's ledger record."""

    target_path: str
    before_snapshot: str
    after_snapshot: str
    before_exists: bool


@dataclass(frozen=True)
class RefinementRecord:
    """One JSONL audit record for a human-reviewed instruction proposal."""

    id: str
    timestamp: str
    target_path: str
    before_snapshot: str
    after_snapshot: str
    before_exists: bool
    edit_snapshots: list[dict[str, Any]]
    evidence_refs: list[str]
    status: str = _PENDING_HUMAN_REVIEW


def capture_refinement(
    proposal: RefinementProposal,
    *,
    before_snapshot: str,
    after_snapshot: str,
    before_exists: bool,
    edit_snapshots: list[RefinementEditSnapshot],
) -> RefinementCapture:
    """Capture snapshots only; this function never writes or applies an edit."""
    return RefinementCapture(
        proposal=proposal,
        before_snapshot=before_snapshot,
        after_snapshot=after_snapshot,
        before_exists=before_exists,
        edit_snapshots=edit_snapshots,
    )


def materialize_records(
    captures: Sequence[RefinementCapture], *, timestamp: str
) -> list[RefinementRecord]:
    """Give this run's captures stable, reviewable IDs without global allocation."""
    return [
        RefinementRecord(
            id=f"{_REFINEMENT_ID_PREFIX}{timestamp.replace('-', '').replace(':', '')}-{index:03d}",
            timestamp=timestamp,
            target_path=capture.proposal.target_file,
            before_snapshot=capture.before_snapshot,
            after_snapshot=capture.after_snapshot,
            before_exists=capture.before_exists,
            edit_snapshots=[asdict(snapshot) for snapshot in capture.edit_snapshots],
            evidence_refs=list(capture.proposal.evidence_incidents),
        )
        for index, capture in enumerate(captures, start=1)
    ]


def load_records(path: Path) -> list[dict[str, Any]]:
    """Load and schema-validate a JSONL ledger, rejecting malformed rows."""
    if not path.exists():
        return []
    try:
        records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid refinement ledger JSONL: {exc}") from exc
    validate_records(records)
    return records


def validate_records(records: Iterable[dict[str, Any]]) -> None:
    """Raise ValueError unless all records satisfy the shipped JSON Schema."""
    record_list = list(records)
    errors: list[str] = []
    for index, record in enumerate(record_list, start=1):
        errors.extend(
            f"record {index}{message}"
            for message in memory_registry.validate_payload(
                record, memory_registry.REFINEMENTS_SCHEMA_NAME
            )
        )
    ids = [record.get("id") for record in record_list]
    duplicates = sorted({record_id for record_id in ids if ids.count(record_id) > 1})
    if duplicates:
        errors.append(f"duplicate refinement ids: {duplicates}")
    if errors:
        raise ValueError("Invalid refinement ledger:\n" + "\n".join(errors))


def append_records(path: Path, records: Sequence[RefinementRecord]) -> None:
    """Append validated audit records. This writes the ledger only, never a target."""
    if not records:
        return
    existing = load_records(path)
    serialized = [asdict(record) for record in records]
    validate_records([*existing, *serialized])
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as ledger:
        for record in serialized:
            ledger.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def find_record(records: Iterable[dict[str, Any]], refinement_id: str) -> dict[str, Any]:
    """Return exactly one record for a requested ID."""
    matches = [record for record in records if record.get("id") == refinement_id]
    if not matches:
        raise ValueError(f"Refinement id not found: {refinement_id}")
    if len(matches) != 1:
        raise ValueError(f"Refinement id is not unique: {refinement_id}")
    return matches[0]


def render_revert_diff(record: dict[str, Any]) -> str:
    """Render, but never apply, the reverse patch encoded by a ledger record."""
    return "".join(
        line for snapshot in record["edit_snapshots"] for line in _render_reverse_edit(snapshot)
    )


def _render_reverse_edit(snapshot: dict[str, Any]) -> list[str]:
    target = str(snapshot["target_path"])
    before_name = f"b/{target}" if snapshot["before_exists"] else "/dev/null"
    return list(
        difflib.unified_diff(
            str(snapshot["after_snapshot"]).splitlines(keepends=True),
            str(snapshot["before_snapshot"]).splitlines(keepends=True),
            fromfile=f"a/{target}",
            tofile=before_name,
        )
    )


__all__ = [
    "RefinementCapture",
    "RefinementEditSnapshot",
    "RefinementProposal",
    "RefinementRecord",
    "append_records",
    "capture_refinement",
    "find_record",
    "load_records",
    "materialize_records",
    "render_revert_diff",
    "validate_records",
]
