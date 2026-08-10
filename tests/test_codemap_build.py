"""Tests for corral.codemap.build over the demo_pkg fixture."""

from __future__ import annotations

from pathlib import Path

import corral.codemap.build as build_module
from corral.codemap.build import (
    CACHE_DIR_NAME,
    OUTPUT_FILENAMES,
    build_code_map,
    build_code_map_with_cache,
    iter_python_files,
)

from .conftest import read_parquet_rows


def test_iter_python_files_finds_fixture_modules(demo_pkg: Path) -> None:
    rel_paths = [p.relative_to(demo_pkg).as_posix() for p in iter_python_files(demo_pkg)]
    assert rel_paths == sorted(
        ["__init__.py", "loaders.py", "pipeline.py", "queries.py", "settings.py"]
    )


def test_build_code_map_symbols(demo_pkg: Path) -> None:
    _, symbols = build_code_map(demo_pkg)
    by_key = {(s.file, s.symbol): s for s in symbols}

    assert by_key[("pipeline.py", "run")].kind == "function"
    assert by_key[("pipeline.py", "run")].is_public
    assert by_key[("pipeline.py", "Reporter")].kind == "class"
    assert by_key[("queries.py", "archive_orders")].kind == "function"
    assert by_key[("settings.py", "get_threshold")].kind == "function"
    # Leading-underscore symbol without __all__ is private.
    assert by_key[("queries.py", "_row_filter")].is_public is False
    # Module-level assignments are not symbols.
    assert ("queries.py", "ORDERS_QUERY") not in by_key


def test_build_code_map_imports(demo_pkg: Path) -> None:
    imports, _ = build_code_map(demo_pkg)
    edges = {
        (i.source_file, i.target_module, i.target_symbol, i.is_relative) for i in imports
    }
    # Cross-module relative imports inside the fixture package.
    assert ("pipeline.py", ".settings", "get_threshold", True) in edges
    assert ("pipeline.py", ".queries", "archive_orders", True) in edges
    assert ("settings.py", ".loaders", "load_app_config", True) in edges
    # Third-party absolute imports are recorded too.
    assert ("loaders.py", "yaml", None, False) in edges
    assert ("loaders.py", "pathlib", "Path", False) in edges


def test_write_code_map_parquet_outputs(demo_pkg: Path, tmp_path: Path) -> None:
    output_dir = tmp_path / "code_map"
    build_code_map_with_cache(demo_pkg, output_dir, use_cache=False)

    for filename in OUTPUT_FILENAMES:
        assert (output_dir / filename).is_file(), filename

    import_rows = read_parquet_rows(output_dir / "imports.parquet")
    assert {
        "source_file": "pipeline.py",
        "target_module": ".settings",
        "target_symbol": "get_threshold",
        "is_relative": True,
        "lineno": 6,
    } in import_rows

    symbol_rows = read_parquet_rows(output_dir / "symbols.parquet")
    assert {
        "file": "pipeline.py",
        "symbol": "Reporter",
        "kind": "class",
        "lineno": 9,
        "is_public": True,
    } in symbol_rows


def test_skip_dirs_filters_default_dirs(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "keep.py").write_text("def keep():\n    return 1\n")
    for skipped in (".venv", "data", "tests/fixtures", ".claude/worktrees"):
        (tmp_path / skipped).mkdir(parents=True)
        (tmp_path / skipped / "drop.py").write_text("def drop():\n    return 2\n")

    default = {p.relative_to(tmp_path).as_posix() for p in iter_python_files(tmp_path)}
    assert default == {"src/keep.py"}

    everything = {
        p.relative_to(tmp_path).as_posix() for p in iter_python_files(tmp_path, skip_dirs=[])
    }
    assert everything == {
        "src/keep.py",
        ".venv/drop.py",
        "data/drop.py",
        "tests/fixtures/drop.py",
        ".claude/worktrees/drop.py",
    }


def test_scan_dirs_restricts_scope(tmp_path: Path) -> None:
    for sub in ("a", "b"):
        (tmp_path / sub).mkdir()
        (tmp_path / sub / "mod.py").write_text("def f():\n    return None\n")

    scoped = {
        p.relative_to(tmp_path).as_posix() for p in iter_python_files(tmp_path, scan_dirs=["a"])
    }
    assert scoped == {"a/mod.py"}


def test_unparsable_file_is_skipped(tmp_path: Path, capsys) -> None:
    (tmp_path / "broken.py").write_text("def broken(:\n")
    (tmp_path / "fine.py").write_text("def fine():\n    return 1\n")

    imports, symbols = build_code_map(tmp_path)
    assert {s.file for s in symbols} == {"fine.py"}
    assert imports == []
    assert "broken.py" in capsys.readouterr().err


def test_scan_and_skip_dirs_accept_generators(tmp_path: Path) -> None:
    # A generator can only be consumed once; both filters must see every
    # entry for every file.
    for sub in ("a", "b"):
        (tmp_path / sub).mkdir()
        (tmp_path / sub / "mod.py").write_text("def f():\n    return None\n")
    (tmp_path / "junk").mkdir()
    (tmp_path / "junk" / "mod.py").write_text("def f():\n    return None\n")

    files = {
        p.relative_to(tmp_path).as_posix()
        for p in iter_python_files(
            tmp_path,
            scan_dirs=(d for d in ("a", "b")),
            skip_dirs=(d for d in ("junk",)),
        )
    }
    assert files == {"a/mod.py", "b/mod.py"}


# ---------------------------------------------------------------------------
# Cache behaviour (per tree-sha AND per scan scope)
# ---------------------------------------------------------------------------


def _clean_git_tree(root: Path, monkeypatch, sha: str = "fixedtreesha") -> None:
    """Pretend *root* is a clean git tree with the given tree sha."""
    monkeypatch.setattr(build_module, "_git_tree_sha", lambda _: sha)
    monkeypatch.setattr(build_module, "_git_tree_is_clean", lambda _: True)


def _counting_build(monkeypatch) -> list[dict]:
    """Wrap build_code_map so tests can count actual rebuilds."""
    calls: list[dict] = []
    real_build = build_module.build_code_map

    def counting(*args, **kwargs):
        calls.append(kwargs)
        return real_build(*args, **kwargs)

    monkeypatch.setattr(build_module, "build_code_map", counting)
    return calls


def _symbol_files(output_dir: Path) -> set[str]:
    return {row["file"] for row in read_parquet_rows(output_dir / "symbols.parquet")}


def test_cache_same_tree_same_scope_hits_cache(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "src" / "mod.py").write_text("def f():\n    return 1\n")
    output_dir = tmp_path / "code_map"

    _clean_git_tree(root, monkeypatch)
    calls = _counting_build(monkeypatch)

    build_code_map_with_cache(root, output_dir, scan_dirs=["src"], skip_dirs=[])
    assert len(calls) == 1

    # Same tree + same scope: served from the cache, no rebuild.
    build_code_map_with_cache(root, output_dir, scan_dirs=["src"], skip_dirs=[])
    assert len(calls) == 1
    assert [e.name for e in (output_dir / CACHE_DIR_NAME).iterdir()] != []
    assert _symbol_files(output_dir) == {"src/mod.py"}


def test_cache_changed_scope_rebuilds(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "proj"
    for sub in ("a", "b"):
        (root / sub).mkdir(parents=True)
        (root / sub / "mod.py").write_text(f"def {sub}():\n    return 1\n")
    output_dir = tmp_path / "code_map"

    _clean_git_tree(root, monkeypatch)
    calls = _counting_build(monkeypatch)

    build_code_map_with_cache(root, output_dir, scan_dirs=["a"], skip_dirs=[])
    assert len(calls) == 1
    assert _symbol_files(output_dir) == {"a/mod.py"}

    # A different scope on the same tree must rebuild, not reuse scope A's
    # cache entry.
    build_code_map_with_cache(root, output_dir, scan_dirs=["a", "b"], skip_dirs=[])
    assert len(calls) == 2
    assert _symbol_files(output_dir) == {"a/mod.py", "b/mod.py"}

    # Both scopes keep their own cache entries; switching back is a hit.
    entries = sorted(e.name for e in (output_dir / CACHE_DIR_NAME).iterdir())
    assert len(entries) == 2

    build_code_map_with_cache(root, output_dir, scan_dirs=["a"], skip_dirs=[])
    assert len(calls) == 2
    assert _symbol_files(output_dir) == {"a/mod.py"}

    # A changed skip_dirs is a scope change too.
    build_code_map_with_cache(root, output_dir, scan_dirs=["a", "b"], skip_dirs=["b"])
    assert len(calls) == 3
    assert _symbol_files(output_dir) == {"a/mod.py"}
