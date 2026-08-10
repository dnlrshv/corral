"""Trigger-rule model and deterministic glob/regex matcher."""

from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..config import DEFAULT_TRIGGER_RULES_SCHEMA


@dataclass(frozen=True)
class TriggerRule:
    id: str
    description: str
    path_globs: tuple[str, ...]
    keyword_patterns: tuple[str, ...]
    loads: tuple[str, ...]


@dataclass(frozen=True)
class TriggerRules:
    always_load: tuple[str, ...]
    rules: dict[str, TriggerRule]

    def all_load_paths(self) -> set[str]:
        paths = set(self.always_load)
        for rule in self.rules.values():
            paths.update(rule.loads)
        return paths


@dataclass(frozen=True)
class MatchResult:
    fired_rule_ids: frozenset[str]
    matched_loads: frozenset[str]
    loads_by_rule: dict[str, tuple[str, ...]] = field(default_factory=dict)


def _validate(raw: Any, schema: dict[str, Any]) -> list[str]:
    try:
        import jsonschema
    except ImportError:
        if not isinstance(raw, dict):
            return ["$: trigger-rules root must be a mapping"]
        return [
            f"$: {key!r} is a required property"
            for key in schema.get("required", [])
            if key not in raw
        ]
    validator_cls = jsonschema.validators.validator_for(schema)
    validator_cls.check_schema(schema)
    return [
        f"{error.json_path}: {error.message}"
        for error in sorted(validator_cls(schema).iter_errors(raw), key=lambda item: item.json_path)
    ]


def load_trigger_rules(
    rules_path: Path, schema_path: Path = DEFAULT_TRIGGER_RULES_SCHEMA
) -> TriggerRules:
    raw = yaml.safe_load(rules_path.read_text(encoding="utf-8")) or {}
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    errors = _validate(raw, schema)
    if errors:
        raise ValueError("; ".join(errors))
    rules: dict[str, TriggerRule] = {}
    for rule_id, value in raw["rules"].items():
        for pattern in value["keyword_patterns"]:
            re.compile(pattern)
        rules[rule_id] = TriggerRule(
            id=rule_id,
            description=value["description"],
            path_globs=tuple(value["path_globs"]),
            keyword_patterns=tuple(value["keyword_patterns"]),
            loads=tuple(value["loads"]),
        )
    return TriggerRules(always_load=tuple(raw["always_load"]), rules=rules)


def path_matches(path: str, glob: str) -> bool:
    """Component-aware matching; ``*`` never crosses a slash."""
    if glob.endswith("/**"):
        prefix = glob[:-3]
        return path == prefix or path.startswith(prefix + "/")
    wildcard = any(char in glob for char in "*?[")
    if not wildcard:
        return path == glob
    if "/" in glob:
        path_parts = path.split("/")
        glob_parts = glob.split("/")
        if len(path_parts) != len(glob_parts):
            return False
        return all(
            fnmatch.fnmatchcase(part, pattern)
            for part, pattern in zip(path_parts, glob_parts, strict=True)
        )
    return fnmatch.fnmatchcase(path.rsplit("/", 1)[-1], glob)


def rule_fires(rule: TriggerRule, touched_paths: list[str], task_text: str) -> bool:
    if any(
        path_matches(path, glob)
        for glob in rule.path_globs
        for path in touched_paths
    ):
        return True
    return any(re.search(pattern, task_text) for pattern in rule.keyword_patterns)


def match_rules(
    rules: TriggerRules, touched_paths: list[str], task_text: str
) -> MatchResult:
    fired: set[str] = set()
    loads_by_rule: dict[str, tuple[str, ...]] = {}
    matched = set(rules.always_load)
    for rule_id, rule in rules.rules.items():
        if rule_fires(rule, touched_paths, task_text):
            fired.add(rule_id)
            loads_by_rule[rule_id] = rule.loads
            matched.update(rule.loads)
    return MatchResult(frozenset(fired), frozenset(matched), loads_by_rule)


def topic_loads(
    paths: set[str] | frozenset[str], topic_prefixes: list[str] | tuple[str, ...]
) -> set[str]:
    """Select configured topic paths; an empty prefix list selects nothing."""
    return {path for path in paths if any(path.startswith(prefix) for prefix in topic_prefixes)}
