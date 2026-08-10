from __future__ import annotations

import json
from pathlib import Path

import pytest

from corral.memory import registry as memory_registry
from corral.retro import registry
from corral.retro.mining import GotchaCandidate
from corral.retro.types import BridgeEvidence, EvidenceGroup, FixupPairContext


def candidate(**overrides) -> GotchaCandidate:
    values = {
        "rule": "Always validate the payload",
        "workflow_kinds": ["fix-issue"],
        "repo_paths": ["src/api.py"],
        "surface_ids": [],
        "source_prs": [11, 12],
        "control_type": "prompt_only",
        "control_path": None,
        "inject_into_briefer": True,
        "confidence": 0.9,
        "rationale": "two fix-ups",
        "severity": "info",
        "created": "2026-08-03",
        "evidence_key": "claude::src/api.py",
        "source_refs": [],
    }
    values.update(overrides)
    return GotchaCandidate(**values)


def group(pairs=((1, 101),)) -> EvidenceGroup:
    contexts = tuple(
        FixupPairContext(original, "a", fixup, "b", 1.0, ("x.py",), "claude", "src")
        for original, fixup in pairs
    )
    return EvidenceGroup("claude::x.py", "claude", "src", contexts)


def test_id_allocation_continues_year_sequence() -> None:
    existing = [{"id": "G-2026-004"}, {"id": "G-2025-099"}, {"id": "G-2026-001"}]
    assert registry.allocate_next_ids(existing, "2026", 2) == ["G-2026-005", "G-2026-006"]
    assert registry.allocate_next_ids([], "2026", 1) == ["G-2026-001"]
    assert registry.allocate_next_ids(existing, "2027", 2) == ["G-2027-001", "G-2027-002"]


def test_default_expiry_is_ninety_days() -> None:
    assert registry.default_expiry("2026-08-03") == "2026-11-01"
    assert registry.default_expiry("2027-12-15") == "2028-03-14"
    assert registry.default_expiry("2028-01-01") == "2028-03-31"


def test_entry_schema_valid() -> None:
    entry = registry.build_gotcha_entry(candidate(), "G-2026-001")
    errors = memory_registry.validate_payload(
        {"gotchas": [entry]}, memory_registry.GOTCHAS_SCHEMA_NAME
    )
    assert errors == []
    assert entry["expires"] == "2026-11-01"
    assert entry["control_pr"] is None


def test_dedup_against_existing_prs_and_open_issue_pairs() -> None:
    existing_prs = registry.existing_source_prs([{"source_prs": [1]}])
    open_pairs = registry.open_issue_pr_pairs(
        [
            {"title": "Gotcha from PR #5 -> #6", "body": ""},
            {"title": "severity candidate", "body": "see #12 and #11"},
        ]
    )
    assert registry.is_duplicate_group(group(pairs=((1, 103),)), existing_prs=existing_prs, open_pairs=set())
    assert registry.is_duplicate_group(group(pairs=((5, 6),)), existing_prs=set(), open_pairs=open_pairs)
    assert registry.is_duplicate_group(group(pairs=((11, 12),)), existing_prs=set(), open_pairs=open_pairs)
    assert not registry.is_duplicate_group(group(pairs=((7, 108),)), existing_prs=existing_prs, open_pairs=open_pairs)


def test_open_issue_pair_parsing_matches_title_precedence_and_body_combinations() -> None:
    pairs = registry.open_issue_pr_pairs(
        [
            {"title": "Gotcha PR #8   ->   #9", "body": "ignored #1 #2"},
            {"title": "severity candidate", "body": "Evidence: #9, #3, and #5"},
        ]
    )
    assert pairs == {(8, 9), (3, 5), (3, 9), (5, 9)}


def test_without_known_bridge_refs_drops_consumed_rows() -> None:
    records = (
        BridgeEvidence("memory:p/a.md", "", "memory", "src", "s", "t"),
        BridgeEvidence("memory:p/b.md", "", "memory", "src", "s", "t"),
    )
    g = EvidenceGroup("k", "claude", "src", (), bridge_evidence=records)
    pruned = registry.without_known_bridge_refs(g, {"memory:p/a.md"})
    assert [r.source_ref for r in pruned.bridge_evidence] == ["memory:p/b.md"]


def test_write_refuses_invalid_payload(tmp_path: Path) -> None:
    target = tmp_path / "gotchas.json"
    bad = {"gotchas": [{"id": "not-a-gotcha-id"}]}
    with pytest.raises(ValueError):
        registry.write_gotchas_file(target, bad)
    assert not target.exists()

    dup = {"gotchas": [registry.build_gotcha_entry(candidate(), "G-2026-001")] * 2}
    errors = registry.validate_gotchas_payload(dup)
    assert any("duplicate" in error for error in errors)

    good = {"gotchas": [registry.build_gotcha_entry(candidate(), "G-2026-001")]}
    registry.write_gotchas_file(target, good)
    assert json.loads(target.read_text())["gotchas"][0]["id"] == "G-2026-001"


def test_load_missing_file_is_empty_registry(tmp_path: Path) -> None:
    assert registry.load_gotchas_file(tmp_path / "absent.json") == {"gotchas": []}
    bad = tmp_path / "bad.json"
    bad.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(ValueError):
        registry.load_gotchas_file(bad)


def test_severity_issue_title_and_body_use_configured_label() -> None:
    cand = candidate(severity="P1", source_prs=[12, 11])
    title = registry.build_severity_issue_title(cand, "G-2026-001", label="agent-gotcha")
    assert title.startswith("[agent-gotcha] P1 candidate G-2026-001 from PR #11 -> #12")
    body = registry.build_severity_issue_body(cand, "G-2026-001")
    assert "#11" in body and "#12" in body
    assert "dnlrshv" not in title and "dnlrshv" not in body
