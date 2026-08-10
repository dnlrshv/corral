"""Tests for the provider-neutral telemetry writer (corral.telemetry.writer)."""

from __future__ import annotations

import json
from datetime import timezone, datetime
from pathlib import Path

import pytest

from corral.telemetry import writer


def test_build_telemetry_defaults_and_field_order() -> None:
    record = writer.build_telemetry(
        agent="codex",
        session_id="s1",
        model=None,
        environ={},
        now=datetime(2026, 8, 3, 12, 0, 0, tzinfo=timezone.utc),
    )
    assert tuple(record) == writer.TELEMETRY_FIELDS
    assert record["agent"] == "codex"
    assert record["tokens_available"] is False
    assert record["input_tokens"] is None
    assert record["arm"] == "unknown"
    assert record["preflight_status"] == "unknown"
    assert record["started_at"] == record["ended_at"] == "2026-08-03T12:00:00Z"


def test_build_telemetry_env_metadata_and_tokens() -> None:
    environ = {
        "GITHUB_REPOSITORY": "octo/repo",
        "PR_NUMBER": "12",
        "GITHUB_RUN_ID": "77",
        "AGENT_WORKFLOW_KIND": "merge",
        "MERGE_COMPLEXITY_CLASS": "small",
        "MERGE_COMPLEXITY_REASONS": '["single-file", "tests-only"]',
        "MERGE_COMPLEXITY_BAND": "low",
        "MERGE_COMPLEXITY_TIER": "t1",
    }
    record = writer.build_telemetry(
        agent="claude",
        session_id="s2",
        model="model-x",
        environ=environ,
        input_tokens=5,
        output_tokens=6,
        tokens_available=True,
    )
    assert record["repo"] == "octo/repo"
    assert record["pr_number"] == 12
    assert record["run_id"] == "77"
    assert record["workflow_kind"] == "merge"
    assert record["complexity_class"] == "small"
    assert record["complexity_reasons"] == ["single-file", "tests-only"]
    assert record["band"] == "low"
    assert record["tier"] == "t1"
    assert record["input_tokens"] == 5
    assert record["output_tokens"] == 6


def test_build_telemetry_rejects_unknown_agent() -> None:
    with pytest.raises(ValueError):
        writer.build_telemetry(agent="unknown-agent", session_id="s3", model=None, environ={})


def test_writer_main_round_trip(tmp_path: Path) -> None:
    output = tmp_path / "nested" / "record.json"
    rc = writer.main(
        [
            "--agent",
            "codex",
            "--session-id",
            "abc",
            "--output",
            str(output),
            "--tokens-available",
            "--input-tokens",
            "3",
        ]
    )
    assert rc == 0
    data = json.loads(output.read_text(encoding="utf-8"))
    assert data["session_id"] == "abc"
    assert data["input_tokens"] == 3
    assert data["tokens_available"] is True
