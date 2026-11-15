"""Deterministic executor prompt rendering; no I/O and no route substitution."""

from __future__ import annotations

import json
import shlex
from typing import Sequence

from .executor_brief import digest

HISTORY_HEADER = (
    "Prior amendments for this same task identity (continue, do not restart):"
)


def _amendment(item):
    item = dict(item)
    value = item.get("value")
    if isinstance(value, dict) and isinstance(value.get("brief"), dict):
        brief = value["brief"]
        item["value"] = {
            **value,
            "brief": {
                "goal": brief["goal"],
                "acceptance": [row["id"] for row in brief["acceptance"]],
            },
            "brief_digest": digest(brief),
        }
    return json.dumps(item, sort_keys=True)


def render(
    *,
    brief: dict | None,
    objective: str,
    rules: Sequence[dict],
    prompt_extras: str | None,
    amendment_history: Sequence[dict],
    schema_delivery: str,
    schema_file_hint: str,
    result_file_hint: str,
    result_schema: dict,
    legacy_notice: str | None,
) -> str:
    if schema_delivery not in ("harness-flag", "inline"):
        raise ValueError("schema_delivery must be harness-flag or inline")
    parts = ["# Task"]
    if brief is None:
        parts.append(objective)
        if legacy_notice:
            parts.append(legacy_notice)
    else:
        parts += ["## Goal", brief["goal"], "## Why", brief["why"]]
        if brief.get("links"):
            parts.append("Links: " + ", ".join(brief["links"]))
        parts.append("## Sources, in priority order")
        for i, source in enumerate(brief["sources"], 1):
            line = f"{i}. `{source['path']}` ({source.get('role', 'authoritative')})"
            if source.get("note"):
                line += " — " + source["note"]
            parts.append(line)
        parts.append(
            "Where two sources disagree, the earlier entry wins; within one source, the newest decision wins over older text."
        )
        parts.append("## Acceptance criteria")
        for criterion in brief["acceptance"]:
            line = f"{criterion['id']}. {criterion['text']}\n     Verified by command `{criterion['verify']}`."
            if criterion.get("test"):
                line += "   Test: " + criterion["test"]
            parts.append(line)
        parts.append("## Verification commands")
        for command in brief["commands"]:
            line = f"`{command['id']}`: `{shlex.join(command['argv'])}`  in `{command.get('cwd', '.')}`, runs: {command.get('where', 'workspace')}"
            if command.get("env"):
                line += "\n  needs: " + ", ".join(command["env"])
            if command.get("evidence"):
                line += "\n  evidence: " + command["evidence"]
            parts.append(line)
        parts.append(
            "Evidence for any command is the command, its exit code and the tail of its output. A command that did not start, or a syntax-only check, is not evidence; report that it could not run instead."
        )
    parts.append("## Constraints")
    for constraint in brief["constraints"] if brief else ():
        parts.append(f"- {constraint['text']} — because {constraint['reason']}")
    for rule in rules:
        parts.append(f"- {rule['text']} — because {rule['reason']} [{rule['id']}]")
    if brief is not None:
        parts.append("## Scope")
        for key, label in (
            ("change", "Change"),
            ("read_only", "Read only"),
            ("out", "Out of scope"),
        ):
            values = brief["scope"].get(key, [])
            content = ", ".join(f"`{v}`" if key != "out" else v for v in values)
            parts.append(f"{label}: {content or '(none declared)'}")
    parts += [
        "## If something is unclear",
        "Stop and report it in `open_questions` instead of choosing. A conflict between sources, a criterion you cannot verify with the listed commands, or a needed change outside the scope are all open questions or blockers, not decisions.",
        "## Result",
    ]
    criterion_text = (
        "one entry per criterion id above"
        if brief
        else "one entry per acceptance criterion you can identify in the task, or an empty list"
    )
    parts.append(
        f"Your final message is a JSON object with the fields: schema, status, summary, acceptance ({criterion_text}, with status met / not-met / not-verified, the command id and the evidence), commands_run (id, argv, cwd, exit_code, output_tail), files_changed, open_questions, blockers."
    )
    if schema_delivery == "harness-flag":
        parts.append(
            "The harness collects it against the result schema it was launched with."
        )
    else:
        parts.append(
            "Write exactly that object as the last message. The schema:\n```json\n"
            + json.dumps(result_schema, indent=2, sort_keys=True)
            + "\n```"
        )
    if prompt_extras:
        parts += ["## Continuation context", prompt_extras]
    if amendment_history:
        parts.append(HISTORY_HEADER)
        parts.extend("- " + _amendment(item) for item in amendment_history)
    return "\n\n".join(parts) + "\n"
