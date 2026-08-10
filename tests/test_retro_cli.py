from __future__ import annotations

import sys
from pathlib import Path

from corral.cli import main


def write_files(tmp_path: Path, verifier_binary: str) -> Path:
    (tmp_path / "seats.yaml").write_text(
        "schema_version: 1\nseats:\n"
        "  draft:\n    provider: a\n    model: m1\n    auth_env: null\n"
        "    adapter: shell-command\n    options:\n"
        f"      argv: [{sys.executable!r}, --version]\n"
        "  verify:\n    provider: b\n    model: m2\n    auth_env: null\n"
        "    adapter: shell-command\n    options:\n"
        f"      argv: [{verifier_binary!r}, --version]\n"
        "  optional:\n    provider: c\n    model: m3\n    auth_env: null\n"
        "    adapter: shell-command\n    options:\n      argv: [definitely-missing-optional]\n"
    )
    config = tmp_path / "corral.yaml"
    config.write_text("seats_file: seats.yaml\nretro:\n  drafter_seat: draft\n  verifier_seats: [verify]\n")
    return config


def test_retro_seats_check_ignores_unavailable_optional_seat(tmp_path: Path, capsys) -> None:
    config = write_files(tmp_path, sys.executable)
    assert main(["retro", "seats", "check", "--config", str(config)]) == 0
    output = capsys.readouterr().out
    assert "optional" in output
    assert "unavailable" in output


def test_retro_seats_check_fails_for_required_unavailable_seat(tmp_path: Path, capsys) -> None:
    config = write_files(tmp_path, "definitely-missing-required")
    assert main(["retro", "seats", "check", "--config", str(config)]) == 1
    assert "verify" in capsys.readouterr().out
