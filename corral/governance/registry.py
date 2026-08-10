"""Instruction-rule registry parsing, validation, and anchor consistency."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import yaml

from .config import GovernanceConfig

SELECTOR_KINDS = ("paths", "workflows", "surfaces")
CONCERN_KEY_RE = re.compile(r"^[a-z][a-z0-9-]*$")


@dataclass(frozen=True)
class Finding:
    severity: str
    check: str
    message: str


def validate_registry_structure(raw: Any, config: GovernanceConfig | None = None) -> list[str]:
    """Pure-Python structural check matching the packaged registry schema.

    The gate intentionally does not need the optional ``jsonschema`` package;
    its rule-id and modality vocabularies are supplied by base-ref config.
    """
    cfg = config or GovernanceConfig()
    errors: list[str] = []
    if not isinstance(raw, dict):
        return ["registry root must be a mapping"]
    if raw.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    rules = raw.get("rules")
    if not isinstance(rules, dict) or not rules:
        return [*errors, "'rules' must be a non-empty mapping"]
    try:
        rule_id_re = re.compile(cfg.rule_id_pattern)
    except re.error as exc:
        return [f"configured rule_id_pattern is invalid: {exc}"]
    valid_modalities = set(cfg.modalities)
    for rid, rule in rules.items():
        if not isinstance(rid, str) or not rule_id_re.fullmatch(rid):
            errors.append(f"{rid}: rule id does not match configured rule_id_pattern")
        if not isinstance(rule, dict):
            errors.append(f"{rid}: entry must be a mapping")
            continue
        unknown = set(rule) - {
            "file",
            "anchor",
            "concern_key",
            "modality",
            "selectors",
            "review_by",
            "note",
        }
        if unknown:
            errors.append(f"{rid}: unknown field(s): {', '.join(sorted(unknown))}")
        for req in ("file", "anchor", "concern_key", "modality", "review_by"):
            if not isinstance(rule.get(req), str) or not rule.get(req):
                errors.append(f"{rid}: missing required field {req!r}")
        anchor = rule.get("anchor")
        if isinstance(anchor, str) and len(anchor) < 8:
            errors.append(f"{rid}: anchor too short (min 8 chars) -- must be distinctive")
        concern = rule.get("concern_key")
        if isinstance(concern, str) and not CONCERN_KEY_RE.fullmatch(concern):
            errors.append(f"{rid}: concern_key {concern!r} is not a kebab-case slug")
        modality = rule.get("modality")
        if modality is not None and modality not in valid_modalities:
            errors.append(f"{rid}: modality {modality!r} not in {sorted(valid_modalities)}")
        selectors = rule.get("selectors", {})
        if selectors:
            if not isinstance(selectors, dict):
                errors.append(f"{rid}: selectors must be a mapping")
            else:
                for kind, values in selectors.items():
                    if kind not in SELECTOR_KINDS:
                        errors.append(f"{rid}: unknown selector kind {kind!r}")
                    elif not isinstance(values, list) or not all(
                        isinstance(value, str) for value in values
                    ):
                        errors.append(f"{rid}: selector {kind!r} must be a list of strings")
    return errors


def parse_registry(
    text: str, config: GovernanceConfig | None = None
) -> dict[str, dict[str, Any]]:
    raw = yaml.safe_load(text) if text.strip() else {}
    if raw in (None, {}):
        return {}
    errors = validate_registry_structure(raw, config)
    if errors:
        raise ValueError("; ".join(errors))
    return dict(raw["rules"])


def check_consistency(
    registry: dict[str, dict[str, Any]], read_file: Callable[[str], str | None]
) -> list[Finding]:
    findings: list[Finding] = []
    cache: dict[str, str | None] = {}
    for rid in sorted(registry):
        rule = registry[rid]
        path = rule["file"]
        if path not in cache:
            cache[path] = read_file(path)
        text = cache[path]
        if text is None:
            findings.append(Finding("FAIL", "consistency", f"{rid}: file {path!r} does not exist"))
        elif rule["anchor"] not in text:
            findings.append(
                Finding(
                    "FAIL",
                    "consistency",
                    f"{rid}: anchor is no longer present in {path!r} (stale registry "
                    f"entry -- update the anchor or remove the rule): {rule['anchor']!r}",
                )
            )
    return findings


def _paths_overlap(left: list[str], right: list[str]) -> bool:
    return any(
        a == b or a.startswith(b) or b.startswith(a)
        for a in left
        for b in right
    )


def selectors_overlap(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left = left or {}
    right = right or {}
    if _paths_overlap(left.get("paths", []) or [], right.get("paths", []) or []):
        return True
    return any(
        set(left.get(kind, []) or []) & set(right.get(kind, []) or [])
        for kind in ("workflows", "surfaces")
    )
