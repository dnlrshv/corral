"""Regression tests for Codex CLI stream envelope parsing and usage normalization.

Covers:
- Turn.completed usage event parsing (codex-cli 0.144.x shape)
- Counter semantics: subset retention without double counting
- Precedence: token_count cumulative usage over turn.completed if both present
- Multi-turn turn.completed without token_count treated as ambiguous (kept unknown)
- Resumed native session distinction preventing cross-invocation double counting
- Missing / malformed usage retention without failing completion
- Thread.started session_id extraction
- Item.completed agent message narrative extraction
- Item.completed error notice retained as warning, not terminal failure
- Terminal turn.failed and top-level error events fail envelope even if exit code is 0
- Portable checked-in synthetic stream replay
- Opt-in raw artifact replay via CORRAL_PILOT_HARNESS_STDOUT
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from corral.execution import envelope_streams, usage


def test_codex_turn_completed_usage_exact_counters():
    """turn.completed reports input_tokens, cached_input_tokens, output_tokens, reasoning_output_tokens."""
    stdout = "\n".join([
        json.dumps({"type": "thread.started", "thread_id": "thread-abc-123"}),
        json.dumps({"type": "item.completed", "item": {"id": "item_1", "type": "agent_message", "text": "all done"}}),
        json.dumps({
            "type": "turn.completed",
            "usage": {"input_tokens": 320000, "cached_input_tokens": 288000,
                      "output_tokens": 13000, "reasoning_output_tokens": 7500},
        }),
    ])
    env = envelope_streams.parse_codex(
        stdout_text=stdout, stderr_text="", exit_code=0, invocation="inv-1", result_path=None
    )
    assert env.ok and env.status == "completed"
    assert env.identity.get("session_id") == "thread-abc-123"
    assert env.narrative == "all done"
    assert len(env.usage_events) == 1

    event = env.usage_events[0]
    assert event["scope"] == "session" and event["mode"] == "cumulative" and event["sequence"] == 1
    assert event["origin"] == "native-measured" and event["session_id"] == "thread-abc-123"
    assert event["counters"] == {
        "input_tokens": 320000, "cache_read_tokens": 288000,
        "output_tokens": 13000, "thinking_tokens": 7500,
    }

    # Verify summary: subset fields are NOT added back into input/output totals.
    summary = usage.summarize(env.usage_events)
    assert summary["measured_fields"] == {
        "input_tokens": 320000, "cache_read_tokens": 288000,
        "output_tokens": 13000, "thinking_tokens": 7500,
    }
    assert summary["observed_fields"] == summary["measured_fields"]
    assert summary["unknown"] == []


def test_codex_no_double_counting_when_both_token_count_and_turn_completed():
    """When both token_count and turn.completed exist, token_count takes precedence to prevent double-count."""
    stdout = "\n".join([
        json.dumps({
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {"input_tokens": 1000, "cached_input_tokens": 200,
                                          "output_tokens": 100, "reasoning_output_tokens": 50},
                    "last_token_usage": {"input_tokens": 500, "output_tokens": 50},
                },
            }
        }),
        json.dumps({
            "type": "turn.completed",
            "usage": {"input_tokens": 1000, "cached_input_tokens": 200,
                      "output_tokens": 100, "reasoning_output_tokens": 50},
        }),
    ])
    env = envelope_streams.parse_codex(
        stdout_text=stdout, stderr_text="", exit_code=0, invocation="inv-both", result_path=None
    )
    assert env.ok and env.status == "completed"
    assert len(env.usage_events) == 1
    assert env.usage_events[0]["counters"] == {
        "input_tokens": 1000, "cache_read_tokens": 200, "output_tokens": 100, "thinking_tokens": 50,
    }
    assert env.detail.get("final_turn_delta") == {"input_tokens": 500, "output_tokens": 50}
    assert len(env.detail.get("turn_usages", [])) == 1

    summary = usage.summarize(env.usage_events)
    assert summary["measured_fields"] == {
        "input_tokens": 1000, "cache_read_tokens": 200, "output_tokens": 100, "thinking_tokens": 50,
    }


def test_codex_multi_turn_completed_without_token_count_is_ambiguous():
    """Multiple turn.completed events without session token_count cannot be safely summed or accumulated."""
    stdout = "\n".join([
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 500, "output_tokens": 100}}),
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 800, "output_tokens": 50}}),
    ])
    env = envelope_streams.parse_codex(
        stdout_text=stdout, stderr_text="", exit_code=0, invocation="inv-multi", result_path=None
    )
    assert env.ok and env.status == "completed"
    assert env.usage_events == []
    assert any("multi-turn aggregation is ambiguous" in w for w in env.warnings)
    assert len(env.detail.get("turn_usages", [])) == 2

    summary = usage.summarize(env.usage_events, registered=["inv-multi"])
    assert summary["measured_fields"] == {}
    assert {"invocation": "inv-multi", "reason": "no native events"} in summary["unknown"]


def test_codex_resumed_session_distinction_prevents_cross_invocation_double_counting():
    """Single turn.completed from a resumed native session reflects cumulative thread tokens, not fresh invocation usage.

    Primary evidence: codex-rs/exec/src/event_processor_with_jsonl_output.rs lines 125-136 & 520-526.
    TurnCompletedEvent.usage is mapped from ThreadTokenUsage.total (cumulative thread tokens).
    Across distinct invocation IDs, emitting resumed cumulative tokens under invocation would cause
    usage.summarize() to sum prior and resumed invocations together.
    """
    stdout_inv1 = "\n".join([
        json.dumps({"type": "thread.started", "thread_id": "thread-shared-456"}),
        json.dumps({"type": "turn.completed",
                    "usage": {"input_tokens": 1000, "cached_input_tokens": 200,
                              "output_tokens": 100, "reasoning_output_tokens": 50}}),
    ])
    stdout_inv2 = "\n".join([
        json.dumps({"type": "thread.started", "thread_id": "thread-shared-456"}),
        json.dumps({"type": "turn.completed",
                    "usage": {"input_tokens": 1800, "cached_input_tokens": 600,
                              "output_tokens": 180, "reasoning_output_tokens": 80}}),
    ])

    # Invocation 1: fresh start
    env1 = envelope_streams.parse_codex(
        stdout_text=stdout_inv1, stderr_text="", exit_code=0, invocation="inv-1",
        result_path=None, resumed=False
    )
    assert env1.ok and len(env1.usage_events) == 1
    assert env1.usage_events[0]["counters"]["input_tokens"] == 1000

    # Invocation 2: resumed native session
    env2 = envelope_streams.parse_codex(
        stdout_text=stdout_inv2, stderr_text="", exit_code=0, invocation="inv-2",
        result_path=None, resumed=True
    )
    assert env2.ok
    assert env2.usage_events == []
    assert any("resumed" in w for w in env2.warnings)
    assert env2.detail.get("resumed") is True
    assert len(env2.detail.get("turn_usages", [])) == 1

    # Combined accounting across invocations: inv-1 keeps honest measured counters; inv-2 is reported under unknown
    summary = usage.summarize(env1.usage_events + env2.usage_events, registered=["inv-1", "inv-2"])
    # Crucial: measured_fields is exactly 1000, NOT 1000 + 1800 = 2800
    assert summary["measured_fields"]["input_tokens"] == 1000
    assert summary["measured_fields"]["output_tokens"] == 100
    assert {"invocation": "inv-2", "reason": "no native events"} in summary["unknown"]


def test_codex_malformed_usage_preserves_completion_and_reports_gap():
    """Malformed usage payload warns and reports gap, but does not invalidate successful task execution."""
    stdout = "\n".join([
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "finished work"}}),
        json.dumps({"type": "turn.completed", "usage": "not-a-dict"}),
    ])
    env = envelope_streams.parse_codex(
        stdout_text=stdout, stderr_text="", exit_code=0, invocation="inv-bad-usage", result_path=None
    )
    assert env.ok and env.status == "completed" and env.narrative == "finished work"
    assert env.usage_events == []
    assert any("malformed" in w for w in env.warnings)
    assert any("usage stays unknown" in w for w in env.warnings)

    summary = usage.summarize(env.usage_events, registered=["inv-bad-usage"])
    assert summary["measured_fields"] == {}
    assert {"invocation": "inv-bad-usage", "reason": "no native events"} in summary["unknown"]


def test_codex_missing_usage_preserves_completion_and_reports_gap():
    """Stream without token_count or turn.completed completes with truthful usage gap warning."""
    stdout = "\n".join([
        json.dumps({"type": "thread.started", "thread_id": "thread-no-usage"}),
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "done without usage"}}),
    ])
    env = envelope_streams.parse_codex(
        stdout_text=stdout, stderr_text="", exit_code=0, invocation="inv-no-usage", result_path=None
    )
    assert env.ok and env.status == "completed" and env.usage_events == []
    assert any("usage stays unknown" in w for w in env.warnings)


def test_codex_terminal_turn_failed_fails_at_exit_zero():
    """turn.failed event marks envelope failed and records error even when process exit_code is 0."""
    stdout = "\n".join([
        json.dumps({"type": "thread.started", "thread_id": "thread-fail"}),
        json.dumps({"type": "turn.started"}),
        json.dumps({
            "type": "turn.failed", "turn_id": "turn-1",
            "error": {"message": "context window limit exceeded; turn terminated"},
        }),
    ])
    env = envelope_streams.parse_codex(
        stdout_text=stdout, stderr_text="", exit_code=0, invocation="inv-turn-failed", result_path=None
    )
    assert not env.ok and env.status == "failed"
    assert any("turn failed" in e and "context window limit" in e for e in env.errors)


def test_codex_terminal_top_level_error_fails_at_exit_zero():
    """Top-level error event marks envelope failed even if process exit code is 0."""
    stdout = json.dumps({"type": "error", "error": {"message": "unrecoverable protocol corruption"}})
    env = envelope_streams.parse_codex(
        stdout_text=stdout, stderr_text="", exit_code=0, invocation="inv-error", result_path=None
    )
    assert not env.ok and env.status == "failed"
    assert any("codex reported error" in e and "unrecoverable protocol" in e for e in env.errors)


def test_codex_item_level_notice_is_warning_not_failure():
    """item.completed error notice (e.g. model metadata fallback) is an advisory warning, not a fatal failure."""
    stdout = "\n".join([
        json.dumps({"type": "thread.started", "thread_id": "thread-notice"}),
        json.dumps({"type": "item.completed", "item": {
            "id": "item_0", "type": "error",
            "message": "Model metadata for `qwen3.8-max` not found. Defaulting to fallback metadata...",
        }}),
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "Task succeeded."}}),
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 50}}),
    ])
    env = envelope_streams.parse_codex(
        stdout_text=stdout, stderr_text="", exit_code=0, invocation="inv-notice", result_path=None
    )
    assert env.ok and env.status == "completed" and env.errors == []
    assert any("item notice" in w and "Model metadata" in w for w in env.warnings)
    assert len(env.usage_events) == 1


def test_codex_legacy_payload_shape_unchanged():
    """Existing session_meta, thread_settings_applied, and token_count payload parsing remains 100% compatible."""
    stdout = "\n".join([
        json.dumps({"type": "session_meta",
                    "payload": {"cli_version": "0.140.0", "model_provider": "openai", "session_id": "sess-1"}}),
        json.dumps({"payload": {"type": "thread_settings_applied",
                                "thread_settings": {"model": "gemini-1.5-pro", "reasoning_effort": "high"}}}),
        json.dumps({"payload": {"type": "token_count", "info": {
            "total_token_usage": {"input_tokens": 500, "output_tokens": 120,
                                  "cached_input_tokens": 100, "reasoning_output_tokens": 40},
        }}}),
    ])
    env = envelope_streams.parse_codex(
        stdout_text=stdout, stderr_text="", exit_code=0, invocation="inv-legacy", result_path=None
    )
    assert env.ok and env.status == "completed"
    assert env.identity == {
        "harness_version": "0.140.0", "provider": "openai", "session_id": "sess-1",
        "model": "gemini-1.5-pro", "effort": "high",
    }
    assert len(env.usage_events) == 1
    assert env.usage_events[0]["counters"] == {
        "input_tokens": 500, "output_tokens": 120, "cache_read_tokens": 100, "thinking_tokens": 40,
    }


def test_replay_sanitized_pilot_stream():
    """Replay a checked-in synthetic stream in the pilot's event shape; measured counters match turn.completed."""
    sanitized_stdout = "\n".join([
        json.dumps({"type": "thread.started", "thread_id": "00000000-0000-4000-8000-000000000001"}),
        json.dumps({"type": "item.completed", "item": {
            "id": "item_0", "type": "error",
            "message": "Model metadata for `qwen3.8-max` not found. Defaulting to fallback metadata; "
                       "this can degrade performance and cause issues.",
        }}),
        json.dumps({"type": "turn.started"}),
        json.dumps({"type": "item.completed", "item": {
            "id": "item_1", "type": "agent_message",
            "text": "Completed implementation and verified duration parser tests pass cleanly.",
        }}),
        json.dumps({"type": "turn.completed", "usage": {
            "input_tokens": 320000, "cached_input_tokens": 288000,
            "output_tokens": 13000, "reasoning_output_tokens": 7500,
        }}),
    ])
    env = envelope_streams.parse_codex(
        stdout_text=sanitized_stdout, stderr_text="", exit_code=0, invocation="pilot-sanitized-inv", result_path=None
    )
    assert env.ok and env.status == "completed"
    assert env.identity.get("session_id") == "00000000-0000-4000-8000-000000000001"
    assert env.narrative == "Completed implementation and verified duration parser tests pass cleanly."
    assert any("item notice" in w for w in env.warnings)
    assert len(env.usage_events) == 1

    event = env.usage_events[0]
    assert event["origin"] == "native-measured" and event["scope"] == "session"
    assert event["mode"] == "cumulative" and event["sequence"] == 1
    assert event["counters"] == {
        "input_tokens": 320000, "cache_read_tokens": 288000,
        "output_tokens": 13000, "thinking_tokens": 7500,
    }

    summary = usage.summarize(env.usage_events)
    assert summary["measured_fields"] == {
        "input_tokens": 320000, "cache_read_tokens": 288000,
        "output_tokens": 13000, "thinking_tokens": 7500,
    }
    assert summary["origin_breakdown"]["native-measured"] == 1
    assert summary["unknown"] == []


def test_replay_raw_pilot_stream_opt_in():
    """Opt-in full raw replay when CORRAL_PILOT_HARNESS_STDOUT is explicitly provided in the environment."""
    raw_env = os.environ.get("CORRAL_PILOT_HARNESS_STDOUT")
    if not raw_env:
        pytest.skip("CORRAL_PILOT_HARNESS_STDOUT not set; skipping private raw artifact replay")
    raw_path = Path(raw_env)
    assert raw_path.is_file(), f"Configured CORRAL_PILOT_HARNESS_STDOUT does not exist: {raw_path}"

    stdout = raw_path.read_text(encoding="utf-8")
    env = envelope_streams.parse_codex(
        stdout_text=stdout, stderr_text="", exit_code=0, invocation="pilot-replay-inv", result_path=None
    )
    # A captured stream is private to the run that produced it, so only its shape is checked.
    assert env.ok and env.status == "completed"
    assert env.identity.get("session_id")
    assert len(env.usage_events) == 1
    assert isinstance(env.usage_events[0]["counters"]["input_tokens"], int)
