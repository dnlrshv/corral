from __future__ import annotations

from datetime import date

import pytest

from corral.retro.mining import (
    build_contexts,
    build_prompt,
    group_evidence,
    is_excluded_from_mining,
    normalize_candidate,
    qualifying_groups,
)
from corral.retro.types import BridgeEvidence, FixupPairContext
from tests.retro_support import fixup_rows


def pair(
    original: int,
    fixup: int,
    files=("src/orders.py",),
    *,
    agent="claude",
    original_title="",
    fixup_title="",
) -> FixupPairContext:
    return FixupPairContext(
        original_pr=original,
        original_author="claude-agent",
        fixup_pr=fixup,
        fixup_author="dev",
        days_between=2.0,
        shared_files=tuple(files),
        agent=agent,
        area="src",
        original_title=original_title,
        fixup_title=fixup_title,
    )


def test_build_contexts_round_trips_rows() -> None:
    contexts = build_contexts([fixup_rows(original_pr=1, fixup_pr=2, shared_files=["a.py"])])
    assert contexts[0].original_pr == 1
    assert contexts[0].fixup_pr == 2
    assert contexts[0].shared_files == ("a.py",)


def test_exclusions_by_title_and_path() -> None:
    assert is_excluded_from_mining(
        pair(1, 2, original_title="Weekly Gotcha Retrospective 2026-W30")
    )
    assert is_excluded_from_mining(pair(1, 2, files=("agent_telemetry/rollup.parquet",)))
    assert is_excluded_from_mining(pair(1, 2, files=("AGENTS.md", "wiki/home.md")))
    assert not is_excluded_from_mining(pair(1, 2, files=("AGENTS.md", "src/orders.py")))
    assert not is_excluded_from_mining(pair(1, 2))
    # patterns are configurable
    assert is_excluded_from_mining(
        pair(1, 2, original_title="housekeeping sweep"),
        ignored_title_patterns=["housekeeping"],
    )


@pytest.mark.parametrize("marker", ["ratchet", "merged-tree", "shrink", "instruction budget"])
def test_instruction_ratchet_collision_carve_out(marker: str) -> None:
    collision = pair(
        1,
        2,
        files=("CLAUDE/rules.md", "AGENTS.md"),
        fixup_title=f"Emergency {marker} repair",
    )
    assert not is_excluded_from_mining(collision)
    assert is_excluded_from_mining(
        pair(1, 2, files=("agent_telemetry/rollup.parquet",), fixup_title=marker)
    )


def test_two_root_floor_counts_distinct_incidents() -> None:
    # one pair + a note on the SAME incident = ONE root incident
    groups = group_evidence([pair(1, 2)], {1: ["we forgot the migration"], 2: ["we forgot the migration"]})
    assert groups[0].evidence_count == 1
    assert qualifying_groups(groups) == []

    # two pairs with distinct originals = two root incidents
    two = group_evidence([pair(1, 2), pair(3, 4)])
    assert two[0].evidence_count == 2
    assert [g.key for g in qualifying_groups(two)] == [two[0].key]


def test_qualifying_sorts_most_evidenced_first() -> None:
    pairs = [pair(1, 2), pair(3, 4), pair(3, 5)]  # second group has 2 pairs
    groups = group_evidence(pairs)
    qualified = qualifying_groups(groups)
    assert qualified[0].evidence_count >= qualified[-1].evidence_count


def test_bridge_only_group_uses_incident_refs_as_roots() -> None:
    record = BridgeEvidence(
        source_ref="memory:proj/notes.md",
        incident_ref="bridge-incident:INC-7",
        agent="memory",
        area="src",
        summary="s",
        text="t",
        repo_paths=("src/orders.py",),
    )
    groups = group_evidence([], None, bridge_evidence=[record])
    assert groups[0].key == "memory::src/orders.py"
    assert groups[0].root_incident_refs == {"bridge-incident:INC-7"}


def test_normalize_candidate_coercions() -> None:
    group = group_evidence([pair(1, 2)])[0]
    candidate = normalize_candidate(
        {
            "rule": "Always X",
            "confidence": 1.7,
            "control_type": "wizardry",
            "severity": "p1",
            "workflow_kinds": [],
        },
        group,
        created_on=date(2026, 8, 10),
    )
    assert candidate.confidence == 1.0
    assert candidate.control_type == "prompt_only"
    assert candidate.severity == "P1"  # canonical configured casing
    assert candidate.workflow_kinds == ["fix-issue"]
    assert candidate.source_prs == [1, 2]
    assert candidate.created == "2026-08-10"

    with pytest.raises(ValueError):
        normalize_candidate({"confidence": 0.5}, group)  # missing rule
    with pytest.raises(ValueError):
        normalize_candidate({"rule": "r", "confidence": "high"}, group)


def test_normalize_severity_keeps_info_lowercase_and_falls_back() -> None:
    group = group_evidence([pair(1, 2)])[0]
    assert normalize_candidate({"rule": "r", "confidence": 0.5, "severity": "INFO"}, group).severity == "info"
    assert normalize_candidate({"rule": "r", "confidence": 0.5, "severity": "P9"}, group).severity == "info"


def test_prompt_carries_excerpts_and_bounds() -> None:
    group = group_evidence([pair(1, 2, original_title="Add orders")])[0]
    prompt = build_prompt(group, {1: "diff one", 2: "diff two"}, severe_severities=["P1"])
    assert "diff one" in prompt and "diff two" in prompt
    assert "#1" in prompt and "#2" in prompt
    assert "P1 means this should file an immediate review issue" in prompt
    assert "Do not invent facts" in prompt
    plain = build_prompt(group, {})
    assert "(unavailable)" in plain
    assert "immediate review issue" not in plain
