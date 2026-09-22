import json
import stat
from pathlib import Path

import pytest

from corral.execution import containment

from . import native_support as ns

requires_sandbox = pytest.mark.skipif(
    containment.sandbox_exec() is None,
    reason="native worker containment requires macOS sandbox-exec",
)


@requires_sandbox
def test_telemetry_highlevel_qwen_codex(tmp_path):
    env = ns.native_env(tmp_path, envelope="codex-jsonl-v1")
    controller = env["controller"]

    binary = env["binary"]
    script = """#!/usr/bin/env python3
import sys, json
import argparse
parser = argparse.ArgumentParser()
parser.add_argument("--prompt")
parser.add_argument("--model")
parser.add_argument("--effort")
parser.add_argument("--workspace")
parser.add_argument("--scratch")
parser.add_argument("--result")
args, _ = parser.parse_known_args()

with open(args.workspace + "/math_ops.py", "w") as f:
    f.write("def add(a, b):\\n    return a + b\\n")
with open(args.result, "w") as f:
    f.write(json.dumps({"structured": {"answer": 5, "changed": ["math_ops.py"]}}))

sys.stdout.write('{"type":"thread_settings_applied","payload":{"thread_settings":{"model":"qwen3.8-max","reasoning_effort":"high","model_provider_id":"baba"}}}\\n')
sys.stdout.write('{"type":"turn.completed","usage":{"input_tokens":325497,"cached_input_tokens":292352,"output_tokens":13206,"reasoning_output_tokens":7567}}\\n')
sys.stdout.flush()
"""
    binary.write_text(script)
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)

    spec = ns.native_spec(env)
    task = controller.submit("owner", "telemetry-qwen-codex", spec)
    run = controller.run("owner", task, execution_host=ns.FAKE_HOST)
    result = run["result"]
    assert result["accepted"] is True
    usage = result["usage"]
    # Qwen mapping: cached_input_tokens -> cache_read_tokens
    assert usage["measured_fields"]["input_tokens"] == 325497
    assert usage["measured_fields"]["cache_read_tokens"] == 292352
    assert usage["measured_fields"]["output_tokens"] == 13206
    assert usage["measured_fields"]["thinking_tokens"] == 7567

    # Repeated polling/replay unchanged
    run2 = controller.run("owner", task, execution_host=ns.FAKE_HOST)
    assert run2["result"] == run["result"]

@requires_sandbox
def test_telemetry_highlevel_gemini_agy(tmp_path):
    env = ns.native_env(tmp_path, envelope="agy-json-v1")
    controller = env["controller"]

    binary = env["binary"]
    script = """#!/usr/bin/env python3
import sys, json
import argparse
parser = argparse.ArgumentParser()
parser.add_argument("--prompt")
parser.add_argument("--workspace")
parser.add_argument("--result")
args, _ = parser.parse_known_args()

with open(args.workspace + "/math_ops.py", "w") as f:
    f.write("def add(a, b):\\n    return a + b\\n")
with open(args.result, "w") as f:
    f.write(json.dumps({"structured": {"answer": 5, "changed": ["math_ops.py"]}}))

envelope = {
    "status": "SUCCESS",
    "conversation_id": "gemini-session-123",
    "usage": {
        "input_tokens": 100,
        "output_tokens": 200,
        "thinking_tokens": 50,
        "cache_read_tokens": 0,
        "total_tokens": 300
    },
    "structured": {"answer": 5, "changed": ["math_ops.py"]}
}
print(json.dumps(envelope))
"""
    binary.write_text(script)
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)

    spec = ns.native_spec(env)
    task = controller.submit("owner", "telemetry-gemini-agy", spec)
    run = controller.run("owner", task, execution_host=ns.FAKE_HOST)
    result = run["result"]
    assert result["accepted"] is True
    usage = result["usage"]
    assert usage["measured_fields"]["input_tokens"] == 100
    assert usage["measured_fields"]["output_tokens"] == 200
    assert usage["measured_fields"]["thinking_tokens"] == 50

@requires_sandbox
def test_telemetry_highlevel_failed_attempt_and_repair(tmp_path):
    env = ns.native_env(tmp_path, envelope="agy-json-v1")
    controller = env["controller"]

    binary = env["binary"]
    script = """#!/usr/bin/env python3
import sys, json, argparse, os
from pathlib import Path
parser = argparse.ArgumentParser()
parser.add_argument("--prompt")
parser.add_argument("--workspace")
parser.add_argument("--result")
args, _ = parser.parse_known_args()

is_g2 = "-g2" in args.workspace or "-g2" in args.result

if not is_g2:
    envelope = {
        "status": "FAILED",
        "conversation_id": "gemini-session-failed-g1",
        "usage": {"input_tokens": 50, "output_tokens": 0},
        "structured": {}
    }
    print(json.dumps(envelope))
    sys.exit(1)
else:
    with open(args.workspace + "/math_ops.py", "w") as f:
        f.write("def add(a, b):\\n    return a + b\\n")
    with open(args.result, "w") as f:
        f.write(json.dumps({"structured": {"answer": 5, "changed": ["math_ops.py"]}}))

    envelope = {
        "status": "SUCCESS",
        "conversation_id": "gemini-session-failed-g2",
        "usage": {"input_tokens": 100, "output_tokens": 200},
        "structured": {"answer": 5, "changed": ["math_ops.py"]}
    }
    print(json.dumps(envelope))
    sys.exit(0)
"""
    binary.write_text(script)
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)

    spec = ns.native_spec(env)
    task = controller.submit("owner", "telemetry-repair", spec)

    # 1. Failed first attempt (generation 1)
    run1 = controller.run("owner", task, execution_host=ns.FAKE_HOST)
    assert run1["result"]["accepted"] is False
    assert run1["result"]["usage"]["measured_fields"]["input_tokens"] == 50
    inv1 = json.loads((Path(run1["result"]["artifact_directory"]) / "adapter-result.json").read_text())["attempt"]

    # 2. Repair (amendment schedules generation 2)
    controller.continue_task("owner", task, "amend-1", {"objective": "Fix it."})
    run2 = controller.run("owner", task, execution_host=ns.FAKE_HOST)
    assert run2["result"]["accepted"] is True
    assert run2["result"]["usage"]["measured_fields"]["input_tokens"] == 100
    assert run2["result"]["usage"]["measured_fields"]["output_tokens"] == 200
    inv2 = json.loads((Path(run2["result"]["artifact_directory"]) / "adapter-result.json").read_text())["attempt"]

    assert inv1 != inv2

    # Check dedup in Spool - failure + repair events are distinct per invocation
    events = controller.store.records("usage")
    usage_events = list(events.values())
    assert len(usage_events) == 2

    # Verify the separate events
    tokens = {e["counters"]["input_tokens"] for e in usage_events}
    assert tokens == {50, 100}

    # Replay second run unchanged
    run3 = controller.run("owner", task, execution_host=ns.FAKE_HOST)
    assert run3["result"] == run2["result"]

@requires_sandbox
def test_telemetry_outage_buffering(tmp_path):
    env = ns.native_env(tmp_path, envelope="agy-json-v1")
    controller = env["controller"]

    binary = env["binary"]
    script = """#!/usr/bin/env python3
import sys, json, argparse
parser = argparse.ArgumentParser()
parser.add_argument("--workspace")
parser.add_argument("--result")
args, _ = parser.parse_known_args()

with open(args.workspace + "/math_ops.py", "w") as f:
    f.write("def add(a, b):\\n    return a + b\\n")
with open(args.result, "w") as f:
    f.write(json.dumps({"structured": {"answer": 5, "changed": ["math_ops.py"]}}))

envelope = {
    "status": "SUCCESS",
    "conversation_id": "gemini-session-outage",
    "usage": {"input_tokens": 10, "output_tokens": 20},
    "structured": {"answer": 5, "changed": ["math_ops.py"]}
}
print(json.dumps(envelope))
"""
    binary.write_text(script)
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)

    # Simulate an outage in flush:
    import corral.execution.usage
    original_flush = corral.execution.usage.Spool.flush
    def mock_flush(self, target):
        raise OSError("Simulated outage")

    corral.execution.usage.Spool.flush = mock_flush

    spec = ns.native_spec(env)
    task = controller.submit("owner", "telemetry-outage", spec)
    run = controller.run("owner", task, execution_host=ns.FAKE_HOST)

    # Work is still accepted despite telemetry error!
    assert run["result"]["accepted"] is True
    # The usage should show "spooled" publication state
    assert run["result"]["usage"]["publication"] == "spooled"

    # Restore flush
    corral.execution.usage.Spool.flush = original_flush

def test_resume_reject_through_controller_public_api(tmp_path):
    env = ns.native_env(tmp_path, envelope="agy-json-v1")
    controller = env["controller"]

    spec = ns.native_spec(env)
    spec["resume"] = True

    # Should be rejected at submit or launch
    with pytest.raises(PermissionError) as exc:
        controller.submit("owner", "telemetry-resume", spec)
    assert "native session resume is unsupported" in str(exc.value)
