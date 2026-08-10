"""Tests for the AST magic-number membership lint."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from corral.hooks import magic_numbers


def _write_project(tmp_path: Path, source: str) -> Path:
    constants = tmp_path / "constants.py"
    constants.write_text(
        textwrap.dedent(
            """
            from dataclasses import dataclass

            @dataclass(frozen=True)
            class _Limits:
                timeout_seconds: float = 2.5

            LIMITS = _Limits()
            """
        )
    )
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "worker.py").write_text(textwrap.dedent(source))
    return constants


def _run(tmp_path: Path, constants: Path, **kwargs) -> int:
    return magic_numbers.run(
        root=tmp_path,
        constants_path=constants,
        scan_dirs=["src"],
        **kwargs,
    )


def test_literal_matching_constant_is_violation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    constants = _write_project(tmp_path, "timeout = 2.5\n")

    assert _run(tmp_path, constants) == 1
    output = capsys.readouterr().out
    assert "src/worker.py:1" in output
    assert "Limits.timeout_seconds" in output


def test_named_constant_use_is_clean(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    constants = _write_project(
        tmp_path,
        """
        from constants import LIMITS
        timeout = LIMITS.timeout_seconds
        """,
    )

    assert _run(tmp_path, constants) == 0
    assert "clean" in capsys.readouterr().out


def test_allowlist_exception_suppresses_file_value(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    constants = _write_project(tmp_path, "timeout = 2.5\n")
    allowlist = tmp_path / "allowlist.yaml"
    allowlist.write_text("exceptions:\n  src/worker.py: [2.5]\n")

    assert _run(tmp_path, constants, allowlist_path=allowlist) == 0
    assert "clean" in capsys.readouterr().out


def test_magic_ok_reason_suppresses_literal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    constants = _write_project(
        tmp_path, "timeout = 2.5  # magic-ok: external protocol value\n"
    )

    assert _run(tmp_path, constants) == 0
    assert "clean" in capsys.readouterr().out


def test_negative_literal_does_not_match_positive_constant(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    constants = _write_project(tmp_path, "offset = -2.5\n")

    assert _run(tmp_path, constants) == 0
    assert "clean" in capsys.readouterr().out


def test_no_constants_configuration_skips_with_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)

    assert magic_numbers.run(root=tmp_path) == 0
    assert "constants-membership checks skipped" in capsys.readouterr().out


def test_configured_missing_constants_file_is_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert magic_numbers.run(root=tmp_path, constants_path=Path("missing.py")) == 2
    assert "constants file not found" in capsys.readouterr().err


@pytest.mark.parametrize(
    "contents",
    [
        pytest.param(b"from dataclasses import dataclass\n@dataclass(\n", id="malformed"),
        pytest.param(b"\xff", id="non-utf8"),
    ],
)
def test_invalid_constants_module_is_concise_config_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    contents: bytes,
) -> None:
    constants = tmp_path / "constants.py"
    constants.write_bytes(contents)

    assert magic_numbers.run(root=tmp_path, constants_path=constants) == 2
    error = capsys.readouterr().err
    assert error.startswith("error: invalid constants module:")
    assert len(error.splitlines()) == 1
    assert "Traceback" not in error


def test_scan_dirs_inherit_codemap_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_project(tmp_path, "timeout = 2.5\n")
    config = tmp_path / "corral.yaml"
    config.write_text(
        "codemap:\n"
        "  scan_dirs: [src]\n"
        "hooks:\n"
        "  magic_numbers:\n"
        "    constants: constants.py\n"
    )

    assert magic_numbers.run(config_path=config) == 1
    assert "src/worker.py:1" in capsys.readouterr().out


def test_skip_values_suppress_value_globally(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    constants = _write_project(tmp_path, "timeout = 2.5\n")
    allowlist = tmp_path / "allowlist.yaml"
    allowlist.write_text("skip_values: [2.5]\n")

    assert _run(tmp_path, constants, allowlist_path=allowlist) == 0
    assert "clean" in capsys.readouterr().out


def test_high_frequency_values_are_not_linted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    constants = _write_project(tmp_path, "timeout = 2.5\n")
    for index in range(2):
        (tmp_path / "src" / f"extra{index}.py").write_text("timeout = 2.5\n")

    # Three occurrences still lint under the default threshold.
    assert _run(tmp_path, constants) == 1
    capsys.readouterr()

    allowlist = tmp_path / "allowlist.yaml"
    allowlist.write_text("high_frequency_threshold: 2\n")

    assert _run(tmp_path, constants, allowlist_path=allowlist) == 0
    assert "clean" in capsys.readouterr().out


def test_scoped_constants_lint_only_inside_group_globs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    constants = tmp_path / "constants.py"
    constants.write_text(
        textwrap.dedent(
            """
            from dataclasses import dataclass

            @dataclass(frozen=True)
            class _ScopedLimits:
                retry_backoff: float = 7.5

            SCOPED = _ScopedLimits()
            """
        )
    )
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "worker.py").write_text("backoff = 7.5\n")
    allowlist = tmp_path / "allowlist.yaml"
    allowlist.write_text("scoped_constants:\n  ScopedLimits:\n    - src/special.py\n")

    # Outside the scoped glob the value is not linted.
    assert _run(tmp_path, constants, allowlist_path=allowlist) == 0
    assert "clean" in capsys.readouterr().out

    (source_dir / "special.py").write_text("backoff = 7.5\n")

    # Inside the scoped glob the value is linted; elsewhere still skipped.
    assert _run(tmp_path, constants, allowlist_path=allowlist) == 1
    output = capsys.readouterr().out
    assert "src/special.py:1" in output
    assert "src/worker.py" not in output


def test_constants_file_itself_is_not_linted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    constants = source_dir / "constants.py"
    constants.write_text(
        textwrap.dedent(
            """
            from dataclasses import dataclass

            @dataclass(frozen=True)
            class _Limits:
                timeout_seconds: float = 2.5

            LIMITS = _Limits()
            UNUSED = 2.5
            """
        )
    )
    (source_dir / "worker.py").write_text("timeout = 2.5\n")

    assert magic_numbers.run(root=tmp_path, constants_path=constants, scan_dirs=["src"]) == 1
    output = capsys.readouterr().out
    assert "src/worker.py:1" in output
    assert "src/constants.py" not in output
