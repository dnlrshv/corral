"""Stream-shaped native envelopes: codex JSONL and the retained qwen-code rejection fixture.

Split out of :mod:`corral.execution.envelopes` so each module stays small; the parsing
contract is unchanged and every counter keeps the semantics the harness reported.
"""
from __future__ import annotations

import json

from .envelopes import (
    CODEX_TOTAL_COUNTERS,
    QWEN_STREAM_COUNTERS,
    Envelope,
    apply_error_in_success,
    counters_from,
    usage_event,
)


def parse_codex(*, stdout_text: str, stderr_text: str, exit_code: int, invocation: str,
                result_path: str | None, resumed: bool = False) -> Envelope:
    """codex exec --json: NDJSON events; identity from thread_settings_applied/session_meta."""
    envelope = Envelope(schema="codex-jsonl-v1", status="unknown")
    events: list[dict] = []
    unparsable = 0
    for line in (stdout_text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except ValueError:
            unparsable += 1
    if unparsable:
        envelope.warnings.append(f"{unparsable} stdout line(s) were not parsable JSONL")
    if not events:
        envelope.status = "failed"
        envelope.errors.append("codex produced no JSONL events")
        envelope.detail = {"exit_code": exit_code, "stderr_tail": (stderr_text or "")[-500:]}
        return envelope
    token_counts: list[dict] = []
    turn_usages: list[dict] = []
    narrative_parts: list[str] = []
    aborted: list[str] = []
    for event in events:
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        kind = payload.get("type")
        event_type = event.get("type")
        if event_type == "session_meta":
            for target, source in (("harness_version", "cli_version"), ("provider", "model_provider"),
                                   ("session_id", "session_id"), ("originator", "originator")):
                if payload.get(source) is not None:
                    envelope.identity[target] = payload[source]
        elif event_type == "thread.started":
            if event.get("thread_id") is not None:
                envelope.identity.setdefault("session_id", event["thread_id"])
        elif kind == "thread_settings_applied":
            settings = payload.get("thread_settings") or {}
            for target, source in (("model", "model"), ("effort", "reasoning_effort"),
                                   ("provider", "model_provider_id")):
                if settings.get(source) is not None:
                    envelope.identity[target] = settings[source]
        elif kind == "token_count":
            info = payload.get("info") or {}
            if isinstance(info.get("total_token_usage"), dict):
                token_counts.append(info)
        elif event_type == "turn.completed":
            usage_payload = event.get("usage")
            if isinstance(usage_payload, dict):
                turn_usages.append(usage_payload)
            elif usage_payload is not None:
                envelope.warnings.append("turn.completed usage field is malformed; ignored")
        elif kind == "agent_message":
            narrative_parts.append(str(payload.get("message") or ""))
        elif event_type == "item.completed":
            item = event.get("item")
            if isinstance(item, dict):
                item_type = item.get("type")
                if item_type == "agent_message":
                    narrative_parts.append(str(item.get("text") or ""))
                elif item_type == "error":
                    # Non-terminal item notice (e.g. model metadata fallback or non-fatal MCP warning)
                    msg = str(item.get("message") or "")[:200]
                    envelope.warnings.append(f"codex item notice: {msg or 'unspecified notice'}")
        elif event_type == "turn.failed":
            err = event.get("error")
            msg = ""
            if isinstance(err, dict):
                msg = str(err.get("message") or err.get("detail") or "")
            elif err:
                msg = str(err)
            elif event.get("message"):
                msg = str(event["message"])
            envelope.errors.append(f"codex turn failed: {msg or 'unspecified error'}")
        elif event_type in ("error", "stream_error"):
            err = event.get("error") or event.get("message")
            msg = ""
            if isinstance(err, dict):
                msg = str(err.get("message") or err.get("detail") or "")
            elif err:
                msg = str(err)
            envelope.errors.append(f"codex reported {event_type}: {msg or 'unspecified error'}")
        elif kind == "turn_aborted":
            aborted.append(str(payload.get("reason") or "unknown"))
        elif kind in ("error", "stream_error"):
            envelope.errors.append(f"codex reported {kind}: {str(payload.get('message') or '')[:200]}")
    envelope.narrative = "\n".join(part for part in narrative_parts if part).strip()
    if "model" not in envelope.identity:
        envelope.warnings.append("codex stream reported no thread_settings model; observed model stays unknown")
    structured = None
    if result_path:
        try:
            with open(result_path, encoding="utf-8") as handle:
                text = handle.read().strip()
            structured = json.loads(text) if text else None
        except OSError:
            envelope.warnings.append("codex --output-last-message file missing; structured stays unknown")
        except ValueError:
            envelope.warnings.append("codex last-message file is not JSON; retained as narrative text")
            structured = {"text": text}
    if structured is None:
        envelope.warnings.append("no structured completion channel; narrative retained separately")
    envelope.structured = structured
    if resumed:
        envelope.detail["resumed"] = True
        if token_counts:
            envelope.detail["token_counts"] = token_counts
            for info in token_counts:
                last = counters_from(info.get("last_token_usage"), CODEX_TOTAL_COUNTERS)
                if last:
                    envelope.detail.setdefault("final_turn_delta", last)
        if turn_usages:
            envelope.detail["turn_usages"] = turn_usages
        envelope.warnings.append(
            "codex session was resumed; turn.completed usage reflects cumulative session tokens "
            "across invocations, so invocation usage stays unknown to prevent cross-invocation double counting"
        )
    elif token_counts:
        for index, info in enumerate(token_counts, start=1):
            counters = counters_from(info.get("total_token_usage"), CODEX_TOTAL_COUNTERS)
            if not counters:
                continue
            envelope.usage_events.append(usage_event(
                event_id=f"{envelope.identity.get('session_id') or invocation}-total-{index}",
                scope="session", mode="cumulative", sequence=index, counters=counters,
                invocation=invocation, schema=envelope.schema, origin="native-measured",
                session_id=envelope.identity.get("session_id")))
            last = counters_from(info.get("last_token_usage"), CODEX_TOTAL_COUNTERS)
            if last:
                # Reported per-turn delta; kept out of summed counters to avoid double counting.
                envelope.detail.setdefault("final_turn_delta", last)
            if info.get("model_context_window") is not None:
                envelope.detail["model_context_window"] = info["model_context_window"]
        if turn_usages:
            envelope.detail["turn_usages"] = turn_usages
    elif len(turn_usages) == 1:
        counters = counters_from(turn_usages[0], CODEX_TOTAL_COUNTERS)
        if counters:
            envelope.usage_events.append(usage_event(
                event_id=f"{envelope.identity.get('session_id') or invocation}-turn-1",
                scope="session", mode="cumulative", sequence=1, counters=counters,
                invocation=invocation, schema=envelope.schema, origin="native-measured",
                session_id=envelope.identity.get("session_id")))
    elif len(turn_usages) > 1:
        extracted = [counters_from(u, CODEX_TOTAL_COUNTERS) for u in turn_usages]
        if all(c == extracted[0] for c in extracted) and extracted[0]:
            counters = extracted[0]
            envelope.usage_events.append(usage_event(
                event_id=f"{envelope.identity.get('session_id') or invocation}-turn-1",
                scope="session", mode="cumulative", sequence=1, counters=counters,
                invocation=invocation, schema=envelope.schema, origin="native-measured",
                session_id=envelope.identity.get("session_id")))
            envelope.detail["duplicate_terminal_events"] = len(turn_usages)
            envelope.detail["turn_usages"] = turn_usages
            envelope.warnings.append(
                f"codex stream emitted {len(turn_usages)} duplicate terminal turn.completed events; deduplicated to single snapshot"
            )
        else:
            envelope.detail["turn_usages"] = turn_usages
            envelope.warnings.append(
                "codex stream reported multiple turn.completed events without token_count; "
                "multi-turn aggregation is ambiguous, usage stays unknown"
            )
    if not envelope.usage_events and not any("ambiguous" in w or "resumed" in w for w in envelope.warnings):
        envelope.warnings.append("codex stream reported no token_count or turn.completed usage; usage stays unknown")
    if aborted:
        envelope.errors.append("codex turn aborted: " + ", ".join(sorted(set(aborted))))
    envelope.status = "failed" if envelope.errors else ("completed" if exit_code == 0 else "failed")
    if exit_code != 0 and not envelope.errors:
        envelope.errors.append(f"codex exited {exit_code}")
    final_counters = envelope.usage_events[-1]["counters"] if envelope.usage_events else {}
    apply_error_in_success(envelope, envelope.narrative, final_counters)
    envelope.detail["exit_code"] = exit_code
    envelope.detail["stderr_tail"] = (stderr_text or "")[-500:]
    envelope.detail["token_count_events"] = len(token_counts)
    envelope.detail["turn_completed_events"] = len(turn_usages)
    return envelope


def parse_qwen_stream(*, stdout_text: str, stderr_text: str, exit_code: int,
                       invocation: str) -> Envelope:
    """qwen-code CLI stream JSON. Retained as a rejection fixture: this route is broken.

    qwen-code 0.15.9 reports `subtype: success` / `is_error: false` while writing a
    provider error string into `result` and reporting all-zero usage. Parsing exists so
    the error-in-success control is proven against the captured failure, not so the route
    can be launched: no host declares it and the registry carries no qwen-code profile.
    """
    envelope = Envelope(schema="qwen-code-stream-v1", status="unknown")
    result_event: dict | None = None
    identity_model = None
    for line in (stdout_text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "system" and event.get("subtype") == "init":
            identity_model = event.get("model")
            envelope.identity["harness_version"] = event.get("qwen_code_version")
            envelope.identity["session_id"] = event.get("session_id")
            envelope.identity["permission_mode"] = event.get("permission_mode")
        if event.get("type") == "result":
            result_event = event
    if identity_model:
        envelope.identity["model"] = identity_model
        envelope.warnings.append("harness-reported model string is not provider attestation")
    if result_event is None:
        envelope.status = "failed"
        envelope.errors.append("qwen-code stream produced no result event")
        envelope.detail = {"exit_code": exit_code, "stderr_tail": (stderr_text or "")[-500:]}
        return envelope
    envelope.narrative = str(result_event.get("result") or "")
    envelope.status = str(result_event.get("subtype") or "unknown")
    counters = counters_from(result_event.get("usage"), QWEN_STREAM_COUNTERS)
    if counters:
        envelope.usage_events.append(usage_event(
            event_id=str(result_event.get("uuid") or f"{invocation}-qwen"), scope="session",
            mode="cumulative", sequence=1, counters=counters, invocation=invocation,
            schema=envelope.schema, origin="native-measured",
            session_id=result_event.get("session_id")))
    else:
        envelope.warnings.append("qwen-code reported no usable counters; usage stays unknown")
    envelope.structured = {"result_chars": len(envelope.narrative), "subtype": envelope.status,
                           "is_error": result_event.get("is_error"),
                           "num_turns": result_event.get("num_turns"),
                           "permission_denials": result_event.get("permission_denials")}
    apply_error_in_success(envelope, envelope.narrative, counters)
    if result_event.get("is_error") is True and not envelope.errors:
        envelope.errors.append("qwen-code result event reported is_error")
    if not envelope.narrative.strip():
        envelope.errors.append("qwen-code returned an empty result payload")
    if exit_code != 0 and not envelope.errors:
        envelope.errors.append(f"qwen-code exited {exit_code}")
    envelope.status = "failed" if envelope.errors else envelope.status
    envelope.detail = {"exit_code": exit_code, "duration_ms": result_event.get("duration_ms"),
                       "duration_api_ms": result_event.get("duration_api_ms"),
                       "num_turns": result_event.get("num_turns"),
                       "stderr_tail": (stderr_text or "")[-500:]}
    return envelope
