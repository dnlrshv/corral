"""Capture coding-agent session telemetry from a Claude Code Stop hook.

The hook is intentionally fail-soft: any error is logged and the process exits 0
so telemetry collection can never block an agent session.

The spool directory is resolved in order:

1. ``--spool-dir`` CLI flag,
2. the ``CORRAL_TELEMETRY_DIR`` environment variable,
3. ``telemetry.spool_dir`` from ``corral.yaml`` (only when ``--config`` is
   passed),
4. the XDG-style default ``~/.cache/corral/telemetry`` (honouring
   ``$XDG_CACHE_HOME``).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

UTC = timezone.utc

#: Environment variable that overrides the telemetry spool directory.
SPOOL_DIR_ENV_VAR = "CORRAL_TELEMETRY_DIR"

AGENT_NAME = "claude"
TELEMETRY_FIELDS = (
    "session_id",
    "started_at",
    "ended_at",
    "model",
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

TOKEN_PATHS = {
    "input_tokens": (
        ("input_tokens",),
        ("usage", "input_tokens"),
        ("session", "input_tokens"),
        ("session", "usage", "input_tokens"),
        ("session_metadata", "input_tokens"),
        ("session_metadata", "usage", "input_tokens"),
    ),
    "output_tokens": (
        ("output_tokens",),
        ("usage", "output_tokens"),
        ("session", "output_tokens"),
        ("session", "usage", "output_tokens"),
        ("session_metadata", "output_tokens"),
        ("session_metadata", "usage", "output_tokens"),
    ),
    "cache_read_tokens": (
        ("cache_read_tokens",),
        ("cache_read_input_tokens",),
        ("usage", "cache_read_tokens"),
        ("usage", "cache_read_input_tokens"),
        ("session", "cache_read_tokens"),
        ("session", "usage", "cache_read_input_tokens"),
        ("session_metadata", "cache_read_tokens"),
        ("session_metadata", "usage", "cache_read_input_tokens"),
    ),
    "cache_write_tokens": (
        ("cache_write_tokens",),
        ("cache_creation_input_tokens",),
        ("usage", "cache_write_tokens"),
        ("usage", "cache_creation_input_tokens"),
        ("session", "cache_write_tokens"),
        ("session", "usage", "cache_creation_input_tokens"),
        ("session_metadata", "cache_write_tokens"),
        ("session_metadata", "usage", "cache_creation_input_tokens"),
    ),
}


def _get_logger() -> Any:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    return logging.getLogger(__name__)


LOGGER = _get_logger()


def default_telemetry_dir(environ: Mapping[str, str] | None = None) -> Path:
    """Resolve the telemetry spool directory.

    The ``CORRAL_TELEMETRY_DIR`` environment variable wins; otherwise the
    XDG-style cache location ``~/.cache/corral/telemetry`` is used (with
    ``$XDG_CACHE_HOME`` honoured when set).
    """
    env = os.environ if environ is None else environ
    override = env.get(SPOOL_DIR_ENV_VAR)
    if override:
        return Path(override).expanduser()
    xdg_cache_home = env.get("XDG_CACHE_HOME")
    cache_base = Path(xdg_cache_home).expanduser() if xdg_cache_home else Path.home() / ".cache"
    return cache_base / "corral" / "telemetry"


def main(argv: list[str] | None = None) -> int:
    try:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument(
            "--spool-dir",
            type=Path,
            default=None,
            help="Telemetry spool directory (default: $CORRAL_TELEMETRY_DIR, else "
            "~/.cache/corral/telemetry)",
        )
        parser.add_argument(
            "--config",
            type=Path,
            default=None,
            help="Path to corral.yaml; telemetry.spool_dir applies when the "
            "environment variable is unset",
        )
        args = parser.parse_args(argv)
        payload = _read_hook_payload(sys.stdin.read())
        environ = _resolved_environ(args)
        artifact = capture_session(payload, environ)
        LOGGER.info(f"Captured agent telemetry artifact: {artifact}")
    except SystemExit as exc:
        # argparse raises SystemExit(2) for malformed arguments.  This command
        # runs as a Stop hook, so even invocation errors must remain fail-soft.
        if exc.code:
            _log_failure("Telemetry capture arguments were invalid; continuing session")
    except Exception as exc:
        message = " ".join(str(exc).split())
        _log_failure(f"Telemetry capture failed; continuing session: {message}")
    return 0


def _log_failure(message: str) -> None:
    """Best-effort logging that preserves the Stop hook's fail-soft contract."""
    try:
        LOGGER.warning(message)
    except Exception:
        pass


def _resolved_environ(args: argparse.Namespace) -> dict[str, str]:
    environ = dict(os.environ)
    if args.spool_dir is not None:
        environ[SPOOL_DIR_ENV_VAR] = str(args.spool_dir)
        return environ
    if environ.get(SPOOL_DIR_ENV_VAR):
        return environ
    if args.config is not None:
        from corral.config import load_config

        configured = load_config(args.config).telemetry.spool_dir
        if configured:
            environ[SPOOL_DIR_ENV_VAR] = configured
    return environ


def capture_session(payload: Mapping[str, Any], environ: Mapping[str, str]) -> Path:
    transcript = _read_transcript(payload.get("transcript_path"))
    telemetry = build_telemetry(payload, environ, transcript)
    return write_telemetry(telemetry, environ)


def build_telemetry(
    payload: Mapping[str, Any],
    environ: Mapping[str, str],
    transcript: list[Mapping[str, Any]] | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    transcript = transcript or []
    ended_at = _first_string(payload, ("ended_at", "end_time", "timestamp")) or _utc_now(now)
    started_at = (
        _first_string(payload, ("started_at", "start_time"))
        or _first_transcript_timestamp(transcript)
        or ended_at
    )
    session_id = _session_id(payload, ended_at)
    token_totals = _token_totals(payload, transcript)
    tokens_available = _tokens_available(payload, transcript)

    telemetry: dict[str, Any] = {
        "session_id": session_id,
        "started_at": started_at,
        "ended_at": ended_at,
        "model": _model(payload, transcript),
        "input_tokens": token_totals["input_tokens"] if tokens_available else None,
        "output_tokens": token_totals["output_tokens"] if tokens_available else None,
        "cache_read_tokens": token_totals["cache_read_tokens"] if tokens_available else None,
        "cache_write_tokens": token_totals["cache_write_tokens"] if tokens_available else None,
        "tokens_available": tokens_available,
        "tool_call_count": _tool_call_count(payload, transcript),
        "pr_number": _pr_number(payload, environ),
        "repo": _repo(payload, environ),
        "agent": AGENT_NAME,
        "arm": _arm(payload, environ),
        "preflight_status": _preflight_status(payload, environ),
        "fallback_reason": _fallback_reason(payload, environ),
        "workflow_kind": _first_env(environ, ("AGENT_WORKFLOW_KIND", "GITHUB_WORKFLOW")),
        "run_id": _first_env(environ, ("GITHUB_RUN_ID",)),
    }
    return {field: telemetry[field] for field in TELEMETRY_FIELDS}


def write_telemetry(telemetry: Mapping[str, Any], environ: Mapping[str, str]) -> Path:
    telemetry_dir = default_telemetry_dir(environ)
    telemetry_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = telemetry_dir / f"{_safe_filename(str(telemetry['session_id']))}.json"
    artifact_path.write_text(json.dumps(telemetry, indent=2, sort_keys=False) + "\n")
    return artifact_path


def _read_hook_payload(raw_stdin: str) -> dict[str, Any]:
    if not raw_stdin.strip():
        return {}
    payload = json.loads(raw_stdin)
    if not isinstance(payload, dict):
        raise ValueError("Hook payload must be a JSON object")
    return payload


def _read_transcript(path_value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(path_value, str) or not path_value:
        return []

    path = Path(path_value).expanduser()
    if not path.exists():
        return []

    entries: list[Mapping[str, Any]] = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def _token_totals(
    payload: Mapping[str, Any], transcript: list[Mapping[str, Any]]
) -> dict[str, int]:
    explicit = {field: _first_int_path(payload, paths) for field, paths in TOKEN_PATHS.items()}
    if any(value is not None for value in explicit.values()):
        return {field: explicit[field] or 0 for field in TOKEN_PATHS}

    totals = dict.fromkeys(TOKEN_PATHS, 0)
    for entry in transcript:
        usage = _usage_from_transcript_entry(entry)
        totals["input_tokens"] += _coerce_int(usage.get("input_tokens")) or 0
        totals["output_tokens"] += _coerce_int(usage.get("output_tokens")) or 0
        totals["cache_read_tokens"] += _first_positive_int(
            usage.get("cache_read_input_tokens"), usage.get("cache_read_tokens")
        )
        totals["cache_write_tokens"] += _first_positive_int(
            usage.get("cache_creation_input_tokens"), usage.get("cache_write_tokens")
        )
    return totals


def _usage_from_transcript_entry(entry: Mapping[str, Any]) -> Mapping[str, Any]:
    message = entry.get("message")
    if isinstance(message, Mapping) and isinstance(message.get("usage"), Mapping):
        return message["usage"]
    if isinstance(entry.get("usage"), Mapping):
        return entry["usage"]
    return {}


def _tokens_available(payload: Mapping[str, Any], transcript: list[Mapping[str, Any]]) -> bool:
    return any(
        _first_int_path(payload, paths) is not None for paths in TOKEN_PATHS.values()
    ) or any(_usage_from_transcript_entry(entry) for entry in transcript)


def _tool_call_count(payload: Mapping[str, Any], transcript: list[Mapping[str, Any]]) -> int:
    explicit = _first_int(payload, ("tool_call_count", "tool_calls", "num_tool_calls"))
    if explicit is not None:
        return explicit

    count = 0
    for entry in transcript:
        message = entry.get("message")
        if not isinstance(message, Mapping):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        count += sum(
            1 for block in content if isinstance(block, Mapping) and block.get("type") == "tool_use"
        )
    return count


def _model(payload: Mapping[str, Any], transcript: list[Mapping[str, Any]]) -> str | None:
    model = _first_string(payload, ("model", "model_id"))
    if model:
        return model

    for entry in reversed(transcript):
        message = entry.get("message")
        if isinstance(message, Mapping) and isinstance(message.get("model"), str):
            return message["model"]
        if isinstance(entry.get("model"), str):
            return entry["model"]
    return None


def _session_id(payload: Mapping[str, Any], ended_at: str) -> str:
    session_id = _first_string(
        payload, ("session_id", "sessionId", "conversation_id", "conversationId")
    )
    if session_id:
        return session_id
    return "unknown-" + re.sub(r"[^0-9A-Za-z]+", "-", ended_at).strip("-")


def _pr_number(payload: Mapping[str, Any], environ: Mapping[str, str]) -> int | None:
    payload_value = _value_at_path(payload, ("pr_number",))
    if payload_value is None:
        payload_value = _value_at_path(payload, ("pull_request", "number"))
    value = payload_value or _first_env(
        environ, ("PR_NUMBER", "GITHUB_PR_NUMBER", "GH_PR_NUMBER", "PULL_REQUEST_NUMBER")
    )
    coerced = _coerce_int(value)
    return coerced


def _repo(payload: Mapping[str, Any], environ: Mapping[str, str]) -> str | None:
    repo = _first_string(payload, ("repo", "repository"))
    if repo:
        return repo
    repo = _first_env(environ, ("GITHUB_REPOSITORY", "GH_REPO", "REPOSITORY"))
    if repo:
        return repo
    return _repo_from_git_remote(Path(str(payload.get("cwd") or os.getcwd())))


def _arm(payload: Mapping[str, Any], environ: Mapping[str, str]) -> str:
    value = _first_string(payload, ("arm", "preflight_arm", "agent_preflight_arm"))
    if value:
        return value
    return _first_env(environ, ("AGENT_PREFLIGHT_ARM", "PREFLIGHT_ARM")) or "unknown"


def _preflight_status(payload: Mapping[str, Any], environ: Mapping[str, str]) -> str:
    value = _first_string(payload, ("preflight_status", "agent_preflight_status"))
    if value:
        return value
    return _first_env(environ, ("AGENT_PREFLIGHT_STATUS", "PREFLIGHT_STATUS")) or "unknown"


def _fallback_reason(payload: Mapping[str, Any], environ: Mapping[str, str]) -> str | None:
    """Structured cause of a `preflight_status: fallback` downgrade, when known.

    Nullable by design, unlike `preflight_status`: most fallbacks have no recorded
    reason (auth unavailable, network error, parse error, etc.) and this stays
    `None`. Populated with `semantic_quality` when post-validation discards a
    majority-hallucinated brief (`BriefQualityError`), or with
    `llm_response_invalid` when the brief response still fails to parse/validate
    after the one-shot retry (`BriefResponseError`).
    """
    value = _first_string(payload, ("fallback_reason", "agent_preflight_fallback_reason"))
    if value:
        return value
    return _first_env(environ, ("AGENT_PREFLIGHT_FALLBACK_REASON", "PREFLIGHT_FALLBACK_REASON"))


def _repo_from_git_remote(cwd: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except Exception:
        return None

    if result.returncode != 0:
        return None
    return _normalize_repo_remote(result.stdout.strip())


def _normalize_repo_remote(remote_url: str) -> str | None:
    patterns = (
        r"^https://github\.com/(?P<repo>[^/]+/[^/]+?)(?:\.git)?$",
        r"^git@github\.com:(?P<repo>[^/]+/[^/]+?)(?:\.git)?$",
    )
    for pattern in patterns:
        match = re.match(pattern, remote_url)
        if match:
            return match.group("repo")
    return None


def _first_transcript_timestamp(transcript: list[Mapping[str, Any]]) -> str | None:
    for entry in transcript:
        timestamp = entry.get("timestamp")
        if isinstance(timestamp, str) and timestamp:
            return timestamp
    return None


def _first_string(payload: Mapping[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _first_int(payload: Mapping[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        value = _coerce_int(payload.get(key))
        if value is not None:
            return value
    return None


def _first_int_path(payload: Mapping[str, Any], paths: tuple[tuple[str, ...], ...]) -> int | None:
    for path in paths:
        value = _coerce_int(_value_at_path(payload, path))
        if value is not None:
            return value
    return None


def _value_at_path(payload: Mapping[str, Any], path: tuple[str, ...]) -> Any:
    value: Any = payload
    for key in path:
        if not isinstance(value, Mapping):
            return None
        value = value.get(key)
    return value


def _first_env(environ: Mapping[str, str], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = environ.get(key)
        if value:
            return value
    return None


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _first_positive_int(*values: Any) -> int:
    for value in values:
        coerced = _coerce_int(value)
        if coerced is not None and coerced > 0:
            return coerced
    return 0


def _safe_filename(value: str) -> str:
    filename = re.sub(r"[^0-9A-Za-z_.-]+", "-", value).strip(".-")
    return filename or "unknown-session"


def _utc_now(now: datetime | None) -> str:
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current.astimezone(UTC).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())
