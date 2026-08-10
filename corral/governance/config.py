"""Configuration model for instruction governance.

All repository policy is explicit here.  In particular, corral does not
assume private instruction paths, topic prefixes, reviewers, severity tiers,
or allowed/forbidden retrieval pools.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


SCHEMAS_DIR = Path(__file__).resolve().parent / "schemas"
DEFAULT_RULES_SCHEMA = SCHEMAS_DIR / "instruction_rules.schema.json"
DEFAULT_TRIGGER_RULES_SCHEMA = SCHEMAS_DIR / "instruction_trigger_rules.schema.json"
DEFAULT_MANIFEST_SCHEMA = SCHEMAS_DIR / "instruction_manifest.schema.json"

DEFAULT_RULE_ID_PATTERN = r"^(R-[A-Z0-9]+-\d{3,4}|G-\d{4}-\d{3})$"
DEFAULT_MODALITIES = ("MUST", "MUST NOT", "ASK", "READ")
DEFAULT_OPERATIONS = ("sharpen", "add_rule", "add_skill", "demote", "delete")
DEFAULT_TIERS = (
    "executable",
    "core",
    "workflow_prompt",
    "topic_file",
    "gotcha",
    "skill",
    "wiki",
)


@dataclass
class ProposalConfig:
    operations: list[str] = field(default_factory=lambda: list(DEFAULT_OPERATIONS))
    tiers: list[str] = field(default_factory=lambda: list(DEFAULT_TIERS))
    max: int = 3
    # Optional reviewer allow-list. Empty means any non-empty review_by value.
    reviewers: list[str] = field(default_factory=list)


@dataclass
class ReplayConfig:
    manifest: str = "instruction_manifest.yaml"
    trigger_rules: str = "instruction_trigger_rules.yaml"
    corpus: str = "replay_corpus.yaml"
    topic_prefixes: list[str] = field(default_factory=list)
    critical_tiers: list[str] = field(default_factory=list)
    min_recall: float = 0.95
    # First matching path glob supplies the case tier. Empty by default.
    severity_paths: dict[str, str] = field(default_factory=dict)
    default_tier: str = "standard"
    # Builder-only pools. Empty means no paths are invented or forbidden.
    allowed_loads: list[str] = field(default_factory=list)
    forbidden_loads: list[str] = field(default_factory=list)


@dataclass
class BudgetConfig:
    # Tier/kind -> hard token ceiling. Empty preserves manifest/corpus ceilings.
    token_ceilings: dict[str, int] = field(default_factory=dict)


@dataclass
class StalenessConfig:
    """Deterministic instruction-staleness thresholds (all adopter-tunable).

    Defaults preserve the source governance consult values. The two windows
    form a Schmitt trigger: ``demote_days`` must stay >= ``retain_days`` and
    ``demote_rate`` below ``retain_rate`` so the neutral MONITOR band exists.
    """

    #: Fraction of evaluable sessions a rule must apply to over ``retain_days``
    #: to be retained in the always-loaded core.
    retain_rate: float = 0.20
    #: Recent window (days) for the retain decision.
    retain_days: int = 90
    #: Distinct normalized workflow kinds a retained rule must span.
    retain_workflow_count: int = 2
    #: Applicability below this fraction over ``demote_days`` flags demotion.
    demote_rate: float = 0.10
    #: Long window (days) for the demotion decision (>= ``retain_days``).
    demote_days: int = 180
    #: Minimum evaluable sessions before any demotion verdict is eligible.
    min_sessions: int = 30
    #: Glob covering the destination for demoted (non-normative) prose. No
    #: default: demotion proposals are only emitted once adopters configure it.
    demote_target_glob: str | None = None


@dataclass
class GovernanceConfig:
    registry: str = "instruction_rules.yaml"
    # The absolute package path is replaced naturally when validator code is
    # materialized from a BASE archive.
    schema: str = str(DEFAULT_RULES_SCHEMA)
    instruction_globs: list[str] = field(default_factory=list)
    protected_paths: list[str] = field(default_factory=list)
    rule_id_pattern: str = DEFAULT_RULE_ID_PATTERN
    modalities: list[str] = field(default_factory=lambda: list(DEFAULT_MODALITIES))
    proposals: ProposalConfig = field(default_factory=ProposalConfig)
    replay: ReplayConfig = field(default_factory=ReplayConfig)
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    staleness: StalenessConfig = field(default_factory=StalenessConfig)
    #: Human reviewer for governance-generated proposals/reports. No default:
    #: adopters must opt in before proposals carry a review_by value.
    reviewer: str | None = None

    def registry_schema_path(self, root: Path) -> Path:
        path = Path(self.schema)
        return path if path.is_absolute() else root / path


def _section(raw: dict[str, Any], key: str, prefix: str = "governance") -> dict[str, Any]:
    value = raw.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"corral.yaml: {prefix}.{key} must be a mapping")
    return value


def _string(value: Any, key: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"corral.yaml: {key} must be a string")
    return value


def _strings(value: Any, key: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"corral.yaml: {key} must be a list of strings")
    return list(value)


def _string_map(value: Any, key: str) -> dict[str, str]:
    if not isinstance(value, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in value.items()
    ):
        raise ValueError(f"corral.yaml: {key} must be a mapping of string to string")
    return dict(value)


def _positive_int(value: Any, key: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"corral.yaml: {key} must be a positive integer")
    return value


def governance_config_from_mapping(raw: dict[str, Any] | None) -> GovernanceConfig:
    """Apply governance defaults to a parsed ``governance:`` mapping."""
    raw = raw or {}
    if not isinstance(raw, dict):
        raise ValueError("corral.yaml: governance must be a mapping")
    cfg = GovernanceConfig()
    if "registry" in raw:
        cfg.registry = _string(raw["registry"], "governance.registry")
    if "schema" in raw:
        value = raw["schema"]
        cfg.schema = (
            str(DEFAULT_RULES_SCHEMA)
            if value is None
            else _string(value, "governance.schema")
        )
    if "instruction_globs" in raw:
        cfg.instruction_globs = _strings(
            raw["instruction_globs"], "governance.instruction_globs"
        )
    if "protected_paths" in raw:
        cfg.protected_paths = _strings(raw["protected_paths"], "governance.protected_paths")
    if "rule_id_pattern" in raw:
        cfg.rule_id_pattern = _string(raw["rule_id_pattern"], "governance.rule_id_pattern")
    if "modalities" in raw:
        cfg.modalities = _strings(raw["modalities"], "governance.modalities")
        if not cfg.modalities:
            raise ValueError("corral.yaml: governance.modalities must not be empty")

    proposals = _section(raw, "proposals")
    if "operations" in proposals:
        cfg.proposals.operations = _strings(
            proposals["operations"], "governance.proposals.operations"
        )
    if "tiers" in proposals:
        cfg.proposals.tiers = _strings(proposals["tiers"], "governance.proposals.tiers")
    if "max" in proposals:
        cfg.proposals.max = _positive_int(proposals["max"], "governance.proposals.max")
        if cfg.proposals.max > 3:
            raise ValueError(
                "corral.yaml: governance.proposals.max may not exceed the hard cap of 3"
            )
    if "reviewers" in proposals:
        cfg.proposals.reviewers = _strings(
            proposals["reviewers"], "governance.proposals.reviewers"
        )

    replay = _section(raw, "replay")
    for key in ("manifest", "trigger_rules", "corpus", "default_tier"):
        if key in replay:
            setattr(cfg.replay, key, _string(replay[key], f"governance.replay.{key}"))
    for key in ("topic_prefixes", "critical_tiers", "allowed_loads", "forbidden_loads"):
        if key in replay:
            setattr(cfg.replay, key, _strings(replay[key], f"governance.replay.{key}"))
    if "severity_paths" in replay:
        cfg.replay.severity_paths = _string_map(
            replay["severity_paths"], "governance.replay.severity_paths"
        )
    if "min_recall" in replay:
        value = replay["min_recall"]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("corral.yaml: governance.replay.min_recall must be a number")
        cfg.replay.min_recall = float(value)
        if not 0.0 <= cfg.replay.min_recall <= 1.0:
            raise ValueError(
                "corral.yaml: governance.replay.min_recall must be between 0 and 1"
            )

    if "reviewer" in raw:
        if raw["reviewer"] is None:
            cfg.reviewer = None
        else:
            cfg.reviewer = _string(raw["reviewer"], "governance.reviewer")
            if not cfg.reviewer.strip():
                raise ValueError("corral.yaml: governance.reviewer must not be blank")

    staleness = _section(raw, "staleness")
    for key in ("retain_rate", "demote_rate"):
        if key in staleness:
            value = staleness[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"corral.yaml: governance.staleness.{key} must be a number")
            setattr(cfg.staleness, key, float(value))
            if not 0.0 <= getattr(cfg.staleness, key) <= 1.0:
                raise ValueError(
                    f"corral.yaml: governance.staleness.{key} must be between 0 and 1"
                )
    for key in ("retain_days", "retain_workflow_count", "demote_days", "min_sessions"):
        if key in staleness:
            setattr(
                cfg.staleness,
                key,
                _positive_int(staleness[key], f"governance.staleness.{key}"),
            )
    if cfg.staleness.demote_days < cfg.staleness.retain_days:
        raise ValueError(
            "corral.yaml: governance.staleness.demote_days must be >= "
            "governance.staleness.retain_days (the long window is the hysteresis)"
        )
    if cfg.staleness.demote_rate >= cfg.staleness.retain_rate:
        raise ValueError(
            "corral.yaml: governance.staleness.demote_rate must be < "
            "governance.staleness.retain_rate (the two-threshold band)"
        )
    if "demote_target_glob" in staleness:
        if staleness["demote_target_glob"] is None:
            cfg.staleness.demote_target_glob = None
        else:
            cfg.staleness.demote_target_glob = _string(
                staleness["demote_target_glob"], "governance.staleness.demote_target_glob"
            )

    budget = _section(raw, "budget")
    if "token_ceilings" in budget:
        ceilings = budget["token_ceilings"]
        if not isinstance(ceilings, dict) or not all(
            isinstance(k, str)
            and not isinstance(v, bool)
            and isinstance(v, int)
            and v > 0
            for k, v in ceilings.items()
        ):
            raise ValueError(
                "corral.yaml: governance.budget.token_ceilings must map tiers to "
                "positive integers"
            )
        cfg.budget.token_ceilings = dict(ceilings)
    return cfg


def governance_config_from_document(document: dict[str, Any] | None) -> GovernanceConfig:
    document = document or {}
    if not isinstance(document, dict):
        raise ValueError("corral.yaml must contain a mapping at the top level")
    return governance_config_from_mapping(document.get("governance"))
