"""Tests for the staged-change surface gate."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from corral.hooks import surface_check
from corral.hooks.surface_check import Surface


SURFACES = Path(__file__).parent / "fixtures" / "surfaces.yaml"


def test_load_surfaces_reads_declared_schema() -> None:
    surfaces = surface_check.load_surfaces(SURFACES)

    assert [surface.name for surface in surfaces] == ["payments-config", "orders-api"]
    assert surfaces[0].needs_human is True
    assert surfaces[0].needs_equivalence_check is True
    assert surfaces[1].line_ranges == ["src/api/orders.py:10-20"]
    assert surfaces[1].needs_validation is True
    assert surfaces[1].yaml_block_selectors == []


def test_whole_file_surface_blocks() -> None:
    hits = surface_check.check_surfaces(
        surface_check.load_surfaces(SURFACES),
        ["config/payments.yaml"],
        {"config/payments.yaml": [(1, 1)]},
    )

    assert len(hits) == 1
    assert hits[0].surface.name == "payments-config"
    assert hits[0].blocks is True


def test_line_range_surface_only_matches_overlapping_hunk() -> None:
    surfaces = surface_check.load_surfaces(SURFACES)

    assert surface_check.check_surfaces(
        surfaces,
        ["src/api/orders.py"],
        {"src/api/orders.py": [(15, 16)]},
    )[0].matched_line_ranges == ["src/api/orders.py:10-20"]
    assert (
        surface_check.check_surfaces(
            surfaces,
            ["src/api/orders.py"],
            {"src/api/orders.py": [(30, 31)]},
        )
        == []
    )


def test_missing_hunks_fall_back_to_whole_file_match() -> None:
    hits = surface_check.check_surfaces(
        surface_check.load_surfaces(SURFACES),
        ["src/api/orders.py"],
        {},
    )

    assert len(hits) == 1
    assert hits[0].matched_files == ["src/api/orders.py"]
    assert hits[0].matched_line_ranges == []


@pytest.mark.parametrize(
    ("staged_files", "staged_hunks", "warn_only", "expected_rc", "message"),
    [
        (["config/payments.yaml"], {"config/payments.yaml": [(1, 2)]}, False, 1, "BLOCKED"),
        (
            ["config/payments.yaml"],
            {"config/payments.yaml": [(1, 2)]},
            True,
            0,
            "WARNINGS (--warn-only)",
        ),
        (["src/api/orders.py"], {"src/api/orders.py": [(12, 12)]}, False, 0, "WARNINGS"),
        (["src/other.py"], {"src/other.py": [(1, 1)]}, False, 0, ""),
    ],
)
def test_run_with_synthetic_staged_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    staged_files: list[str],
    staged_hunks: dict[str, list[tuple[int, int]]],
    warn_only: bool,
    expected_rc: int,
    message: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(surface_check, "find_repo_root", lambda: tmp_path)
    monkeypatch.setattr(surface_check, "get_staged_files", lambda: staged_files)
    monkeypatch.setattr(surface_check, "get_staged_hunks", lambda: staged_hunks)

    rc = surface_check.run(warn_only=warn_only, surfaces_path=SURFACES)

    assert rc == expected_rc
    output = capsys.readouterr().out
    if message:
        assert message in output
    else:
        assert output == ""


def test_run_missing_registry_skips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(surface_check, "find_repo_root", lambda: tmp_path)

    assert surface_check.run() == 0
    assert "surfaces.yaml not found" in capsys.readouterr().err


def test_warning_output_includes_validation_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(surface_check, "find_repo_root", lambda: tmp_path)
    monkeypatch.setattr(surface_check, "get_staged_files", lambda: ["src/api/orders.py"])
    monkeypatch.setattr(
        surface_check, "get_staged_hunks", lambda: {"src/api/orders.py": [(12, 12)]}
    )

    assert surface_check.run(surfaces_path=SURFACES) == 0
    output = capsys.readouterr().out
    assert "[WARN] orders-api" in output
    assert "needs_shadow_run" in output
    assert "needs_validation" in output


# ---------------------------------------------------------------------------
# Raw `git diff --cached` output parsing (no real git involved).
# ---------------------------------------------------------------------------


def _fake_git_runner(outputs: dict[tuple[str, ...], str | bytes]):
    def fake_run(cmd: list[str], **_kwargs) -> subprocess.CompletedProcess:
        key = tuple(cmd)
        assert key in outputs, f"unexpected git invocation: {cmd}"
        return subprocess.CompletedProcess(cmd, 0, stdout=outputs[key], stderr=b"")

    return fake_run


def test_get_staged_files_parses_name_only_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _fake_git_runner(
        {("git", "diff", "--cached", "--name-only", "-z"): b"src/a.py\0renamed/b.py\0"}
    )
    monkeypatch.setattr(surface_check.subprocess, "run", runner)

    assert surface_check.get_staged_files() == ["src/a.py", "renamed/b.py"]


def test_non_ascii_staged_path_matches_blocking_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _fake_git_runner(
        {
            ("git", "diff", "--cached", "--name-only", "-z"): "src/åäö.py\0".encode(),
            ("git", "diff", "--cached", "--unified=0"): (
                '+++ "b/src/\\303\\245\\303\\244\\303\\266.py"\n'
                "@@ -0,0 +1 @@\n"
                "+value = 1\n"
            ).encode(),
        }
    )
    monkeypatch.setattr(surface_check.subprocess, "run", runner)
    surface = Surface(
        name="unicode-path",
        description="Non-ASCII path.",
        paths=["src/åäö.py"],
        line_ranges=["src/åäö.py:1-1"],
        needs_human=True,
    )

    hits = surface_check.check_surfaces(
        [surface], surface_check.get_staged_files(), surface_check.get_staged_hunks()
    )

    assert len(hits) == 1
    assert hits[0].matched_line_ranges == ["src/åäö.py:1-1"]


REALISTIC_DIFF = """\
diff --git a/src/api/orders.py b/src/api/orders.py
index 1234567..89abcde 100644
--- a/src/api/orders.py
+++ b/src/api/orders.py
@@ -10,2 +10,3 @@ def validate():
-old line
+new line
+extra line
@@ -40,0 +41,2 @@
+brand new lines
+after line 40
@@ -50,3 +51,0 @@ def trailing():
-removed one
-removed two
-removed three
diff --git a/gone.py b/gone.py
deleted file mode 100644
index abcdef0..0000000
--- a/gone.py
+++ /dev/null
@@ -1,3 +0,0 @@
-a
-b
-c
diff --git a/old_name.py b/renamed/new_name.py
similarity index 90%
rename from old_name.py
rename to renamed/new_name.py
index 1111111..2222222 100644
--- a/old_name.py
+++ b/renamed/new_name.py
@@ -5 +5 @@
-x = 1
+x = 2
diff --git a/fresh.py b/fresh.py
new file mode 100644
index 0000000..3333333
--- /dev/null
+++ b/fresh.py
@@ -0,0 +1,2 @@
+first
+second
"""


def test_get_staged_hunks_parses_real_diff_shapes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _fake_git_runner({("git", "diff", "--cached", "--unified=0"): REALISTIC_DIFF})
    monkeypatch.setattr(surface_check.subprocess, "run", runner)

    hunks = surface_check.get_staged_hunks()

    # Modified file: touched ranges and a pure insertion; deletion-only hunk
    # (+51,0) contributes nothing.
    assert hunks["src/api/orders.py"] == [(10, 12), (41, 42)]
    # Renamed file is tracked under its new path; count-less @@ means one line.
    assert hunks["renamed/new_name.py"] == [(5, 5)]
    # New file: -0,0 +1,2.
    assert hunks["fresh.py"] == [(1, 2)]
    # Deleted file emits +++ /dev/null: no entry, and its hunks must not leak
    # into the previously parsed file.
    assert "gone.py" not in hunks


def test_get_staged_hunks_replaces_non_utf8_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _fake_git_runner(
        {
            ("git", "diff", "--cached", "--unified=0"): (
                b"+++ b/src/binaryish.py\n@@ -0,0 +1 @@\n+\xff\n"
            )
        }
    )
    monkeypatch.setattr(surface_check.subprocess, "run", runner)

    assert surface_check.get_staged_hunks() == {"src/binaryish.py": [(1, 1)]}


def test_run_invalid_surfaces_yaml_is_concise_config_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    registry = tmp_path / "surfaces.yaml"
    registry.write_text("surfaces: [\n", encoding="utf-8")
    monkeypatch.setattr(surface_check, "find_repo_root", lambda: tmp_path)

    assert surface_check.run(surfaces_path=registry) == 2
    error = capsys.readouterr().err
    assert error.startswith("error: invalid surfaces.yaml:")
    assert len(error.splitlines()) == 1
    assert "Traceback" not in error


def test_deleted_file_with_line_ranges_falls_back_to_whole_file() -> None:
    surface = Surface(
        name="gone-surface",
        description="Surface on a deleted file.",
        paths=["gone.py"],
        line_ranges=["gone.py:1-3"],
        needs_human=True,
    )

    hits = surface_check.check_surfaces([surface], ["gone.py"], {})

    assert len(hits) == 1
    assert hits[0].matched_files == ["gone.py"]
    assert hits[0].matched_line_ranges == []
    assert hits[0].blocks is True
