"""Corpus-builder excision and per-tier budget tests."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from corral.governance.budget import lint_manifest, token_estimate_tokens
from corral.governance.config import GovernanceConfig
from corral.governance.manifest.model import Budgets, Manifest, Profile, Unit
from corral.governance.replay.builder import (
    build_case,
    derive_tier,
    load_reviewed_cases,
)
from corral.governance.replay.evaluator import always_bundle_paths
from corral.governance.replay.triggers import TriggerRule, TriggerRules


def fixture_manifest() -> Manifest:
    return Manifest(
        budgets=Budgets(1000, 1000, 1000, 1000, 1000, 10, 20, 14),
        profiles={"example": Profile("example", "core", "")},
        units={
            "core": Unit("core", "docs/core.md", "always_loaded", (), None, None),
            "payments": Unit(
                "payments", "docs/payments.md", "trigger_loaded", (), None, None
            ),
        },
        bundle_ratchets={},
        skills={},
        budget_debt=(),
    )


def fixture_rules() -> TriggerRules:
    return TriggerRules(
        ("docs/core.md",),
        {
            "payments": TriggerRule(
                "payments",
                "payments",
                ("config/**",),
                (),
                ("docs/payments.md",),
            )
        },
    )


@pytest.fixture
def builder_root(tmp_path: Path) -> Path:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "core.md").write_text("core instructions")
    (tmp_path / "docs" / "payments.md").write_text("payments")
    (tmp_path / "docs" / "orders.md").write_text("orders")
    return tmp_path


def test_severity_map_applies_first_matching_glob() -> None:
    severity = {"config/payments/**": "critical", "config/**": "elevated"}
    assert derive_tier({"config/payments/retry.yaml"}, severity, "standard") == "critical"
    assert derive_tier({"config/orders.yaml"}, severity, "standard") == "elevated"
    assert derive_tier({"README.md"}, severity, "standard") == "standard"


def test_reviewed_case_loading_preserves_metadata(tmp_path: Path) -> None:
    path = tmp_path / "reviewed.yaml"
    path.write_text(
        """profile: example
source_repo: synthetic/example
cases:
  - number: 8
    kind: pr
    title: Example
    touched_paths: [config/payments/retry.yaml]
    reviewed: true
"""
    )
    cases, metadata = load_reviewed_cases(path)
    assert cases[0]["number"] == 8
    assert metadata == {"profile": "example", "source_repo": "synthetic/example"}


def test_unreviewed_case_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "reviewed.yaml"
    path.write_text(
        "- {number: 8, kind: pr, title: Example, touched_paths: [], reviewed: false}\n"
    )
    with pytest.raises(ValueError, match="reviewed must be true"):
        load_reviewed_cases(path)


def test_builder_has_no_default_forbidden_pool(builder_root: Path) -> None:
    cfg = GovernanceConfig()
    case = build_case(
        {
            "number": 8,
            "kind": "pr",
            "title": "Payments",
            "touched_paths": ["config/payments/retry.yaml"],
        },
        builder_root,
        fixture_rules(),
        always_bundle_paths(fixture_manifest(), "example"),
        cfg,
    )
    assert case["tier"] == "standard"
    assert case["forbidden_loads"] == []


def test_builder_uses_configured_pools_severity_and_ceiling(builder_root: Path) -> None:
    cfg = GovernanceConfig()
    cfg.replay.allowed_loads = ["docs/core.md", "docs/payments.md"]
    cfg.replay.forbidden_loads = ["docs/payments.md", "docs/orders.md"]
    cfg.replay.severity_paths = {"config/**": "critical"}
    cfg.budget.token_ceilings = {"critical": 25}
    built = build_case(
        {
            "number": 9,
            "kind": "issue",
            "title": "Payments",
            "touched_paths": ["config/payments/retry.yaml"],
        },
        builder_root,
        fixture_rules(),
        {"docs/core.md"},
        cfg,
    )
    assert built["tier"] == "critical"
    assert built["max_bundle_tokens"] == 25
    assert built["forbidden_loads"] == ["docs/orders.md"]


def test_budget_linter_enforces_configured_ceiling_per_unit_tier(
    builder_root: Path,
) -> None:
    findings = lint_manifest(
        fixture_manifest(),
        builder_root,
        as_of=date(2026, 8, 10),
        token_ceilings={"always_loaded": 1, "trigger_loaded": 100},
    )
    assert any(
        finding.severity == "FAIL" and "'always_loaded' tier ceiling 1" in finding.message
        for finding in findings
    )
    assert not any("'trigger_loaded' tier ceiling" in finding.message for finding in findings)
    assert [token_estimate_tokens(text) for text in ("", "a", "abcde")] == [0, 1, 2]
