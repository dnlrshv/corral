"""Versioned native envelope parsing: structured completion, narrative, identity, usage.

Every parser is bound to a declared schema id and to captured fixtures. Counters keep the
semantics the harness actually reports: cumulative session snapshots stay cumulative,
per-turn deltas stay deltas, and cache/reasoning subsets are recorded as separate fields
instead of being folded into input/output. Observed identity is only taken from fields the
harness reported; a successful exit code or a label never proves provider identity.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

# Reported counter name -> Corral counter name. Subset counters stay separate fields.
AGY_COUNTERS = {
    "input_tokens": "input_tokens",
    "output_tokens": "output_tokens",
    "thinking_tokens": "thinking_tokens",
    "cache_read_tokens": "cache_read_tokens",
    "cache_write_tokens": "cache_write_tokens",
    "total_tokens": "total_tokens",
}
QWEN_STREAM_COUNTERS = {
    "input_tokens": "input_tokens",
    "output_tokens": "output_tokens",
    "cache_read_input_tokens": "cache_read_tokens",
    "cache_creation_input_tokens": "cache_write_tokens",
}
CODEX_TOTAL_COUNTERS = {
    "input_tokens": "input_tokens",
    "cached_input_tokens": "cache_read_tokens",
    "output_tokens": "output_tokens",
    "reasoning_output_tokens": "thinking_tokens",
    "total_tokens": "total_tokens",
}
SYNTHETIC_COUNTERS = dict(AGY_COUNTERS)
INSPECTION_COUNTERS = {
    "input_tokens": "input_tokens",
    "output_tokens": "output_tokens",
    "thinking_tokens": "thinking_tokens",
    "cache_read_tokens": "cache_read_tokens",
    "total_tokens": "total_tokens",
}

# Text a harness writes into a nominally successful payload when the turn actually failed.
ERROR_IN_SUCCESS_MARKERS = (
    "[api error",
    "api error:",
    "connection error",
    "cause: fetch failed",
    "rate limit exceeded",
    "resource_exhausted",
    "invalid api-key",
    "invalidapikey",
    "unauthorized",
)

_AGY_SELECTION = re.compile(r'selected model override to backend: label="(?P<label>[^"]+)"')


@dataclass
class Envelope:
    schema: str
    status: str
    structured: Any = None
    narrative: str = ""
    identity: dict = field(default_factory=dict)
    usage_events: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    detail: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors


def _valid_counter(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def counters_from(payload: Any, mapping: dict[str, str]) -> dict[str, int]:
    if not isinstance(payload, dict):
        return {}
    result = {}
    for source, target in mapping.items():
        value = _valid_counter(payload.get(source))
        if value is not None:
            result[target] = value
    return result


def usage_event(*, event_id: str, scope: str, mode: str, sequence: int, counters: dict[str, int],
                invocation: str, schema: str, origin: str, session_id: str | None = None) -> dict:
    event = {"id": event_id, "scope": scope, "epoch": 0, "sequence": sequence, "mode": mode,
             "counters": counters, "invocation": invocation, "schema": schema, "origin": origin}
    if session_id:
        event["session_id"] = session_id
    return event


def _scan_error_text(text: str) -> list[str]:
    lowered = (text or "").lower()
    return sorted({marker for marker in ERROR_IN_SUCCESS_MARKERS if marker in lowered})


def apply_error_in_success(envelope: Envelope, text: str, counters: dict[str, int]) -> None:
    """Reject mislabeled failures without rejecting substantive text that discusses errors.

    A payload that *starts* with a provider error marker is a failure whatever the exit
    code says. A marker buried in an otherwise substantive payload only fails when the
    harness reported no output tokens, which is the captured qwen-code 0.15.9 signature
    (`subtype: success`, `is_error: false`, `[API Error: ... fetch failed]`, all-zero usage).
    """
    markers = _scan_error_text(text)
    if not markers:
        return
    head = (text or "").strip()[:200].lower()
    if any(marker in head for marker in markers):
        envelope.errors.append("payload begins with a provider error marker: " + ", ".join(markers))
        return
    if counters.get("output_tokens", 0) == 0:
        envelope.errors.append("error marker text with zero reported output tokens "
                               "(mislabeled failure): " + ", ".join(markers))
    else:
        envelope.warnings.append("error marker text inside a substantive payload; retained as warning: "
                                 + ", ".join(markers))


def parse(schema: str, *, stdout_text: str, stderr_text: str, exit_code: int,
          invocation: str, result_path: str | None = None, log_text: str = "",
          synthetic_expected: bool = False) -> Envelope:
    """Dispatch to the declared schema parser; unknown schemas fail closed."""
    if schema == "corral-synthetic-v1":
        return _parse_synthetic(stdout_text=stdout_text, stderr_text=stderr_text, exit_code=exit_code,
                                invocation=invocation, result_path=result_path,
                                synthetic_expected=synthetic_expected)
    if schema == "agy-json-v1":
        return _parse_agy(stdout_text=stdout_text, stderr_text=stderr_text, exit_code=exit_code,
                          invocation=invocation, log_text=log_text)
    if schema == "corral-inspection-report-v1":
        return _parse_inspection(stdout_text=stdout_text, stderr_text=stderr_text,
                                 exit_code=exit_code, invocation=invocation,
                                 synthetic_expected=synthetic_expected)
    if schema in ("codex-jsonl-v1", "qwen-code-stream-v1"):
        # Imported here to keep the stream parsers in their own module without an import cycle.
        from . import envelope_streams

        if schema == "codex-jsonl-v1":
            return envelope_streams.parse_codex(stdout_text=stdout_text, stderr_text=stderr_text,
                                                exit_code=exit_code, invocation=invocation,
                                                result_path=result_path)
        return envelope_streams.parse_qwen_stream(stdout_text=stdout_text, stderr_text=stderr_text,
                                                 exit_code=exit_code, invocation=invocation)
    raise ValueError(f"unsupported envelope schema: {schema}")


def _parse_inspection(*, stdout_text: str, stderr_text: str, exit_code: int,
                      invocation: str, synthetic_expected: bool) -> Envelope:
    """Parse the one-response, no-tool inspection transport envelope."""
    envelope = Envelope(schema="corral-inspection-report-v1", status="unknown")
    payload = _load_json_text(stdout_text)
    if not isinstance(payload, dict):
        envelope.status = "failed"
        envelope.errors.append("inspection transport produced no parsable envelope")
        envelope.detail = {"exit_code": exit_code, "stderr_tail": (stderr_text or "")[-500:]}
        return envelope
    if payload.get("schema") != envelope.schema:
        envelope.errors.append("inspection envelope schema mismatch")
    if bool(payload.get("synthetic")) != bool(synthetic_expected):
        envelope.errors.append("inspection envelope synthetic identity mismatch")
    envelope.status = str(payload.get("status") or "unknown")
    if envelope.status != "completed":
        envelope.errors.append("inspection transport did not report completion")
    envelope.narrative = str(payload.get("narrative") or "")
    if not envelope.narrative.strip():
        envelope.errors.append("inspection report is empty")
    envelope.structured = payload.get("result")
    _reject_empty(envelope, envelope.structured)
    identity = payload.get("identity")
    if isinstance(identity, dict):
        envelope.identity = {key: value for key, value in identity.items()
                             if key in ("model", "provider", "account_ref", "route",
                                        "harness", "version")}
    else:
        envelope.errors.append("inspection response identity is missing")
    counters = counters_from(payload.get("usage"), INSPECTION_COUNTERS)
    if counters:
        envelope.usage_events.append(usage_event(
            event_id=f"{invocation}-inspection", scope="turn", mode="delta", sequence=1,
            counters=counters, invocation=invocation, schema=envelope.schema,
            origin="native-measured"))
    else:
        envelope.warnings.append("inspection response reported no usable usage counters")
    observed = payload.get("observed") if isinstance(payload.get("observed"), dict) else {}
    envelope.detail = {"exit_code": exit_code, "stderr_tail": (stderr_text or "")[-500:],
                       "requested": payload.get("requested"), "observed": observed,
                       "session_mode": observed.get("session_mode"),
                       "effort_attested": observed.get("effort_attested")}
    if observed.get("session_mode") != "stateless":
        envelope.errors.append("inspection transport did not attest stateless invocation")
    if exit_code != 0:
        envelope.errors.append(f"inspection transport exited {exit_code}")
    apply_error_in_success(envelope, envelope.narrative, counters)
    return envelope


def _reject_empty(envelope: Envelope, structured: Any) -> None:
    if structured is None:
        envelope.errors.append("structured completion missing")
    elif isinstance(structured, (dict, list, str)) and len(structured) == 0:
        envelope.errors.append("structured completion empty")


def _load_json_text(text: str | None) -> Any:
    if not text or not text.strip():
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _read_text(path: str | None) -> str | None:
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return None


def _parse_synthetic(*, stdout_text: str, stderr_text: str, exit_code: int, invocation: str,
                     result_path: str | None, synthetic_expected: bool) -> Envelope:
    """Parse the explicitly synthetic fixture envelope used by offline full-path proofs.

    The envelope (status, identity, usage, narrative) is reported on stdout; the structured
    completion is a separate artifact so "the harness said something" and "the harness
    produced a machine-checkable result" are never conflated.
    """
    envelope = Envelope(schema="corral-synthetic-v1", status="unknown")
    payload = _load_json_text(stdout_text)
    source = "stdout"
    if not isinstance(payload, dict):
        payload = _load_json_text(_read_text(result_path))
        source = "result_file"
    if not isinstance(payload, dict):
        envelope.status = "failed"
        envelope.errors.append("synthetic harness produced no parsable envelope")
        envelope.detail = {"exit_code": exit_code, "stderr_tail": (stderr_text or "")[-500:]}
        return envelope
    if payload.get("schema") != "corral-synthetic-v1":
        envelope.errors.append(f"synthetic envelope schema mismatch: {payload.get('schema')!r}")
    if not synthetic_expected:
        envelope.errors.append("synthetic envelope returned for a non-synthetic route")
    if payload.get("synthetic") is not True:
        envelope.errors.append("synthetic envelope is not explicitly marked synthetic")
    envelope.status = str(payload.get("status") or "unknown")
    envelope.narrative = str(payload.get("narrative") or "")
    identity = payload.get("identity")
    if isinstance(identity, dict):
        envelope.identity = {key: value for key, value in identity.items()
                             if key in ("model", "effort", "harness", "provider", "version",
                                        "account_ref", "route")}
    else:
        envelope.warnings.append("synthetic envelope reported no identity block; observed stays unknown")
    structured = payload.get("result")
    if structured is None:
        if source == "stdout":
            structured = _load_json_text(_read_text(result_path))
            if structured is None:
                envelope.warnings.append("declared structured result file was absent or unparsable")
        envelope.detail["structured_source"] = "result_file" if structured is not None else "missing"
    else:
        envelope.detail["structured_source"] = "envelope"
    envelope.structured = structured
    counters = counters_from(payload.get("usage"), SYNTHETIC_COUNTERS)
    mode = str(payload.get("usage_mode") or "cumulative")
    if mode not in ("cumulative", "delta"):
        envelope.errors.append(f"synthetic envelope declares unsupported usage_mode {mode!r}")
    elif counters:
        envelope.usage_events.append(usage_event(
            event_id=str(payload.get("session_id") or f"{invocation}-synthetic"), scope="session",
            mode=mode, sequence=1, counters=counters, invocation=invocation,
            schema=envelope.schema, origin="synthetic-fixture",
            session_id=payload.get("session_id")))
    else:
        envelope.warnings.append("synthetic envelope reported no usage counters; usage stays unknown")
    _reject_empty(envelope, envelope.structured)
    if envelope.status not in ("completed", "succeeded"):
        envelope.errors.append(f"synthetic envelope status {envelope.status!r} is not a completion")
    apply_error_in_success(envelope, envelope.narrative, counters)
    if exit_code != 0 and not envelope.errors:
        envelope.errors.append(f"synthetic harness exited {exit_code}")
    envelope.detail.update({"exit_code": exit_code, "num_turns": payload.get("num_turns"),
                            "synthetic": True, "stderr_tail": (stderr_text or "")[-500:]})
    if payload.get("detail") is not None:
        # Raw harness-reported metadata is preserved even when the envelope is rejected.
        envelope.detail["harness_detail"] = payload["detail"]
    return envelope


def _parse_agy(*, stdout_text: str, stderr_text: str, exit_code: int, invocation: str,
               log_text: str) -> Envelope:
    """agy --output-format json: one JSON object with cumulative session counters."""
    envelope = Envelope(schema="agy-json-v1", status="unknown")
    try:
        payload = json.loads((stdout_text or "").strip())
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        envelope.status = "failed"
        envelope.errors.append("agy produced no parsable JSON envelope")
        envelope.detail = {"exit_code": exit_code, "stdout_tail": (stdout_text or "")[-500:],
                           "stderr_tail": (stderr_text or "")[-500:]}
        return envelope
    raw_status = str(payload.get("status") or "unknown")
    envelope.status = raw_status.upper()
    conversation = payload.get("conversation_id")
    if conversation:
        envelope.identity["session_id"] = str(conversation)
    labels = _AGY_SELECTION.findall(log_text or "")
    if labels:
        # Harness-reported backend selection is evidence of the requested label only.
        envelope.identity["harness_model_label"] = labels[-1]
        envelope.warnings.append("harness selection label is not independent provider attestation")
    else:
        envelope.warnings.append("no harness model selection evidence; observed model stays unknown")
    envelope.narrative = str(payload.get("response") or "")
    structured = payload.get("structured")
    if structured is None:
        structured = {"response_chars": len(envelope.narrative), "status": envelope.status,
                      "conversation_id": conversation, "num_turns": payload.get("num_turns")}
        envelope.warnings.append("agy envelope carries narrative only; structured completion synthesized "
                                 "from reported status fields")
    envelope.structured = structured
    counters = counters_from(payload.get("usage"), AGY_COUNTERS)
    if counters:
        # agy envelopes are cumulative session snapshots; continuations supersede, never sum.
        envelope.usage_events.append(usage_event(
            event_id=str(conversation or f"{invocation}-agy"), scope="session", mode="cumulative",
            sequence=1, counters=counters, invocation=invocation, schema=envelope.schema,
            origin="native-measured", session_id=conversation))
    else:
        envelope.warnings.append("agy envelope reported no usage counters; usage stays unknown")
    if envelope.status != "SUCCESS":
        envelope.errors.append(f"agy reported status {raw_status!r}")
    if not envelope.narrative.strip() and not payload.get("structured"):
        envelope.errors.append("agy returned an empty result payload")
    apply_error_in_success(envelope, envelope.narrative, counters)
    if exit_code != 0 and not envelope.errors:
        envelope.errors.append(f"agy exited {exit_code}")
    envelope.detail = {"exit_code": exit_code, "num_turns": payload.get("num_turns"),
                       "duration_seconds": payload.get("duration_seconds"),
                       "stderr_tail": (stderr_text or "")[-500:]}
    return envelope
