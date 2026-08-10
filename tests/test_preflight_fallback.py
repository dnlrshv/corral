"""End-to-end deterministic-fallback tests for `corral preflight`.

These run without any LLM auth (env cleared) and without the optional
`anthropic`/`jsonschema` extras, so they exercise the fail-soft path that
must never require the network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from corral.cli import main
from corral.preflight import brief as preflight_brief

from .preflight_support import build_preflight_repo, clean_preflight_env, parse_brief_output

pytestmark = pytest.mark.usefixtures("clean_preflight_env")


def test_fallback_brief_end_to_end_over_code_map(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = build_preflight_repo(tmp_path)

    rc = main(
        [
            "preflight",
            "--root",
            str(repo),
            "--task",
            "Fix the archive logic in demo/queries.py and double-check config/payments.yaml",
        ]
    )

    assert rc == 0
    captured = capsys.readouterr()
    fingerprint, brief = parse_brief_output(captured.out)
    assert len(fingerprint) == 12
    assert brief["preflight_status"] == "fallback"
    assert brief["fallback_reason"] == "preflight_llm_unavailable"
    assert brief["preflight_error"]
    # Path mentions from the task drive surface scoping (both mentioned
    # files belong to declared surfaces, in registry order).
    assert brief["surfaces_in_scope"] == ["payments-config", "demo-queries"]
    assert brief["files_to_read_only"] == ["demo/queries.py", "config/payments.yaml"]
    # ...while do_not_touch always comes from needs_human surfaces.
    assert brief["do_not_touch"] == ["config/payments.yaml"]
    assert brief["agent_gotchas"] == []
    assert brief["estimated_blast_radius"] == "medium"
    assert captured.err == (
        "::notice::Preflight LLM authentication is not configured; "
        "using deterministic fallback.\n"
    )
    assert "TypeError" not in captured.err


def test_fallback_brief_injects_matching_gotchas(tmp_path: Path, capsys) -> None:
    repo = build_preflight_repo(tmp_path)
    (repo / "agent_memory").mkdir()
    gotchas = json.loads((Path(__file__).parent / "fixtures" / "gotchas.json").read_text())
    (repo / "agent_memory" / "gotchas.json").write_text(json.dumps(gotchas))

    rc = main(
        ["preflight", "--root", str(repo), "--task", "Rework demo/queries.py archiving"]
    )

    assert rc == 0
    _, brief = parse_brief_output(capsys.readouterr().out)
    ids = [entry["id"] for entry in brief["agent_gotchas"]]
    # Matched via the demo/*.py glob; expired and non-injected entries excluded.
    assert ids == ["G-2025-001"]


def test_fallback_brief_writes_output_file_and_reuses_cache(tmp_path: Path) -> None:
    repo = build_preflight_repo(tmp_path)
    output = tmp_path / "brief.yaml"

    rc = main(
        ["preflight", "--root", str(repo), "--task", "Inspect demo/queries.py", "--output", str(output)]
    )
    assert rc == 0
    first_text = output.read_text()
    fingerprint, brief = parse_brief_output(first_text)
    assert brief["preflight_status"] == "fallback"

    # Same task + same tree state -> cached fingerprint hit: the file is
    # refreshed in place (no quota file configured, so content is unchanged).
    rc = main(
        ["preflight", "--root", str(repo), "--task", "Inspect demo/queries.py", "--output", str(output)]
    )
    assert rc == 0
    assert parse_brief_output(output.read_text())[0] == fingerprint


def test_quota_status_file_is_optional(tmp_path: Path, capsys) -> None:
    repo = build_preflight_repo(tmp_path)
    (repo / "quota_snapshot.yaml").write_text("remaining_calls: 42\nreset: daily\n")
    (repo / "corral.yaml").write_text(
        "preflight:\n  quota_status_file: quota_snapshot.yaml\n"
    )

    rc = main(
        [
            "preflight",
            "--root",
            str(repo),
            "--config",
            str(repo / "corral.yaml"),
            "--task",
            "Inspect demo/queries.py",
        ]
    )

    assert rc == 0
    _, brief = parse_brief_output(capsys.readouterr().out)
    assert brief["quota_status"] == "Quota: remaining_calls=42, reset=daily"


def test_general_brief_via_auto_without_matching_branch(tmp_path: Path, capsys) -> None:
    repo = build_preflight_repo(tmp_path)

    rc = main(["preflight", "--root", str(repo), "--auto"])

    assert rc == 0
    _, brief = parse_brief_output(capsys.readouterr().out)
    assert brief["preflight_status"] == "general"
    # needs_human surfaces come first in the general selection.
    assert brief["surfaces_in_scope"] == ["payments-config", "demo-queries"]
    assert brief["do_not_touch"] == ["config/payments.yaml"]


def test_current_branch_uses_selected_repo_root(tmp_path: Path, monkeypatch) -> None:
    calls: list[tuple[list[str], dict]] = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return type("Result", (), {"stdout": "feat/123-example\n"})()

    monkeypatch.setattr(preflight_brief.subprocess, "run", fake_run)

    assert preflight_brief.get_current_branch(tmp_path) == "feat/123-example"
    assert calls[0][1]["cwd"] == tmp_path


def test_strict_raises_instead_of_falling_back(tmp_path: Path) -> None:
    repo = build_preflight_repo(tmp_path)

    with pytest.raises(Exception) as excinfo:
        main(
            ["preflight", "--root", str(repo), "--task", "Inspect demo/queries.py", "--strict"]
        )
    # The original LLM-path failure propagates untouched.
    assert "strict" not in str(excinfo.value).lower()


def test_deprecated_fallback_on_error_warns_but_succeeds(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = build_preflight_repo(tmp_path)

    rc = main(
        [
            "preflight",
            "--root",
            str(repo),
            "--task",
            "Inspect demo/queries.py",
            "--fallback-on-error",
        ]
    )

    assert rc == 0
    assert "--fallback-on-error is deprecated" in capsys.readouterr().err


def test_missing_surfaces_registry_is_a_clean_error(tmp_path: Path, capsys) -> None:
    repo = build_preflight_repo(tmp_path)
    (repo / "surfaces.yaml").unlink()

    rc = main(["preflight", "--root", str(repo), "--task", "Inspect demo/queries.py"])

    assert rc == 1
    assert "cannot read surfaces registry" in capsys.readouterr().err


def test_output_parent_is_not_created_implicitly(tmp_path: Path) -> None:
    repo = build_preflight_repo(tmp_path)
    output = tmp_path / "missing" / "brief.yaml"

    with pytest.raises(FileNotFoundError):
        main(
            [
                "preflight",
                "--root",
                str(repo),
                "--task",
                "Inspect demo/queries.py",
                "--output",
                str(output),
            ]
        )

    assert not output.parent.exists()
