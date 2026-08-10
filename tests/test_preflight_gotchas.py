"""Gotcha matching + budget tests for the preflight briefer."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from corral.preflight import brief
from corral.preflight.gotcha_budget import (
    MAX_BRIEFER_GOTCHAS,
    MAX_BRIEFER_GOTCHA_TOKENS,
    cap_briefer_gotchas,
    estimate_gotcha_tokens,
)

from .preflight_support import clean_preflight_env

pytestmark = pytest.mark.usefixtures("clean_preflight_env")

FIXTURE_GOTCHAS = Path(__file__).parent / "fixtures" / "gotchas.json"


def load_fixture_gotchas() -> list[dict]:
    return brief.load_agent_gotchas(FIXTURE_GOTCHAS)


def test_load_agent_gotchas_missing_file(tmp_path: Path) -> None:
    assert brief.load_agent_gotchas(tmp_path / "nope.json") == []


def test_load_agent_gotchas_bad_shape(tmp_path: Path) -> None:
    path = tmp_path / "gotchas.json"
    path.write_text("[1, 2, 3]")
    assert brief.load_agent_gotchas(path) == []
    path.write_text(json.dumps({"gotchas": [{"id": "G-2025-001"}, "junk", 5]}))
    assert brief.load_agent_gotchas(path) == [{"id": "G-2025-001"}]


def test_repo_path_glob_matching() -> None:
    gotchas = load_fixture_gotchas()
    matched = brief.filter_briefer_gotchas(gotchas, ["demo/queries.py"])
    assert [entry["id"] for entry in matched] == ["G-2025-001"]

    # Glob match via demo/*.py on a sibling module.
    matched = brief.filter_briefer_gotchas(gotchas, ["demo/pipeline.py"])
    assert [entry["id"] for entry in matched] == ["G-2025-001"]

    # Unrelated path matches nothing.
    assert brief.filter_briefer_gotchas(gotchas, ["other/module.py"]) == []


def test_surface_id_matching() -> None:
    gotchas = load_fixture_gotchas()
    matched = brief.filter_briefer_gotchas(gotchas, [], surface_ids=["payments-config"])
    assert [entry["id"] for entry in matched] == ["G-2025-002"]


def test_workflow_kind_matching_via_env_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_WORKFLOW", "CI: PR Review")
    gotchas = load_fixture_gotchas()

    # Without a configured mapping the env name never filters on kind.
    assert brief.filter_briefer_gotchas(gotchas, []) == []

    # With a mapping, the workflow-kind dimension matches.
    matched = brief.filter_briefer_gotchas(
        gotchas, [], workflow_kinds={"CI: PR Review": "pr-review"}
    )
    assert [entry["id"] for entry in matched] == ["G-2025-005"]


def test_expired_and_non_injected_gotchas_are_excluded() -> None:
    gotchas = load_fixture_gotchas()
    # Everything injectable and non-expired shows up in the general filter.
    ids = [entry["id"] for entry in brief.filter_general_briefer_gotchas(gotchas)]
    assert ids == ["G-2025-001", "G-2025-002", "G-2025-005"]

    # A "today" before the expiry date keeps the expired entry around.
    early = date(2025, 1, 1)
    ids = [entry["id"] for entry in brief.filter_general_briefer_gotchas(gotchas, today=early)]
    assert "G-2025-003" in ids


def test_gotcha_count_cap() -> None:
    entries = [
        {
            "id": f"G-2025-{index:03d}",
            "rule": "x",
            "workflow_kinds": [],
            "repo_paths": ["**/*.py"],
            "surface_ids": [],
            "source_prs": [],
            "control_type": "prompt_only",
            "control_pr": None,
            "control_path": None,
            "inject_into_briefer": True,
            "created": "2025-01-01",
            "expires": None,
        }
        for index in range(MAX_BRIEFER_GOTCHAS + 3)
    ]
    assert len(cap_briefer_gotchas(entries)) == MAX_BRIEFER_GOTCHAS


def test_gotcha_token_budget_cap() -> None:
    # ~4x the token budget in chars -> each single entry exceeds the budget.
    big_rule = "y" * (MAX_BRIEFER_GOTCHA_TOKENS * 4 + 400)
    entries = [
        {"id": f"G-2025-{index:03d}", "rule": big_rule, "inject_into_briefer": True}
        for index in range(3)
    ]
    # Each entry alone blows the token budget, so nothing is selected.
    assert cap_briefer_gotchas(entries) == []
    assert estimate_gotcha_tokens(entries[0]) > MAX_BRIEFER_GOTCHA_TOKENS


def test_select_general_surface_ids_prefers_needs_human() -> None:
    surfaces_yaml = """
surfaces:
  shadowed:
    needs_shadow_run: true
  gated:
    needs_human: true
  equivalence:
    needs_equivalence_check: true
  plain: {}
"""
    assert brief.select_general_surface_ids(surfaces_yaml) == ["gated", "shadowed", "equivalence"]
