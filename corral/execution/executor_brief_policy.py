"""Controller-side pre-launch selection, policy and auditable executor brief records."""

from __future__ import annotations

import hashlib
import os
from dataclasses import asdict
from pathlib import Path

from corral.config import CONFIG_FILENAME, DEFAULT_EXECUTOR_BRIEF_BUDGET_CHARS
from corral.redaction import check_outbound_safe
from corral.rules import load_layers
from . import executor_brief, executor_brief_render, result_schema


def prepare(*, context, workspace, profile, route, task, generation, attempt):
    cfg = executor_brief.config_for_workspace(workspace)
    layers = load_layers(
        workspace=Path(workspace), repo_rules_file=cfg.rules_file, env=os.environ
    )
    brief = context.get("brief")
    mode = "brief" if brief is not None else "legacy-objective"
    warnings = []
    if mode == "legacy-objective":
        if cfg.legacy_objective == "refuse":
            raise PermissionError(
                "native dispatch requires a corral-executor-brief-v1 brief; objective-only dispatch is refused by executor_brief.legacy_objective"
            )
        warnings.append(executor_brief.legacy_notice(cfg.legacy_objective))
    else:
        errors = executor_brief.validate(brief)
        if errors:
            raise ValueError("brief: " + "; ".join(errors))
    extras = context.get("prompt_extras")
    if isinstance(extras, str) and check_outbound_safe(extras):
        raise PermissionError("credential-shaped prompt_extras text refused")
    applicable = layers.applicable(
        repo=context.get("service_repository_profile") or context.get("repo"),
        workspace=str(Path(workspace).resolve()),
        tier=profile.tier,
        phase=context.get("role", "implementation"),
        harness=route.harness,
    )
    applied = [asdict(rule) for rule in applicable]
    schema = context.get("result_schema") or result_schema.for_harness(route.harness)
    delivery = "harness-flag" if "{schema_file}" in route.argv else "inline"
    prompt = executor_brief_render.render(
        brief=brief,
        objective=context["objective"],
        rules=applied,
        prompt_extras=extras,
        amendment_history=context.get("amendment_history") or [],
        schema_delivery=delivery,
        schema_file_hint="{schema_file}",
        result_file_hint="{result_file}",
        result_schema=schema,
        legacy_notice=warnings[0] if warnings else None,
    )
    budget = cfg.max_prompt_chars or DEFAULT_EXECUTOR_BRIEF_BUDGET_CHARS[profile.tier]
    if len(prompt) > budget:
        raise PermissionError(
            f"rendered brief is {len(prompt)} characters; budget {budget} for tier {profile.tier}"
        )
    config_path = Path(workspace) / CONFIG_FILENAME
    render = {
        "tier": profile.tier,
        "schema_delivery": delivery,
        "budget_chars": budget,
        "prompt_chars": len(prompt),
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "rules_digest": layers.digest(),
        "config_source": str(config_path) if config_path.exists() else None,
        "config_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest()
        if config_path.exists()
        else None,
    }

    def layer_record(layer):
        return {"path": str(layer.path), "sha256": layer.sha256} if layer else None

    record = {
        "schema": "corral-executor-brief-record-v1",
        "task": task,
        "generation": generation,
        "attempt": attempt,
        "mode": mode,
        "brief": brief,
        "brief_digest": executor_brief.digest(brief) if brief is not None else None,
        "rules_applied": applied,
        "rules_digest": layers.digest(),
        "layers": {
            "packaged": {"count": len(layers.packaged.rules)},
            "repo": layer_record(layers.repo),
            "overlay": layer_record(layers.overlay),
        },
        "render": render,
        "warnings": warnings,
    }
    fields = {
        "prompt_markdown": prompt,
        "brief": brief,
        "brief_mode": mode,
        "rules_applied": applied,
        "brief_render": render,
        "result_schema": schema,
    }
    return fields, record


def result_record(record, structured):
    if record is None:
        return None
    return {
        "mode": record["mode"],
        "brief_digest": record["brief_digest"],
        "rules_digest": record["rules_digest"],
        "prompt_sha256": record["render"]["prompt_sha256"],
        "warnings": record["warnings"],
        "report": result_schema.assess(structured, record["brief"]),
    }
