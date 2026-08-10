"""Deterministic trigger and retrieval-replay tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from corral.governance.budget import token_estimate_tokens
from corral.governance.manifest.model import (
    Budgets,
    Manifest,
    Profile,
    Unit,
)
from corral.governance.replay.corpus import Corpus, CorpusCase, load_corpus
from corral.governance.replay.evaluator import (
    evaluate_corpus,
    validate_rule_loads_against_manifest,
)
from corral.governance.replay.triggers import (
    TriggerRule,
    TriggerRules,
    match_rules,
    path_matches,
)


def manifest() -> Manifest:
    budgets = Budgets(100, 1000, 1000, 1000, 1000, 10, 20, 14)
    return Manifest(
        budgets=budgets,
        profiles={"example": Profile("example", "core", "")},
        units={
            "core": Unit("core", "docs/core.md", "always_loaded", (), None, None),
            "payments": Unit(
                "payments", "docs/topics/payments.md", "trigger_loaded", (), None, None
            ),
            "orders": Unit(
                "orders", "docs/topics/orders.md", "trigger_loaded", (), None, None
            ),
        },
        bundle_ratchets={},
        skills={},
        budget_debt=(),
    )


def rules(*, payment_load: bool = True) -> TriggerRules:
    loads = ("docs/topics/payments.md",) if payment_load else ("docs/core.md",)
    return TriggerRules(
        always_load=("docs/core.md",),
        rules={
            "payments": TriggerRule(
                "payments",
                "payments",
                ("config/payments/**",),
                (r"(?i)payments[- ]config",),
                loads,
            ),
            "orders": TriggerRule(
                "orders",
                "orders",
                ("src/api/orders.py",),
                (r"(?i)orders[- ]api",),
                ("docs/topics/orders.md",),
            ),
        },
    )


def case(**updates) -> CorpusCase:
    values = dict(
        number=1,
        kind="pr",
        title="payments",
        task_text="Change payments-config",
        touched_paths=("config/payments/retries.yaml",),
        expected_loads=("docs/core.md", "docs/topics/payments.md"),
        forbidden_loads=("docs/topics/orders.md",),
        max_bundle_tokens=1000,
        tier="critical",
        notes="",
    )
    values.update(updates)
    return CorpusCase(**values)


def corpus(one_case: CorpusCase) -> Corpus:
    return Corpus("example", "2026-08-10", "synthetic", (one_case,))


@pytest.fixture
def replay_root(tmp_path: Path) -> Path:
    (tmp_path / "docs" / "topics").mkdir(parents=True)
    (tmp_path / "docs" / "core.md").write_text("core instructions")
    (tmp_path / "docs" / "topics" / "payments.md").write_text("payments instructions")
    (tmp_path / "docs" / "topics" / "orders.md").write_text("orders instructions")
    return tmp_path


def test_trigger_glob_matching() -> None:
    cases = [
        ("config/payments/a.yaml", "config/payments/**", True),
        ("config/payments", "config/payments/**", True),
        ("config/sub/a.yaml", "config/*.yaml", False),
        ("src/a.yaml", "*.yaml", True),
        ("src/api/orders.py", "src/api/orders.py", True),
        ("src/api/other.py", "src/api/orders.py", False),
    ]
    for path, glob, expected in cases:
        assert path_matches(path, glob) is expected


def test_trigger_regex_matching() -> None:
    result = match_rules(rules(), ["README.md"], "Update the ORDERS-API contract")
    assert result.fired_rule_ids == {"orders"}
    assert "docs/topics/orders.md" in result.matched_loads


def test_recall_passes(replay_root: Path) -> None:
    result = evaluate_corpus(
        corpus(case()),
        rules(),
        manifest(),
        replay_root,
        token_estimate_tokens,
        topic_prefixes=["docs/topics/"],
        critical_tiers={"critical"},
    )
    assert result.ok
    assert result.overall_recall == 1.0


def test_recall_floor_fails(replay_root: Path) -> None:
    result = evaluate_corpus(
        corpus(case()),
        rules(payment_load=False),
        manifest(),
        replay_root,
        token_estimate_tokens,
        min_overall_recall=0.75,
    )
    assert not result.ok
    assert result.overall_recall == 0.5
    assert any(finding.case_ref == "corpus" for finding in result.fail_findings)


def test_missing_configured_topic_always_fails(replay_root: Path) -> None:
    result = evaluate_corpus(
        corpus(case(tier="routine")),
        rules(payment_load=False),
        manifest(),
        replay_root,
        token_estimate_tokens,
        min_overall_recall=0.0,
        topic_prefixes=["docs/topics/"],
    )
    assert any("missing topic trigger" in finding.message for finding in result.fail_findings)


def test_critical_tier_requires_full_recall(replay_root: Path) -> None:
    result = evaluate_corpus(
        corpus(case()),
        rules(payment_load=False),
        manifest(),
        replay_root,
        token_estimate_tokens,
        min_overall_recall=0.0,
        critical_tiers={"critical"},
    )
    assert any("100% recall" in finding.message for finding in result.fail_findings)


def test_forbidden_load_detection(replay_root: Path) -> None:
    bad = case(
        task_text="Change payments-config and orders-api",
        forbidden_loads=("docs/topics/orders.md",),
    )
    result = evaluate_corpus(
        corpus(bad), rules(), manifest(), replay_root, token_estimate_tokens
    )
    assert any("forbidden load" in finding.message for finding in result.fail_findings)


def test_forbidden_load_detection_cannot_be_evaded_by_respelling(
    replay_root: Path,
) -> None:
    respelled = TriggerRules(
        always_load=("docs/core.md",),
        rules={
            "orders": TriggerRule(
                "orders",
                "orders",
                ("config/payments/**",),
                (),
                ("./docs/topics/../topics/orders.md",),
            )
        },
    )
    bad = case(
        forbidden_loads=("docs/topics/orders.md",),
        expected_loads=("docs/core.md", "./docs/topics/../topics/orders.md"),
    )
    result = evaluate_corpus(
        corpus(bad), respelled, manifest(), replay_root, token_estimate_tokens
    )
    assert any("forbidden load" in finding.message for finding in result.fail_findings)


def test_forbidden_load_detection_catches_symlink_alias(replay_root: Path) -> None:
    (replay_root / "docs" / "topics" / "alias.md").symlink_to("orders.md")
    aliased = TriggerRules(
        always_load=("docs/core.md",),
        rules={
            "orders": TriggerRule(
                "orders",
                "orders",
                ("config/payments/**",),
                (),
                ("docs/topics/alias.md",),
            )
        },
    )
    bad = case(
        forbidden_loads=("docs/topics/orders.md",),
        expected_loads=("docs/core.md", "docs/topics/alias.md"),
    )
    result = evaluate_corpus(
        corpus(bad), aliased, manifest(), replay_root, token_estimate_tokens
    )
    assert any("forbidden load" in finding.message for finding in result.fail_findings)


def test_case_token_ceiling_breach(replay_root: Path) -> None:
    result = evaluate_corpus(
        corpus(case(max_bundle_tokens=1)),
        rules(),
        manifest(),
        replay_root,
        token_estimate_tokens,
    )
    assert any("ceiling 1" in finding.message for finding in result.fail_findings)


def test_configured_tier_ceiling_is_stricter(replay_root: Path) -> None:
    result = evaluate_corpus(
        corpus(case(max_bundle_tokens=1000)),
        rules(),
        manifest(),
        replay_root,
        token_estimate_tokens,
        token_ceilings={"critical": 1},
    )
    assert result.case_results[0].token_ceiling == 1
    assert not result.ok


def test_manifest_topic_consistency_uses_configured_prefixes() -> None:
    bad_rules = TriggerRules(
        (),
        {
            "unknown": TriggerRule(
                "unknown", "unknown", (), ("x",), ("docs/topics/unknown.md",)
            )
        },
    )
    assert validate_rule_loads_against_manifest(
        bad_rules, manifest(), ["docs/topics/"]
    )
    assert validate_rule_loads_against_manifest(bad_rules, manifest(), []) == []


def test_corpus_loader_accepts_neutral_tier(tmp_path: Path) -> None:
    path = tmp_path / "corpus.yaml"
    path.write_text(
        """profile: example
cases:
  - number: 1
    kind: issue
    title: Example
    task_text: Example
    touched_paths: []
    expected_loads: [docs/core.md]
    forbidden_loads: []
    max_bundle_tokens: 10
    tier: elevated
"""
    )
    assert load_corpus(path).cases[0].tier == "elevated"
