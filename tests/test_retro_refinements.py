from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from corral.retro.refinements import (
    RefinementEditSnapshot,
    append_records,
    capture_refinement,
    find_record,
    load_records,
    materialize_records,
    render_revert_diff,
)
from corral.retro.revert import render_revert_patch

TIMESTAMP = "2026-08-03T12:00:00Z"


@dataclass(frozen=True)
class FakeProposal:
    target_file: str = "docs/guide.md"
    evidence_incidents: tuple[str, ...] = ("pr:11", "pr:12")


def make_records(count: int = 1):
    captures = [
        capture_refinement(
            FakeProposal(),
            before_snapshot="old line\n",
            after_snapshot="new line\n",
            before_exists=True,
            edit_snapshots=[
                RefinementEditSnapshot("docs/guide.md", "old line\n", "new line\n", True)
            ],
        )
        for _ in range(count)
    ]
    return materialize_records(captures, timestamp=TIMESTAMP)


def test_materialize_and_append_roundtrip(tmp_path: Path) -> None:
    ledger = tmp_path / "refinements.jsonl"
    records = make_records()
    assert records[0].id == "REF-20260803T120000Z-001"
    append_records(ledger, records)
    loaded = load_records(ledger)
    assert len(loaded) == 1
    assert loaded[0]["target_path"] == "docs/guide.md"
    assert loaded[0]["status"] == "pending_human_review"


def test_append_rejects_schema_invalid_and_duplicate_ids(tmp_path: Path) -> None:
    ledger = tmp_path / "refinements.jsonl"
    append_records(ledger, make_records())
    with pytest.raises(ValueError):
        append_records(ledger, make_records())  # duplicate REF id

    bad = make_records()[0]
    from dataclasses import replace

    invalid = replace(bad, id="REF-20260803T120000Z-002", evidence_refs=["only-one"])
    with pytest.raises(ValueError):
        append_records(ledger, [invalid])  # evidence_refs minItems=2


def test_load_records_rejects_malformed_jsonl(tmp_path: Path) -> None:
    ledger = tmp_path / "refinements.jsonl"
    ledger.write_text("{not json}\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_records(ledger)
    assert load_records(tmp_path / "absent.jsonl") == []


def test_find_record_uniqueness() -> None:
    rows = [
        {"id": "REF-20260803T120000Z-001"},
        {"id": "REF-20260803T120000Z-001"},
    ]
    with pytest.raises(ValueError, match="not unique"):
        find_record(rows, "REF-20260803T120000Z-001")
    with pytest.raises(ValueError, match="not found"):
        find_record(rows, "REF-20260803T120000Z-999")


def test_render_revert_diff_shape(tmp_path: Path) -> None:
    ledger = tmp_path / "refinements.jsonl"
    append_records(ledger, make_records())
    patch = render_revert_patch(ledger, "REF-20260803T120000Z-001")
    assert patch.startswith("--- a/docs/guide.md")
    assert "+++ b/docs/guide.md" in patch
    assert "-new line" in patch and "+old line" in patch

    record = find_record(load_records(ledger), "REF-20260803T120000Z-001")
    assert render_revert_diff(record) == patch

    # creation (before missing) renders /dev/null as the target side
    creation = capture_refinement(
        FakeProposal(),
        before_snapshot="",
        after_snapshot="brand new\n",
        before_exists=False,
        edit_snapshots=[RefinementEditSnapshot("docs/guide.md", "", "brand new\n", False)],
    )
    creation_record = materialize_records([creation], timestamp=TIMESTAMP)[0]
    creation_patch = render_revert_diff(
        {"edit_snapshots": creation_record.edit_snapshots}
    )
    assert "+++ /dev/null" in creation_patch
