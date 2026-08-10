"""Write provider-neutral agent telemetry JSON records.

This is the lightweight GitHub Actions path for agents whose runtime does not
expose structured token usage. Local agent parsers can still provide exact
token counts; this writer preserves agent/run metadata with
``tokens_available=false`` instead of dropping the session from rollups.
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

UTC = timezone.utc

AGENTS = ("codex", "claude")
TELEMETRY_FIELDS = (
    "session_id",
    "started_at",
    "ended_at",
    "model",
    "complexity_class",
    "complexity_reasons",
    "band",
    "tier",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "tokens_available",
    "tool_call_count",
    "pr_number",
    "repo",
    "agent",
    "arm",
    "preflight_status",
    "fallback_reason",
    "workflow_kind",
    "run_id",
)


def build_telemetry(
    *,
    agent: str,
    session_id: str,
    model: str | None,
    environ: Mapping[str, str] | None = None,
    started_at: str | None = None,
    ended_at: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cache_read_tokens: int | None = None,
    cache_write_tokens: int | None = None,
    tokens_available: bool = False,
    tool_call_count: int | None = None,
    pr_number: int | str | None = None,
    repo: str | None = None,
    arm: str | None = None,
    preflight_status: str | None = None,
    fallback_reason: str | None = None,
    complexity_class: str | None = None,
    complexity_reasons: list[str] | None = None,
    band: str | None = None,
    tier: str | None = None,
    workflow_kind: str | None = None,
    run_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build one normalized telemetry record."""
    if agent not in AGENTS:
        raise ValueError(f"agent must be one of {', '.join(AGENTS)}")

    env = os.environ if environ is None else environ
    timestamp = _utc_now(now)
    record: dict[str, Any] = {
        "session_id": session_id,
        "started_at": started_at or _first_env(env, ("AGENT_STARTED_AT",)) or timestamp,
        "ended_at": ended_at or _first_env(env, ("AGENT_ENDED_AT",)) or timestamp,
        "model": model or _first_env(env, ("AGENT_MODEL", "CODEX_MODEL")),
        "complexity_class": complexity_class or _first_env(env, ("MERGE_COMPLEXITY_CLASS",)),
        "complexity_reasons": complexity_reasons
        if complexity_reasons is not None
        else _json_string_list(env.get("MERGE_COMPLEXITY_REASONS")),
        "band": band or _first_env(env, ("MERGE_COMPLEXITY_BAND",)),
        "tier": tier or _first_env(env, ("MERGE_COMPLEXITY_TIER",)),
        "input_tokens": input_tokens if tokens_available else None,
        "output_tokens": output_tokens if tokens_available else None,
        "cache_read_tokens": cache_read_tokens if tokens_available else None,
        "cache_write_tokens": cache_write_tokens if tokens_available else None,
        "tokens_available": tokens_available,
        "tool_call_count": tool_call_count,
        "pr_number": _coerce_pr_number(pr_number or _first_env(env, _PR_ENV_KEYS)),
        "repo": repo or _first_env(env, ("GITHUB_REPOSITORY", "GH_REPO", "REPOSITORY")),
        "agent": agent,
        "arm": arm or _first_env(env, ("AGENT_PREFLIGHT_ARM", "PREFLIGHT_ARM")) or "unknown",
        "preflight_status": preflight_status
        or _first_env(env, ("AGENT_PREFLIGHT_STATUS", "PREFLIGHT_STATUS"))
        or "unknown",
        "fallback_reason": fallback_reason
        or _first_env(env, ("AGENT_PREFLIGHT_FALLBACK_REASON", "PREFLIGHT_FALLBACK_REASON")),
        "workflow_kind": workflow_kind
        or _first_env(env, ("AGENT_WORKFLOW_KIND", "GITHUB_WORKFLOW")),
        "run_id": run_id or _first_env(env, ("GITHUB_RUN_ID",)),
    }
    return {field: record[field] for field in TELEMETRY_FIELDS}


def write_telemetry(record: Mapping[str, Any], output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return output


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", required=True, choices=AGENTS)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--model")
    parser.add_argument("--started-at")
    parser.add_argument("--ended-at")
    parser.add_argument("--input-tokens", type=int)
    parser.add_argument("--output-tokens", type=int)
    parser.add_argument("--cache-read-tokens", type=int)
    parser.add_argument("--cache-write-tokens", type=int)
    parser.add_argument("--tokens-available", action="store_true")
    parser.add_argument("--tool-call-count", type=int)
    parser.add_argument("--pr-number")
    parser.add_argument("--repo")
    parser.add_argument("--arm")
    parser.add_argument("--preflight-status")
    parser.add_argument("--fallback-reason")
    parser.add_argument("--complexity-class")
    parser.add_argument("--complexity-reason", action="append", dest="complexity_reasons")
    parser.add_argument("--band")
    parser.add_argument("--tier")
    parser.add_argument("--workflow-kind")
    parser.add_argument("--run-id")
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    record = build_telemetry(
        agent=args.agent,
        session_id=args.session_id,
        model=args.model,
        started_at=args.started_at,
        ended_at=args.ended_at,
        input_tokens=args.input_tokens,
        output_tokens=args.output_tokens,
        cache_read_tokens=args.cache_read_tokens,
        cache_write_tokens=args.cache_write_tokens,
        tokens_available=args.tokens_available,
        tool_call_count=args.tool_call_count,
        pr_number=args.pr_number,
        repo=args.repo,
        arm=args.arm,
        preflight_status=args.preflight_status,
        fallback_reason=args.fallback_reason,
        complexity_class=args.complexity_class,
        complexity_reasons=args.complexity_reasons,
        band=args.band,
        tier=args.tier,
        workflow_kind=args.workflow_kind,
        run_id=args.run_id,
    )
    write_telemetry(record, args.output)
    return 0


_PR_ENV_KEYS = ("PR_NUMBER", "GITHUB_PR_NUMBER", "GH_PR_NUMBER", "PULL_REQUEST_NUMBER")


def _coerce_pr_number(value: int | str | None) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _first_env(environ: Mapping[str, str], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = environ.get(key)
        if value:
            return value
    return None


def _json_string_list(value: str | None) -> list[str] | None:
    if not value:
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        return None
    return parsed


def _utc_now(now: datetime | None) -> str:
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())
