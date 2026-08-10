from __future__ import annotations

import os
from pathlib import Path

import pytest

from corral.retro.bridge import discovery
from corral.retro.bridge.readers import (
    load_bridge_evidence,
    load_memory_corpus,
    load_run_artifacts,
    merge_bridge_groups,
    render_bridge_evidence,
)
from corral.retro.bridge.security import (
    UnsafeBridgeRecordError,
    assert_safe_record,
    sanitize_text,
)
from corral.retro.types import BridgeEvidence, EvidenceGroup

SECRET_KEY = "api_key=sk-ant-api03-ABCDEFGHIJKLMNOPQRST"
GH_TOKEN = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456"


def test_sanitize_text_redacts_common_credential_shapes() -> None:
    text = "\n".join(
        [
            SECRET_KEY,
            f"token: {GH_TOKEN}",
            "Bearer abcdef1234567890abcdef",
            "postgres://user:pass@db.internal/app",
            "-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----",
        ]
    )
    sanitized = sanitize_text(text)
    assert "sk-ant-api03" not in sanitized
    assert GH_TOKEN not in sanitized
    assert "abcdef1234567890" not in sanitized
    assert "user:pass@" not in sanitized
    assert "BEGIN RSA PRIVATE KEY" not in sanitized
    assert "<redacted>" in sanitized


def test_assert_safe_record_fails_closed() -> None:
    unsafe = BridgeEvidence(
        source_ref="memory:p/a.md",
        incident_ref="",
        agent="memory",
        area="src",
        summary="s",
        text=f"leaked {GH_TOKEN} here",
    )
    with pytest.raises(UnsafeBridgeRecordError):
        assert_safe_record(unsafe)


def memory_file(root: Path, name: str, body: str, *, memory_type: str = "feedback") -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\ntype: {memory_type}\nname: {name}\ndescription: test memory\n---\n{body}",
        encoding="utf-8",
    )
    os.utime(path, (1754000000, 1754000000))
    return path


def test_memory_corpus_filters_and_redacts(tmp_path: Path) -> None:
    root = tmp_path / "projects" / "proj" / "memory"
    memory_file(root, "gotcha.md", f"Regression lesson: {SECRET_KEY} caused the outage in scripts/x.py")
    memory_file(root, "plain.md", "Just a project note about unrelated things", memory_type="project")
    memory_file(root, "proj_flavored.md", "The incident yesterday: fixup needed", memory_type="project")

    records = load_memory_corpus([root])
    summaries = {record.source_ref for record in records}
    assert len(records) == 2  # plain project note filtered out
    gotcha = next(r for r in records if "gotcha.md" in r.source_ref)
    assert gotcha.agent == "memory"
    assert "sk-ant-api03" not in gotcha.text
    assert "<redacted>" in gotcha.text
    assert "scripts/x.py" in gotcha.repo_paths
    assert gotcha.area == "scripts"
    assert gotcha.modified is not None
    assert summaries  # silence lint about unused


def test_memory_corpus_fail_closed_on_symlinks(tmp_path: Path) -> None:
    real = tmp_path / "elsewhere"
    real.mkdir()
    memory_file(real, "m.md", "lesson learned")
    linked_root = tmp_path / "corpus"
    linked_root.symlink_to(real)
    assert load_memory_corpus([linked_root]) == []


def test_run_artifacts_require_structure_and_flavor(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    good = runs / "2026-08-01-task"
    good.mkdir(parents=True)
    (good / "final_report.md").write_text(
        "# Report\nThe regression was fixed by a fixup in tests/test_x.py\nincident_ref: INC-42",
        encoding="utf-8",
    )
    unstructured = runs / "loose"
    unstructured.mkdir()
    (unstructured / "notes.md").write_text("gotcha incident", encoding="utf-8")

    records = load_run_artifacts([runs])
    assert len(records) == 1
    record = records[0]
    assert record.agent == "run-audit"
    assert record.incident_ref == "bridge-incident:INC-42"
    assert "tests/test_x.py" in record.repo_paths


def test_bridge_evidence_merges_into_matching_group() -> None:
    record = BridgeEvidence(
        source_ref="memory:p/a.md",
        incident_ref="",
        agent="claude",
        area="src",
        summary="s",
        text="t",
        repo_paths=("src/a.py",),
    )
    from corral.retro.types import FixupPairContext

    existing = EvidenceGroup(
        key="claude::src/a.py",
        agent="claude",
        area="src",
        pairs=(FixupPairContext(1, "a", 2, "b", 1.0, ("src/a.py",), "claude", "src"),),
    )
    merged = merge_bridge_groups([existing], [record])
    assert len(merged) == 1
    assert merged[0].bridge_evidence == (record,)
    rendered = render_bridge_evidence(merged[0].bridge_evidence)
    assert "memory:p/a.md" in rendered

    bridge_only = merge_bridge_groups([], [record])
    assert bridge_only[0].key == "claude::src/a.py"
    assert bridge_only[0].pairs == ()


def test_discovery_skips_absent_roots(tmp_path: Path) -> None:
    present = tmp_path / "corpus"
    present.mkdir()
    roots = discovery.resolve_roots([present, tmp_path / "missing", str(tmp_path / "also-missing")])
    assert roots == [present]
    assert load_bridge_evidence(memory_roots=[tmp_path / "missing"]) == []
