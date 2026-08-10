"""Shared helpers for preflight tests (repo builder + env hygiene)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from corral.codemap.build import build_code_map_with_cache

from .conftest import FIXTURES_DIR

ANTHROPIC_ENV_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")

SURFACES_YAML = """\
surfaces:
  payments-config:
    description: Payment configuration.
    paths:
      - config/payments.yaml
    needs_human: true
    needs_shadow_run: false
    needs_equivalence_check: false
    notes: Ask a maintainer to review.
  demo-queries:
    description: Demo query module.
    paths:
      - demo/queries.py
    needs_human: false
    needs_shadow_run: true
    needs_equivalence_check: false
    notes: Exercise the archive path.
"""


@pytest.fixture(autouse=True)
def clean_preflight_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep LLM auth and CI workflow inference out of every test."""
    for name in (*ANTHROPIC_ENV_VARS, "GITHUB_WORKFLOW"):
        monkeypatch.delenv(name, raising=False)


def build_preflight_repo(tmp_path: Path) -> Path:
    """Build a hermetic repo: demo_pkg under demo/ + surfaces + code map."""
    repo = tmp_path / "repo"
    shutil.copytree(FIXTURES_DIR / "demo_pkg", repo / "demo")
    (repo / "config").mkdir()
    (repo / "config" / "payments.yaml").write_text("threshold: 100\n")
    (repo / "surfaces.yaml").write_text(SURFACES_YAML)
    build_code_map_with_cache(
        repo, repo / "code_map", use_cache=False, scan_dirs=["."], skip_dirs=[]
    )
    return repo


def parse_brief_output(output: str) -> tuple[str, dict]:
    """Split the fingerprint header from the YAML brief body."""
    import yaml

    first_line, _, rest = output.partition("\n")
    assert first_line.startswith("# preflight_fingerprint: ")
    return first_line.split(": ", 1)[1], yaml.safe_load(rest)
