"""Versioned executor contract validation without optional schema dependencies."""

from __future__ import annotations

import posixpath
import re

from corral.redaction import check_outbound_safe
from .store import digest as _digest

BRIEF_SCHEMA_V1 = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "corral-executor-brief-v1",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema",
        "goal",
        "why",
        "sources",
        "acceptance",
        "commands",
        "constraints",
        "scope",
        "on_ambiguity",
    ],
    "properties": {
        "schema": {"const": "corral-executor-brief-v1"},
        "goal": {
            "type": "string",
            "minLength": 1,
            "description": "What to achieve, one or two sentences. Becomes the task "
            "objective.",
        },
        "why": {
            "type": "string",
            "minLength": 1,
            "description": "Why it matters; the context a reader cannot infer from the "
            "code.",
        },
        "links": {
            "type": "array",
            "items": {"type": "string"},
            "default": [],
            "description": "Issue, PR or document references, informational.",
        },
        "sources": {
            "type": "array",
            "minItems": 1,
            "description": "Authoritative inputs in priority order; the first "
            "entry wins a conflict. Newest decision wins over older "
            "text within the same entry.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["path"],
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Repository-relative, normalized, no '..'",
                    },
                    "role": {
                        "enum": ["authoritative", "reference"],
                        "default": "authoritative",
                    },
                    "note": {"type": "string"},
                },
            },
        },
        "acceptance": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "text", "verify"],
                "properties": {
                    "id": {"type": "string", "pattern": "^AC[0-9]{1,3}$"},
                    "text": {"type": "string", "minLength": 1},
                    "verify": {
                        "type": "string",
                        "minLength": 1,
                        "description": "The id of a "
                        "command in "
                        "`commands` that "
                        "demonstrates "
                        "this "
                        "criterion.",
                    },
                    "test": {
                        "type": "string",
                        "description": "Test file or test "
                        "id the executor "
                        "adds or runs, "
                        "when known.",
                    },
                },
            },
        },
        "commands": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "argv"],
                "properties": {
                    "id": {"type": "string", "pattern": "^[a-z0-9][a-z0-9._-]*$"},
                    "argv": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "string"},
                    },
                    "cwd": {
                        "type": "string",
                        "default": ".",
                        "description": "Repository-relative.",
                    },
                    "where": {
                        "type": "string",
                        "default": "workspace",
                        "description": "Where it runs, as "
                        "text for the "
                        "executor (for "
                        "example "
                        "'workspace').",
                    },
                    "env": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Names of environment "
                        "variables the "
                        "command needs; "
                        "values are never in "
                        "a brief.",
                    },
                    "evidence": {
                        "type": "string",
                        "description": "What counts as "
                        "evidence beyond "
                        "exit code and "
                        "output tail, "
                        "when the "
                        "default is not "
                        "enough.",
                    },
                },
            },
        },
        "constraints": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["text", "reason"],
                "properties": {
                    "text": {"type": "string", "minLength": 1},
                    "reason": {"type": "string", "minLength": 1},
                    "source": {"type": "string"},
                },
            },
        },
        "scope": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "change": {"type": "array", "items": {"type": "string"}},
                "read_only": {"type": "array", "items": {"type": "string"}},
                "out": {"type": "array", "items": {"type": "string"}},
            },
        },
        "on_ambiguity": {"enum": ["stop-and-report"]},
        "result": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"schema": {"enum": ["corral-executor-result-v1"]}},
        },
    },
}


def shape_errors(value, schema, path="brief") -> list[str]:
    """Validate the small JSON Schema subset used by executor contracts."""
    types = {
        "object": dict,
        "array": list,
        "string": str,
        "integer": int,
        "boolean": bool,
    }
    kind = schema.get("type")
    if kind and (
        not isinstance(value, types[kind])
        or (kind == "integer" and isinstance(value, bool))
    ):
        return [f"{path} must be {kind}"]
    errors = []
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path} must be {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path} must be one of {schema['enum']}")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}.{key} is required")
        if schema.get("additionalProperties") is False:
            errors.extend(
                f"{path}.{key} is unknown" for key in value if key not in properties
            )
        for key, child in properties.items():
            if key in value:
                errors.extend(shape_errors(value[key], child, f"{path}.{key}"))
    elif isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            errors.append(f"{path} requires at least {schema['minItems']} item(s)")
        for index, child in enumerate(value):
            errors.extend(
                shape_errors(child, schema.get("items", {}), f"{path}[{index}]")
            )
    elif isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            errors.append(f"{path} must be non-empty")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errors.append(f"{path} has an invalid format")
    return errors


def check_text(value, path="brief") -> None:
    """Refuse credentials in every string field, including amendment content."""
    if isinstance(value, str) and check_outbound_safe(value):
        raise PermissionError(f"credential-shaped brief text refused: {path}")
    if isinstance(value, dict):
        for key, child in value.items():
            check_text(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            check_text(child, f"{path}[{index}]")


def _relative(path):
    return (
        isinstance(path, str)
        and bool(path)
        and not path.startswith(("/", "~"))
        and posixpath.normpath(path) == path
        and ".." not in path.split("/")
    )


def validate(brief) -> list[str]:
    check_text(brief)
    errors = shape_errors(brief, BRIEF_SCHEMA_V1)
    if not isinstance(brief, dict):
        return errors
    commands = brief.get("commands")
    acceptance = brief.get("acceptance")
    for key, rows in (("commands", commands), ("acceptance", acceptance)):
        seen = set()
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict) and isinstance(row.get("id"), str):
                if row["id"] in seen:
                    errors.append(f"duplicate {key} id: {row['id']}")
                seen.add(row["id"])
    command_ids = (
        {
            row["id"]
            for row in commands or []
            if isinstance(row, dict) and isinstance(row.get("id"), str)
        }
        if isinstance(commands, list)
        else set()
    )
    for index, row in enumerate(acceptance if isinstance(acceptance, list) else []):
        if (
            isinstance(row, dict)
            and isinstance(row.get("verify"), str)
            and row["verify"] not in command_ids
        ):
            errors.append(f"acceptance {index} verify must name a declared command")
    paths = []
    for key, field in (("sources", "path"), ("commands", "cwd")):
        rows = brief.get(key)
        for index, row in enumerate(rows if isinstance(rows, list) else []):
            if isinstance(row, dict) and field in row:
                paths.append((f"{key}[{index}].{field}", row[field]))
    scope = brief.get("scope")
    if isinstance(scope, dict):
        for key in ("change", "read_only"):
            rows = scope.get(key)
            paths.extend(
                (f"scope.{key}[{i}]", p)
                for i, p in enumerate(rows if isinstance(rows, list) else [])
            )
    for label, path in paths:
        if not _relative(path):
            errors.append(
                f"{label} must be a relative normalized path without parent references"
            )
    rows = brief.get("constraints")
    for i, row in enumerate(rows if isinstance(rows, list) else []):
        if isinstance(row, dict) and (
            not isinstance(row.get("reason"), str) or not row["reason"].strip()
        ):
            errors.append(f"constraint {i} has no reason")
    return errors


def digest(brief) -> str:
    return _digest(brief)


def objective_for(brief) -> str:
    return brief["goal"]


def normalize_spec(spec: dict) -> None:
    """Validate a submitted/amended brief and keep its canonical objective coherent."""
    if "brief" not in spec:
        return
    errors = validate(spec["brief"])
    if errors:
        raise ValueError("brief: " + "; ".join(errors))
    goal = objective_for(spec["brief"])
    objective = spec.get("objective")
    if objective is not None and (
        not isinstance(objective, str) or objective.strip() != goal.strip()
    ):
        raise ValueError("objective and brief.goal differ; give one or make them equal")
    spec["objective"] = goal if objective is None else objective


def legacy_notice(policy) -> str:
    return "This task declares no acceptance criteria or verification commands; identify them from the objective."


def read_file(path) -> dict:
    """Validate a JSON brief even when a CLI also supplies an objective."""
    import json
    from pathlib import Path

    value = json.loads(Path(path).read_text(encoding="utf-8"))
    errors = validate(value)
    if errors:
        raise ValueError("brief: " + "; ".join(errors))
    return value


def config_for_workspace(workspace):
    """Read only the dispatch workspace's config; a parent checkout has no policy here."""
    from pathlib import Path
    import yaml
    from corral.config import CONFIG_FILENAME, Config, load_config
    path = Path(workspace) / CONFIG_FILENAME
    try:
        return (load_config(path) if path.exists() else Config()).executor_brief
    except yaml.YAMLError as exc:
        raise ValueError(f"{path}: malformed corral.yaml") from exc
