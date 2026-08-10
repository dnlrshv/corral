"""Tests for Stop-hook telemetry capture (corral.telemetry.capture)."""

from __future__ import annotations

import io
import json
import logging
from pathlib import Path

import pytest

from corral.telemetry import capture


def _write_transcript(tmp_path: Path) -> Path:
    entries = [
        {"timestamp": "2026-08-03T09:00:00Z"},
        {
            "timestamp": "2026-08-03T09:00:05Z",
            "message": {
                "model": "claude-sonnet-4-5",
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 40,
                    "cache_read_input_tokens": 7,
                    "cache_creation_input_tokens": 3,
                },
                "content": [
                    {"type": "tool_use", "name": "bash"},
                    {"type": "text", "text": "hello"},
                ],
            },
        },
        {
            "timestamp": "2026-08-03T09:01:00Z",
            "message": {
                "model": "claude-sonnet-4-5",
                "usage": {"input_tokens": 50, "output_tokens": 10},
                "content": [{"type": "tool_use", "name": "edit"}],
            },
        },
    ]
    path = tmp_path / "transcript.jsonl"
    path.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n", encoding="utf-8")
    return path


def test_capture_session_builds_record_from_stop_hook_payload(tmp_path: Path) -> None:
    transcript = _write_transcript(tmp_path)
    spool = tmp_path / "spool"
    payload = {
        "session_id": "sess-123",
        "transcript_path": str(transcript),
        "ended_at": "2026-08-03T09:02:00Z",
        "cwd": str(tmp_path),
    }
    environ = {
        capture.SPOOL_DIR_ENV_VAR: str(spool),
        "PR_NUMBER": "42",
        "GITHUB_REPOSITORY": "octo/repo",
        "AGENT_PREFLIGHT_ARM": "b",
        "AGENT_PREFLIGHT_STATUS": "generated",
        "AGENT_WORKFLOW_KIND": "fix-issue",
        "GITHUB_RUN_ID": "987",
    }

    artifact = capture.capture_session(payload, environ)

    assert artifact == spool / "sess-123.json"
    record = json.loads(artifact.read_text(encoding="utf-8"))
    assert list(record) == list(capture.TELEMETRY_FIELDS)
    assert record["session_id"] == "sess-123"
    assert record["started_at"] == "2026-08-03T09:00:00Z"  # first transcript timestamp
    assert record["ended_at"] == "2026-08-03T09:02:00Z"
    assert record["model"] == "claude-sonnet-4-5"
    assert record["input_tokens"] == 150
    assert record["output_tokens"] == 50
    assert record["cache_read_tokens"] == 7
    assert record["cache_write_tokens"] == 3
    assert record["tokens_available"] is True
    assert record["tool_call_count"] == 2
    assert record["pr_number"] == 42
    assert record["repo"] == "octo/repo"
    assert record["agent"] == "claude"
    assert record["arm"] == "b"
    assert record["preflight_status"] == "generated"
    assert record["fallback_reason"] is None
    assert record["workflow_kind"] == "fix-issue"
    assert record["run_id"] == "987"


def test_capture_payload_usage_beats_transcript(tmp_path: Path) -> None:
    spool = tmp_path / "spool"
    payload = {
        "session_id": "s2",
        "usage": {
            "input_tokens": 11,
            "output_tokens": 22,
            "cache_read_input_tokens": 5,
            "cache_creation_input_tokens": 6,
        },
    }
    record = json.loads(
        capture.capture_session(payload, {capture.SPOOL_DIR_ENV_VAR: str(spool)}).read_text(
            encoding="utf-8"
        )
    )
    assert record["input_tokens"] == 11
    assert record["output_tokens"] == 22
    assert record["cache_read_tokens"] == 5
    assert record["cache_write_tokens"] == 6
    assert record["tokens_available"] is True
    assert record["tool_call_count"] == 0
    assert record["started_at"] == record["ended_at"]


def test_capture_main_reads_stdin_and_exits_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    transcript = _write_transcript(tmp_path)
    payload = {"session_id": "hook-1", "transcript_path": str(transcript)}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setenv(capture.SPOOL_DIR_ENV_VAR, str(tmp_path / "spool"))

    assert capture.main([]) == 0
    assert (tmp_path / "spool" / "hook-1.json").exists()


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("not json", id="invalid-json"),
        pytest.param("[1, 2, 3]", id="non-object-payload"),
    ],
)
def test_capture_main_fail_soft_on_malformed_payload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    raw: str,
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(raw))
    monkeypatch.setenv(capture.SPOOL_DIR_ENV_VAR, str(tmp_path / "spool"))
    caplog.set_level(logging.WARNING, logger=capture.LOGGER.name)

    assert capture.main([]) == 0
    assert not (tmp_path / "spool").exists()
    (record,) = caplog.records
    assert record.levelno == logging.WARNING
    assert record.exc_info is None
    assert "\n" not in record.getMessage()
    assert record.getMessage().startswith("Telemetry capture failed; continuing session:")


def test_capture_main_fail_soft_on_malformed_arguments() -> None:
    assert capture.main(["--spool-dir"]) == 0


def test_capture_main_fail_soft_when_error_logging_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokenLogger:
        def warning(self, message: str) -> None:
            raise OSError("stderr unavailable")

    monkeypatch.setattr(capture, "LOGGER", BrokenLogger())
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))

    assert capture.main([]) == 0


def test_capture_main_fail_soft_on_unreadable_transcript(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payload = {"session_id": "s3", "transcript_path": str(tmp_path)}  # a directory
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setenv(capture.SPOOL_DIR_ENV_VAR, str(tmp_path / "spool"))

    assert capture.main([]) == 0
    assert not (tmp_path / "spool").exists()


def test_capture_main_fail_soft_on_unwritable_spool(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("file", encoding="utf-8")
    monkeypatch.setattr("sys.stdin", io.StringIO('{"session_id": "x"}'))

    assert capture.main(["--spool-dir", str(blocker / "spool")]) == 0
    assert not (blocker / "spool").exists()


def test_capture_main_empty_stdin_still_spools(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    monkeypatch.setenv(capture.SPOOL_DIR_ENV_VAR, str(tmp_path / "spool"))

    assert capture.main([]) == 0
    files = list((tmp_path / "spool").glob("unknown-*.json"))
    assert len(files) == 1
    record = json.loads(files[0].read_text(encoding="utf-8"))
    assert record["session_id"].startswith("unknown-")
    assert record["tokens_available"] is False


def test_default_telemetry_dir_resolution() -> None:
    assert capture.default_telemetry_dir({"XDG_CACHE_HOME": "/tmp/xdg-cache"}) == Path(
        "/tmp/xdg-cache/corral/telemetry"
    )
    assert capture.default_telemetry_dir({capture.SPOOL_DIR_ENV_VAR: "/tmp/spool"}) == Path(
        "/tmp/spool"
    )
    assert capture.default_telemetry_dir({}) == Path.home() / ".cache" / "corral" / "telemetry"


def test_capture_main_configured_spool_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = tmp_path / "corral.yaml"
    config.write_text(f"telemetry:\n  spool_dir: {tmp_path / 'cfg-spool'}\n", encoding="utf-8")
    monkeypatch.delenv(capture.SPOOL_DIR_ENV_VAR, raising=False)
    monkeypatch.setattr("sys.stdin", io.StringIO('{"session_id": "cfg-1"}'))

    assert capture.main(["--config", str(config)]) == 0
    assert (tmp_path / "cfg-spool" / "cfg-1.json").exists()

    # The environment variable still beats the configured value.
    monkeypatch.setenv(capture.SPOOL_DIR_ENV_VAR, str(tmp_path / "env-spool"))
    monkeypatch.setattr("sys.stdin", io.StringIO('{"session_id": "cfg-2"}'))
    assert capture.main(["--config", str(config)]) == 0
    assert (tmp_path / "env-spool" / "cfg-2.json").exists()
