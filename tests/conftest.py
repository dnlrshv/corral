"""Shared fixtures for the corral test suite."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pyarrow.parquet as pq
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

# Make the in-tree corral package importable without an install step.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

#: Loader mapping used wherever tests need reads_config edges.
DEMO_LOADERS = {"load_app_config": "config/app.yaml"}


@pytest.fixture
def demo_pkg(tmp_path: Path) -> Path:
    """Copy demo_pkg into a temp dir so builds are hermetic.

    The copy lives outside any git work tree, so the rglob fallback of
    ``iter_python_files`` is exercised deterministically.
    """
    target = tmp_path / "demo_pkg"
    shutil.copytree(FIXTURES_DIR / "demo_pkg", target)
    return target


def read_parquet_rows(path: Path) -> list[dict]:
    return pq.read_table(path).to_pylist()


def edge_tuples(rows: list[dict]) -> set[tuple[str, str, str, str, str]]:
    """Project edge rows down to (src_kind, src, dst_kind, dst, edge_type)."""
    return {(r["src_kind"], r["src"], r["dst_kind"], r["dst"], r["edge_type"]) for r in rows}
