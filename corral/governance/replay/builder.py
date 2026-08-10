"""Build a frozen replay corpus from locally reviewed case metadata.

Unlike the source project, this port has no private merge-exclusion import and
does not derive P0/P1.  Case severity is selected only by the configured
``replay.severity_paths`` path-glob map (or a reviewed case's explicit tier),
with a neutral configurable fallback.
"""

from __future__ import annotations

import datetime as dt
import math
from pathlib import Path
from typing import Any

import yaml

from ..budget import token_estimate_tokens
from ..config import GovernanceConfig
from ..manifest.model import Manifest
from .evaluator import always_bundle_paths
from .triggers import TriggerRules, match_rules, path_matches

HEADROOM_FRACTION = 0.15
HEADROOM_MIN = 250


def normalize_text(text: str) -> str:
    return " ".join(text.split())


def load_reviewed_cases(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Load explicitly reviewed PR/issue metadata; never calls a network CLI."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if isinstance(raw, list):
        cases = raw
        metadata: dict[str, Any] = {}
    elif isinstance(raw, dict) and isinstance(raw.get("cases"), list):
        cases = raw["cases"]
        metadata = {key: value for key, value in raw.items() if key != "cases"}
    else:
        raise ValueError("reviewed-case file must be a list or a mapping with a cases list")
    accepted: list[dict[str, Any]] = []
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            raise ValueError(f"cases[{index}]: reviewed case must be a mapping")
        if case.get("reviewed") is not True:
            raise ValueError(f"cases[{index}]: reviewed must be true")
        for key in ("number", "kind", "title", "touched_paths"):
            if key not in case:
                raise ValueError(f"cases[{index}]: missing required key {key!r}")
        if case["kind"] not in ("pr", "issue"):
            raise ValueError(f"cases[{index}]: kind must be 'pr' or 'issue'")
        if not isinstance(case["touched_paths"], list) or not all(
            isinstance(item, str) for item in case["touched_paths"]
        ):
            raise ValueError(f"cases[{index}]: touched_paths must be a list of strings")
        accepted.append(dict(case))
    if not accepted:
        raise ValueError("reviewed-case file has no cases")
    return accepted, metadata


def derive_tier(
    touched_paths: set[str], severity_paths: dict[str, str], default_tier: str
) -> str:
    """Return the tier of the first configured path-glob that matches."""
    for glob, tier in severity_paths.items():
        if any(path_matches(path, glob) for path in touched_paths):
            return tier
    return default_tier


def effective_bundle_tokens(
    root: Path, always_paths: set[str], matched: set[str]
) -> int:
    total = 0
    for relative in sorted(always_paths | matched):
        target = root / relative
        if target.is_file():
            total += token_estimate_tokens(target.read_text(encoding="utf-8"))
    return total


def build_case(
    reviewed: dict[str, Any],
    root: Path,
    rules: TriggerRules,
    always_paths: set[str],
    config: GovernanceConfig,
) -> dict[str, Any]:
    touched = sorted(set(reviewed["touched_paths"]))
    title = normalize_text(str(reviewed["title"]))
    task_text = normalize_text(str(reviewed.get("task_text", title)))
    match = match_rules(rules, touched, task_text)
    matched = set(match.matched_loads)
    if config.replay.allowed_loads:
        outside = matched - set(config.replay.allowed_loads)
        if outside:
            raise ValueError(
                f"{reviewed['kind']}#{reviewed['number']}: matched loads outside configured "
                f"allowed_loads: {sorted(outside)}"
            )
    expected = sorted(matched)
    forbidden = sorted(
        set(config.replay.forbidden_loads) - matched - set(touched)
    )
    tier = str(
        reviewed.get("tier")
        or derive_tier(
            set(touched), config.replay.severity_paths, config.replay.default_tier
        )
    )
    tokens = effective_bundle_tokens(root, always_paths, matched)
    computed_ceiling = tokens + max(HEADROOM_MIN, math.ceil(tokens * HEADROOM_FRACTION))
    ceiling = config.budget.token_ceilings.get(tier, computed_ceiling)
    result: dict[str, Any] = {
        "number": reviewed["number"],
        "kind": reviewed["kind"],
        "title": title,
        "task_text": task_text,
        "touched_paths": touched,
        "expected_loads": expected,
        "forbidden_loads": forbidden,
        "max_bundle_tokens": ceiling,
        "tier": tier,
        "fired_rules": sorted(match.fired_rule_ids),
    }
    if reviewed.get("notes"):
        result["notes"] = str(reviewed["notes"])
    return result


def build_corpus(
    reviewed_cases: list[dict[str, Any]],
    root: Path,
    manifest: Manifest,
    rules: TriggerRules,
    config: GovernanceConfig,
    *,
    profile: str,
    source_repo: str = "",
    generated_on: str | None = None,
) -> dict[str, Any]:
    always_paths = always_bundle_paths(manifest, profile)
    cases = [build_case(case, root, rules, always_paths, config) for case in reviewed_cases]
    by_rule: dict[str, int] = {}
    by_tier: dict[str, int] = {}
    for case in cases:
        for rule_id in case["fired_rules"]:
            by_rule[rule_id] = by_rule.get(rule_id, 0) + 1
        tier = case["tier"]
        by_tier[tier] = by_tier.get(tier, 0) + 1
    return {
        "profile": profile,
        "generated_on": generated_on or dt.date.today().isoformat(),
        "source_repo": source_repo,
        "stratification": {"by_fired_rule": by_rule, "by_tier": by_tier},
        "cases": cases,
    }


def write_corpus(corpus: dict[str, Any], output: Path) -> None:
    output.write_text(
        yaml.safe_dump(corpus, sort_keys=False, width=100, allow_unicode=True),
        encoding="utf-8",
    )
