"""Portable executor result contract and informational self-report assessment."""

from __future__ import annotations

from copy import deepcopy
from .executor_brief import shape_errors

RESULT_SCHEMA_V1 = {
    "type": "object",
    "description": "Executor result for one corral attempt (corral-executor-result-v1).",
    "required": [
        "schema",
        "status",
        "summary",
        "acceptance",
        "commands_run",
        "files_changed",
        "open_questions",
        "blockers",
    ],
    "properties": {
        "schema": {"type": "string", "enum": ["corral-executor-result-v1"]},
        "status": {
            "type": "string",
            "enum": ["done", "partial", "blocked"],
            "description": "done: every acceptance criterion is met with evidence. partial: some are. blocked: work stopped on a blocker or an open question.",
        },
        "summary": {
            "type": "string",
            "description": "What was built and the key decisions.",
        },
        "acceptance": {
            "type": "array",
            "description": "One entry per acceptance criterion in the brief, by id.",
            "items": {
                "type": "object",
                "required": ["id", "status", "command", "evidence"],
                "properties": {
                    "id": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": ["met", "not-met", "not-verified"],
                    },
                    "command": {
                        "type": "string",
                        "description": "Id of the command in commands_run that demonstrates it; empty when not verified.",
                    },
                    "evidence": {
                        "type": "string",
                        "description": "The observation that supports the status: output lines, assertion names, file paths.",
                    },
                },
            },
        },
        "commands_run": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "argv", "cwd", "exit_code", "output_tail"],
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "The brief's command id, or empty for an ad hoc command.",
                    },
                    "argv": {"type": "array", "items": {"type": "string"}},
                    "cwd": {"type": "string"},
                    "exit_code": {"type": "integer"},
                    "output_tail": {
                        "type": "string",
                        "description": "The last lines of combined output.",
                    },
                },
            },
        },
        "files_changed": {"type": "array", "items": {"type": "string"}},
        "open_questions": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Ambiguities or source conflicts the executor stopped on instead of guessing.",
        },
        "blockers": {"type": "array", "items": {"type": "string"}},
    },
}


def for_harness(harness) -> dict:
    """Codex strict output requires closed objects; other harnesses use the portable base."""
    schema = deepcopy(RESULT_SCHEMA_V1)

    def close(node):
        if node.get("type") == "object":
            node["additionalProperties"] = False
        for child in node.get("properties", {}).values():
            close(child)
        if "items" in node:
            close(node["items"])

    if harness == "codex":
        close(schema)
    return schema


def assess(structured, brief) -> dict:
    """Describe coverage and shape; this report never grants or denies acceptance."""
    errors = (
        ["structured completion is not an object"]
        if not isinstance(structured, dict)
        else shape_errors(structured, RESULT_SCHEMA_V1, "result")
    )
    value = structured if isinstance(structured, dict) else {}

    def rows(key):
        value_rows = value.get(key)
        return value_rows if isinstance(value_rows, list) else []

    declared_rows = brief.get("acceptance", []) if isinstance(brief, dict) else []
    declared = (
        [
            r["id"]
            for r in declared_rows
            if isinstance(r, dict) and isinstance(r.get("id"), str)
        ]
        if isinstance(declared_rows, list)
        else []
    )
    criteria = {
        "declared": declared,
        "met": [],
        "not_met": [],
        "not_verified": [],
        "unreported": [],
        "unknown": [],
    }
    reported = []
    for row in rows("acceptance"):
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            continue
        id = row["id"]
        reported.append(id)
        for status, key in (
            ("met", "met"),
            ("not-met", "not_met"),
            ("not-verified", "not_verified"),
        ):
            if row.get("status") == status:
                criteria[key].append(id)
        if brief is not None and id not in declared:
            criteria["unknown"].append(id)
    criteria["unreported"] = [id for id in declared if id not in reported]
    return {
        "schema_valid": not errors,
        "errors": errors,
        "status": value.get("status") if isinstance(value.get("status"), str) else None,
        "criteria": criteria,
        "commands_run": len(rows("commands_run")),
        "nonzero_exits": sum(
            type(r.get("exit_code")) is int and r["exit_code"] != 0
            for r in rows("commands_run")
            if isinstance(r, dict)
        ),
        **{
            key: len(rows(key))
            for key in ("files_changed", "open_questions", "blockers")
        },
    }
