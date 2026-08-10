"""Decision-grade schema helpers for agent telemetry rollups."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

import pyarrow as pa

UTC = timezone.utc

SCHEMA = pa.schema(
    [
        ("session_id", pa.string()),
        ("agent", pa.string()),
        ("model", pa.string()),
        ("reported_model", pa.string()),
        ("complexity_class", pa.string()),
        ("complexity_reasons", pa.list_(pa.string())),
        ("band", pa.string()),
        ("tier", pa.string()),
        ("area", pa.string()),
        ("issue_type", pa.string()),
        ("arm", pa.string()),
        ("preflight_status", pa.string()),
        ("fallback_reason", pa.string()),
        ("workflow_kind", pa.string()),
        ("run_id", pa.int64()),
        ("artifact_name", pa.string()),
        ("artifact_id", pa.int64()),
        ("started_at", pa.timestamp("us", tz="UTC")),
        ("ended_at", pa.timestamp("us", tz="UTC")),
        ("duration_seconds", pa.float64()),
        ("tokens_available", pa.bool_()),
        ("tokens_in", pa.int64()),
        ("tokens_out", pa.int64()),
        ("cache_read_tokens", pa.int64()),
        ("cache_write_tokens", pa.int64()),
        ("tool_call_count", pa.int64()),
        ("input_token_cost_usd_per_million", pa.float64()),
        ("output_token_cost_usd_per_million", pa.float64()),
        ("cache_read_token_cost_usd_per_million", pa.float64()),
        ("cache_write_token_cost_usd_per_million", pa.float64()),
        ("pr_number", pa.int64()),
        ("merged", pa.bool_()),
        ("cycle_time_minutes", pa.float64()),
        ("changed_loc", pa.int64()),
        ("changed_loc_quartile", pa.string()),
        ("first_head_ci_green", pa.bool_()),
        ("ci_fix_iterations", pa.int64()),
        ("final_ci_green", pa.bool_()),
    ]
)

ROW_DEFAULTS: dict[str, Any] = {
    "session_id": "",
    "agent": "unknown",
    "model": "",
    "reported_model": None,
    "complexity_class": None,
    "complexity_reasons": None,
    "band": None,
    "tier": None,
    "area": "unknown",
    "issue_type": "unknown",
    "arm": "unknown",
    "preflight_status": "unknown",
    "fallback_reason": None,
    "workflow_kind": "unknown",
    "run_id": None,
    "artifact_name": None,
    "artifact_id": None,
    "started_at": None,
    "ended_at": None,
    "duration_seconds": None,
    "tokens_available": None,
    "tokens_in": 0,
    "tokens_out": 0,
    "cache_read_tokens": 0,
    "cache_write_tokens": 0,
    "tool_call_count": 0,
    "input_token_cost_usd_per_million": None,
    "output_token_cost_usd_per_million": None,
    "cache_read_token_cost_usd_per_million": None,
    "cache_write_token_cost_usd_per_million": None,
    "pr_number": None,
    "merged": False,
    "cycle_time_minutes": None,
    "changed_loc": None,
    "changed_loc_quartile": "unknown",
    "first_head_ci_green": None,
    "ci_fix_iterations": None,
    "final_ci_green": None,
}

TOKEN_COUNT_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
)


def coerce_int_or_none(value: Any) -> int | None:
    """Return an integer for numeric inputs; booleans are not token counts."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def coerce_bool_or_none(value: Any) -> bool | None:
    """Return a boolean only for unambiguous boolean-like values."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized == "true":
            return True
        if normalized == "false":
            return False
    return None


def _as_float_or_none(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_int(data: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = coerce_int_or_none(data.get(key))
        if value is not None:
            return value
    return None


def _parse_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _duration_seconds(started_at: Any, ended_at: Any) -> float | None:
    started = _parse_timestamp(started_at)
    ended = _parse_timestamp(ended_at)
    if started is None or ended is None:
        return None
    return (ended - started).total_seconds()


def _workflow_kind(session: dict[str, Any], artifact: dict[str, Any]) -> str:
    if session.get("workflow_kind"):
        return str(session["workflow_kind"])
    name = str(artifact.get("name") or "")
    if re.search(r"(^|-)pr-review(-|$)", name):
        return "review"
    if re.search(r"(^|-)merge(-|$)", name):
        return "merge"
    if re.search(r"(^|-)fix(-|$)", name) or name.startswith("agent-telemetry-codex-"):
        return "fix"
    return "unknown"


def _changed_loc(session: dict[str, Any], pr_info: dict[str, Any]) -> int | None:
    explicit = _first_int(session, "changed_loc", "changed_lines")
    if explicit is not None:
        return explicit
    from_pr = coerce_int_or_none(pr_info.get("changed_loc"))
    if from_pr is not None:
        return from_pr
    additions = coerce_int_or_none(pr_info.get("additions"))
    deletions = coerce_int_or_none(pr_info.get("deletions"))
    if additions is None and deletions is None:
        return None
    return (additions or 0) + (deletions or 0)


def _cost_input(session: dict[str, Any], key: str) -> float | None:
    value = session.get(f"{key}_token_cost_usd_per_million")
    if value is not None:
        return _as_float_or_none(value)
    return _as_float_or_none(session.get(f"{key}_cost_usd_per_million"))


def _artifact_name_run_id(artifact: dict[str, Any]) -> int | None:
    name = str(artifact.get("name") or "")
    match = re.search(r"-(\d+)$", name)
    if match is None:
        return None
    return coerce_int_or_none(match.group(1))


def _run_id(session: dict[str, Any], artifact: dict[str, Any]) -> int | None:
    workflow_run = artifact.get("workflow_run")
    if isinstance(workflow_run, dict):
        run_id = coerce_int_or_none(workflow_run.get("id"))
        if run_id is not None:
            return run_id
    return coerce_int_or_none(session.get("run_id")) or _artifact_name_run_id(artifact)


def resolve_preflight_status(session: dict[str, Any]) -> str:
    """Resolve canonical preflight status, preserving legacy arm inference."""
    if session.get("preflight_status"):
        return str(session["preflight_status"])
    arm = str(session.get("arm") or "")
    if arm in {"fallback", "generated"}:
        return arm
    if arm == "b":
        return "generated"
    if arm in {"a", "disabled"}:
        return "not_applicable"
    return "unknown"


def _fallback_reason(session: dict[str, Any]) -> str | None:
    """Structured cause of a `preflight_status: fallback` downgrade, when known.

    Nullable by design: unlike `preflight_status`, most sessions have no recorded
    reason (auth unavailable, network error, parse error, etc.) so this stays
    `None` rather than collapsing to a generic "unknown" categorical default.
    """
    value = session.get("fallback_reason")
    return str(value) if value else None


def _tokens_available(session: dict[str, Any]) -> bool:
    explicit = coerce_bool_or_none(session.get("tokens_available"))
    if explicit is not None:
        return explicit
    return any(session.get(field) is not None for field in TOKEN_COUNT_FIELDS)


def _token_value(session: dict[str, Any], field: str, tokens_available: bool) -> int | None:
    if not tokens_available:
        return None
    value = coerce_int_or_none(session.get(field))
    return value if value is not None else 0


def _tool_call_count(session: dict[str, Any]) -> int | None:
    if "tool_call_count" not in session:
        return 0
    return coerce_int_or_none(session.get("tool_call_count"))


def _artifact_name(session: dict[str, Any], artifact: dict[str, Any]) -> str | None:
    name = artifact.get("name") or session.get("artifact_name")
    return str(name) if name else None


def _ci_outcome_field(pr_info: dict[str, Any], session: dict[str, Any], key: str) -> Any:
    """Prefer the gh-reconstructed value on ``pr_info``; fall back to an explicit
    session override (mainly for tests / synthetic rows). Both are coerced the same
    way regardless of source."""
    if key in pr_info:
        return pr_info.get(key)
    return session.get(key)


def decision_fields(
    session: dict[str, Any], pr_info: dict[str, Any], artifact: dict[str, Any]
) -> dict[str, Any]:
    """Return nullable/defaulted decision-grade rollup fields."""
    started_at = _parse_timestamp(session.get("started_at"))
    ended_at = _parse_timestamp(session.get("ended_at"))
    changed_loc = _changed_loc(session, pr_info)
    tokens_available = _tokens_available(session)
    return {
        "complexity_class": str(session["complexity_class"])
        if session.get("complexity_class")
        else None,
        "complexity_reasons": (
            [str(item) for item in session["complexity_reasons"]]
            if isinstance(session.get("complexity_reasons"), list)
            and all(isinstance(item, str) for item in session["complexity_reasons"])
            else None
        ),
        "band": str(session["band"]) if session.get("band") else None,
        "tier": str(session["tier"]) if session.get("tier") else None,
        "preflight_status": resolve_preflight_status(session),
        "fallback_reason": _fallback_reason(session),
        "workflow_kind": _workflow_kind(session, artifact),
        "run_id": _run_id(session, artifact),
        "artifact_name": _artifact_name(session, artifact),
        "artifact_id": coerce_int_or_none(artifact.get("id"))
        or coerce_int_or_none(session.get("artifact_id")),
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_seconds": _duration_seconds(started_at, ended_at),
        "tokens_available": tokens_available,
        "tokens_in": _token_value(session, "input_tokens", tokens_available),
        "tokens_out": _token_value(session, "output_tokens", tokens_available),
        "cache_read_tokens": _token_value(session, "cache_read_tokens", tokens_available),
        "cache_write_tokens": _token_value(session, "cache_write_tokens", tokens_available),
        "tool_call_count": _tool_call_count(session),
        "input_token_cost_usd_per_million": _cost_input(session, "input"),
        "output_token_cost_usd_per_million": _cost_input(session, "output"),
        "cache_read_token_cost_usd_per_million": _cost_input(session, "cache_read"),
        "cache_write_token_cost_usd_per_million": _cost_input(session, "cache_write"),
        "changed_loc": changed_loc,
        "changed_loc_quartile": str(session.get("changed_loc_quartile") or "unknown"),
        "first_head_ci_green": coerce_bool_or_none(
            _ci_outcome_field(pr_info, session, "first_head_ci_green")
        ),
        "ci_fix_iterations": coerce_int_or_none(
            _ci_outcome_field(pr_info, session, "ci_fix_iterations")
        ),
        "final_ci_green": coerce_bool_or_none(
            _ci_outcome_field(pr_info, session, "final_ci_green")
        ),
    }
