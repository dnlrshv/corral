"""Tests for the coding-agent surface reminder stdin/stdout contract."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from corral.hooks import surface_reminder


SURFACES = Path(__file__).parent / "fixtures" / "surfaces.yaml"


def _set_payload(monkeypatch: pytest.MonkeyPatch, file_path: str) -> None:
    payload = {"tool_name": "Edit", "tool_input": {"file_path": file_path}}
    monkeypatch.setattr(surface_reminder.sys, "stdin", io.StringIO(json.dumps(payload)))


def test_matching_absolute_path_emits_reminder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    _set_payload(monkeypatch, str(tmp_path / "config" / "payments.yaml"))

    assert surface_reminder.run(surfaces_path=SURFACES) == 0
    output = capsys.readouterr().out
    assert "[surface-reminder]" in output
    assert "payments-config" in output
    assert "needs_human" in output
    assert "Ask a maintainer to review." in output


def test_unmatched_path_emits_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    _set_payload(monkeypatch, str(tmp_path / "src" / "other.py"))

    assert surface_reminder.run(surfaces_path=SURFACES) == 0
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("payload", ["not-json", "{}", '{"tool_input": {}}'])
def test_bad_or_incomplete_payload_is_fail_open(
    payload: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(surface_reminder.sys, "stdin", io.StringIO(payload))

    assert surface_reminder.run(surfaces_path=SURFACES) == 0
    assert capsys.readouterr().out == ""


def test_default_registry_comes_from_project_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "policy").mkdir()
    (tmp_path / "policy" / "surfaces.yaml").write_text(SURFACES.read_text())
    (tmp_path / "corral.yaml").write_text("hooks:\n  surfaces: policy/surfaces.yaml\n")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    _set_payload(monkeypatch, str(tmp_path / "src" / "api" / "orders.py"))

    assert surface_reminder.run() == 0
    output = capsys.readouterr().out
    assert "orders-api" in output
    assert "needs_validation" in output
