"""Governance configuration and packaged-schema parity tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from corral.config import load_config
from corral.governance.config import (
    DEFAULT_MANIFEST_SCHEMA,
    DEFAULT_RULES_SCHEMA,
    DEFAULT_TRIGGER_RULES_SCHEMA,
)
from corral.governance.registry import check_consistency, parse_registry

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples" / "governance"


def test_governance_defaults_have_no_private_policy(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = load_config().governance
    assert cfg.registry == "instruction_rules.yaml"
    assert cfg.registry_schema_path(tmp_path) == DEFAULT_RULES_SCHEMA
    assert cfg.instruction_globs == []
    assert cfg.protected_paths == []
    assert cfg.replay.topic_prefixes == []
    assert cfg.replay.critical_tiers == []
    assert cfg.replay.severity_paths == {}
    assert cfg.replay.allowed_loads == []
    assert cfg.replay.forbidden_loads == []
    assert cfg.proposals.reviewers == []
    assert cfg.budget.token_ceilings == {}


def test_governance_source_defaults() -> None:
    cfg = load_config().governance
    assert cfg.modalities == ["MUST", "MUST NOT", "ASK", "READ"]
    assert cfg.proposals.max == 3
    assert cfg.replay.min_recall == 0.95
    assert cfg.rule_id_pattern.startswith("^")


def test_governance_config_overrides(tmp_path: Path) -> None:
    path = tmp_path / "corral.yaml"
    path.write_text(
        """
governance:
  registry: policy/rules.yaml
  schema: policy/rules.schema.json
  instruction_globs: [AGENTS.md, docs/instructions/**]
  protected_paths: [corral/governance/**]
  modalities: [MUST, READ]
  proposals:
    operations: [sharpen]
    tiers: [kernel]
    max: 2
    reviewers: [maintainers]
  replay:
    manifest: policy/manifest.yaml
    trigger_rules: policy/triggers.yaml
    corpus: policy/corpus.yaml
    topic_prefixes: [docs/instructions/]
    critical_tiers: [critical]
    min_recall: 0.98
    severity_paths: {config/**: critical}
    default_tier: routine
    allowed_loads: [AGENTS.md]
    forbidden_loads: [docs/instructions/unrelated.md]
  budget:
    token_ceilings: {critical: 1200}
"""
    )
    cfg = load_config(path).governance
    assert cfg.registry == "policy/rules.yaml"
    assert cfg.proposals.tiers == ["kernel"]
    assert cfg.proposals.reviewers == ["maintainers"]
    assert cfg.replay.severity_paths == {"config/**": "critical"}
    assert cfg.budget.token_ceilings == {"critical": 1200}


def test_bad_governance_config_rejected(tmp_path: Path) -> None:
    cases = [
        ("governance: []\n", "governance"),
        ("governance:\n  instruction_globs: AGENTS.md\n", "instruction_globs"),
        ("governance:\n  replay:\n    min_recall: 2\n", "min_recall"),
        ("governance:\n  proposals:\n    max: 0\n", "proposals.max"),
        ("governance:\n  proposals:\n    max: 4\n", "hard cap of 3"),
        (
            "governance:\n  budget:\n    token_ceilings: {critical: -1}\n",
            "token_ceilings",
        ),
    ]
    path = tmp_path / "corral.yaml"
    for body, message in cases:
        path.write_text(body)
        with pytest.raises(ValueError, match=message):
            load_config(path)


@pytest.mark.parametrize(
    ("schema_path", "example_name"),
    [
        (DEFAULT_RULES_SCHEMA, "instruction_rules.example.yaml"),
        (DEFAULT_MANIFEST_SCHEMA, "instruction_manifest.example.yaml"),
        (DEFAULT_TRIGGER_RULES_SCHEMA, "instruction_trigger_rules.example.yaml"),
    ],
)
def test_packaged_schema_validates_example(schema_path: Path, example_name: str) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(schema_path.read_text())
    payload = yaml.safe_load((EXAMPLES / example_name).read_text())
    jsonschema.Draft202012Validator(schema).validate(payload)


def test_schema_ids_point_to_corral() -> None:
    for schema_path in (
        DEFAULT_RULES_SCHEMA,
        DEFAULT_MANIFEST_SCHEMA,
        DEFAULT_TRIGGER_RULES_SCHEMA,
    ):
        schema = json.loads(schema_path.read_text())
        assert schema["$id"].startswith("https://github.com/dnlrshv/corral/")
        assert "trading-bot" not in json.dumps(schema)


def test_rules_example_exercises_every_modality_and_has_live_anchors() -> None:
    rules = parse_registry((EXAMPLES / "instruction_rules.example.yaml").read_text())
    assert {rule["modality"] for rule in rules.values()} == {
        "MUST",
        "MUST NOT",
        "ASK",
        "READ",
    }
    assert check_consistency(
        rules,
        lambda relative: (ROOT / relative).read_text()
        if (ROOT / relative).is_file()
        else None,
    ) == []
