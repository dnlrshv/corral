"""Tests for the weekly telemetry rollup (corral.telemetry.rollup).

The end-to-end test installs a fake ``gh`` executable on PATH; no network and
no real gh binary are involved.
"""

from __future__ import annotations

import base64
import io
import json
import os
import subprocess
import zipfile
from datetime import timezone, datetime
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from corral.cli import main as cli_main
from corral.telemetry import rollup
from corral.telemetry.rollup_schema import SCHEMA

SESSION = {
    "session_id": "e2e-1",
    "agent": "claude",
    "model": "claude-sonnet-4-5",
    "arm": "b",
    "started_at": "2026-08-03T09:00:00Z",
    "ended_at": "2026-08-03T09:30:00Z",
    "input_tokens": 100,
    "output_tokens": 20,
    "cache_read_tokens": 5,
    "cache_write_tokens": 2,
    "tokens_available": True,
    "tool_call_count": 3,
    "pr_number": 42,
    "run_id": "555",
}


def _make_zip(members: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def test_normalize_row_and_build_table_match_schema() -> None:
    session = {
        "session_id": "s1",
        "agent": "claude",
        "model": "claude-sonnet-4-5",
        "arm": "b",
        "started_at": "2026-08-03T09:00:00Z",
        "ended_at": "2026-08-03T09:30:00Z",
        "input_tokens": 1000,
        "output_tokens": 200,
        "cache_read_tokens": 50,
        "cache_write_tokens": 10,
        "tokens_available": True,
        "tool_call_count": 9,
        "pr_number": "42",
        "complexity_class": "small",
        "complexity_reasons": ["single-file"],
        "band": "low",
        "tier": "t1",
        "run_id": "555",
    }
    pr_info = {
        "merged": True,
        "merged_at": "2026-08-03T11:00:00Z",
        "created_at": "2026-08-03T09:00:00Z",
        "changed_loc": 33,
        "first_head_ci_green": True,
        "ci_fix_iterations": 0,
        "final_ci_green": True,
    }
    artifact = {"id": 7, "name": "agent-telemetry-fix-7"}

    row = rollup.normalize_row(session, pr_info, artifact)
    table = rollup.build_table([row])

    assert table.schema.equals(SCHEMA)
    # Enrichment module not ported: these columns degrade to defaults.
    assert row["area"] == "unknown"
    assert row["issue_type"] == "unknown"
    assert row["changed_loc_quartile"] == "unknown"
    # Preserved mechanism semantics:
    assert row["preflight_status"] == "generated"  # legacy arm "b" inference
    assert row["merged"] is True
    assert row["cycle_time_minutes"] == 120.0
    assert row["changed_loc"] == 33
    assert row["duration_seconds"] == 1800.0
    assert row["tokens_in"] == 1000
    assert row["tokens_out"] == 200
    assert row["workflow_kind"] == "fix"  # derived from the artifact name
    assert row["run_id"] == 555
    assert row["artifact_name"] == "agent-telemetry-fix-7"
    assert row["artifact_id"] == 7
    assert row["pr_number"] == 42


def test_build_table_empty_matches_schema() -> None:
    table = rollup.build_table([])
    assert table.num_rows == 0
    assert table.schema.equals(SCHEMA)


def test_build_table_parquet_round_trip(tmp_path: Path) -> None:
    row = rollup.normalize_row(dict(SESSION), {}, {"id": 3, "name": "agent-telemetry-3"})
    table = rollup.build_table([row])
    output = tmp_path / "rollup.parquet"
    pq.write_table(table, output)
    read_back = pq.read_table(output)
    assert read_back.schema.equals(SCHEMA)
    assert read_back.to_pylist()[0]["session_id"] == "e2e-1"


def test_default_output_path_weekly_naming() -> None:
    assert rollup.default_output_path("out", week="2026-31") == Path(
        "out/rollup_2026-W31.parquet"
    )
    assert rollup.default_output_path("out", week="2026-W05") == Path(
        "out/rollup_2026-W05.parquet"
    )
    now = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    year, week, _ = now.isocalendar()
    assert rollup.default_output_path("out", now=now) == Path(
        f"out/rollup_{year}-W{week:02d}.parquet"
    )


@pytest.mark.parametrize("bad", ["2026-54", "2026-W00", "26-01", "abc"])
def test_parse_week_rejects_invalid(bad: str) -> None:
    with pytest.raises(ValueError):
        rollup.parse_week(bad)


def test_extract_json_sessions_skips_bad_members() -> None:
    zip_bytes = _make_zip(
        {
            "a.json": json.dumps({"session_id": "a"}),
            "sub/b.json": json.dumps({"session_id": "b"}),
            "bad.json": "{not json",
            "list.json": json.dumps([1, 2]),
            "notes.txt": "hi",
        }
    )
    sessions = rollup.extract_json_sessions(zip_bytes)
    assert [session["session_id"] for session in sessions] == ["a", "b"]


def test_extract_json_sessions_bad_zip() -> None:
    assert rollup.extract_json_sessions(b"not a zip") == []


def test_fetch_pr_info_sums_files_across_paginated_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        rollup,
        "_gh_get_json",
        lambda endpoint, jq_filter: {
            "merged": True,
            "merged_at": "2026-08-03T11:00:00Z",
            "created_at": "2026-08-03T09:00:00Z",
        },
    )
    page_one = [
        {"filename": "src/app.py", "additions": 30, "deletions": 3},
        {"filename": "src/lib.py", "additions": 4, "deletions": 1},
    ]
    page_two = [
        {"filename": "tests/test_app.py", "additions": 10, "deletions": 2},
    ]

    def fake_gh_json(args: list[str]) -> subprocess.CompletedProcess[str]:
        assert "--paginate" in args
        # gh applies --jq to each page and concatenates the resulting JSONL.
        stdout = "\n".join(json.dumps(row) for page in (page_one, page_two) for row in page)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=stdout)

    monkeypatch.setattr(rollup, "_gh_json", fake_gh_json)
    monkeypatch.setattr(
        rollup,
        "fetch_ci_outcome_for_pr",
        lambda pr_number, repo, required_contexts: {},
    )

    pr = rollup.fetch_pr_info(42, "octo/repo")

    assert pr["additions"] == 44
    assert pr["deletions"] == 6
    assert pr["changed_loc"] == 50


STUB_TEMPLATE = r'''#!/usr/bin/env python3
"""Fake gh CLI for hermetic rollup tests."""
import base64
import json
import sys
from datetime import datetime, timedelta, timezone

args = sys.argv[1:]
joined = " ".join(args)
zip_bytes = base64.b64decode("__ZIP_B64__")


def out(text):
    sys.stdout.write(text)
    sys.exit(0)


now = datetime.now(timezone.utc)
if joined == "api /repos/octo/repo/actions/artifacts --paginate --jq .artifacts[]":
    created = (now - timedelta(days=1)).isoformat().replace("+00:00", "Z")
    out(json.dumps({"id": 7, "name": "agent-telemetry-fix-7", "created_at": created}) + "\n")
if joined == "api /repos/octo/repo/actions/artifacts/7/zip":
    sys.stdout.buffer.write(zip_bytes)
    sys.exit(0)
if joined == "api /repos/octo/repo/pulls/42 --jq {merged: .merged, merged_at: .merged_at, created_at: .created_at}":
    out(__PR_PAYLOAD_LITERAL__)
if joined == "api --paginate /repos/octo/repo/pulls/42/files --jq .[] | {filename: .filename, additions: .additions, deletions: .deletions}":
    rows = [
        {"filename": "src/app.py", "additions": 30, "deletions": 3},
        {"filename": "tests/test_app.py", "additions": 10, "deletions": 0},
    ]
    out("\n".join(json.dumps(row) for row in rows) + "\n")
if joined == "api --paginate /repos/octo/repo/pulls/42/commits --jq .[].sha":
    out("sha-a\n")
if joined == "api --paginate /repos/octo/repo/commits/sha-a/check-runs --jq .check_runs[] | {name, conclusion, started_at}":
    rows = [
        {"name": "lint", "conclusion": "success", "started_at": "2026-08-03T09:10:00Z"},
        {"name": "test", "conclusion": "success", "started_at": "2026-08-03T09:10:00Z"},
    ]
    out("\n".join(json.dumps(row) for row in rows) + "\n")
sys.exit(1)
'''


def _install_fake_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    zip_bytes = _make_zip({"session.json": json.dumps(SESSION)})
    pr_payload = json.dumps(
        {
            "merged": True,
            "merged_at": "2026-08-03T11:00:00Z",
            "created_at": "2026-08-03T09:00:00Z",
        }
    )
    stub_text = (
        STUB_TEMPLATE.replace("__ZIP_B64__", base64.b64encode(zip_bytes).decode("ascii"))
        .replace("__PR_PAYLOAD_LITERAL__", json.dumps(pr_payload))
    )
    stub = tmp_path / "gh"
    stub.write_text(stub_text, encoding="utf-8")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")


def test_rollup_cli_end_to_end_with_fake_gh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install_fake_gh(tmp_path, monkeypatch)
    monkeypatch.setenv("GITHUB_REPOSITORY", "octo/repo")
    config = tmp_path / "corral.yaml"
    config.write_text(
        "telemetry:\n"
        "  rollup_output_dir: rollouts\n"
        "  required_ci_contexts: [lint, test]\n",
        encoding="utf-8",
    )

    rc = cli_main(["telemetry", "rollup", "--week", "2026-31", "--config", str(config)])

    assert rc == 0
    output = tmp_path / "rollouts" / "rollup_2026-W31.parquet"
    assert output.exists()
    table = pq.read_table(output)
    assert table.schema.equals(SCHEMA)
    rows = table.to_pylist()
    assert len(rows) == 1
    row = rows[0]
    assert row["session_id"] == "e2e-1"
    assert row["agent"] == "claude"
    assert row["merged"] is True
    assert row["pr_number"] == 42
    assert row["cycle_time_minutes"] == 120.0
    assert row["changed_loc"] == 43  # 30+3 + 10+0 from the PR files endpoint
    assert row["tokens_in"] == 100
    assert row["tokens_out"] == 20
    assert row["first_head_ci_green"] is True
    assert row["ci_fix_iterations"] == 0
    assert row["final_ci_green"] is True
    assert row["area"] == "unknown"
    assert row["issue_type"] == "unknown"
    assert row["workflow_kind"] == "fix"  # artifact name agent-telemetry-fix-7
    assert "Wrote rollup" in capsys.readouterr().out


def test_rollup_cli_requires_github_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    config = tmp_path / "corral.yaml"
    config.write_text("telemetry:\n  rollup_output_dir: rollouts\n", encoding="utf-8")

    rc = cli_main(["telemetry", "rollup", "--config", str(config)])

    assert rc == 1
    assert "GITHUB_REPOSITORY" in capsys.readouterr().out


def test_rollup_cli_rejects_bad_week(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install_fake_gh(tmp_path, monkeypatch)
    monkeypatch.setenv("GITHUB_REPOSITORY", "octo/repo")
    config = tmp_path / "corral.yaml"
    config.write_text("telemetry:\n  rollup_output_dir: rollouts\n", encoding="utf-8")

    rc = cli_main(["telemetry", "rollup", "--week", "2026-99", "--config", str(config)])

    assert rc == 1
    assert "week" in capsys.readouterr().out
