"""Harness-derived diagnostics are redacted before the adapter or controller persists them.

The synthetic harness echoes a fake credential through its envelope status, which the
parser quotes into an error. Nothing is monkeypatched and no provider is contacted.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from corral.execution import containment, continuation
from corral.redaction import redact_nested_text

from . import native_support as ns

FAKE = "NotARealSecretValue"


def test_nested_diagnostics_keep_structure_and_counters():
    detail = {"exit_code": 1, "stderr_tail": f"auth failed: api_key={FAKE}",
              "turn_usages": [{"input_tokens": 12}], "notes": [f"Bearer {FAKE}", None]}
    redacted = redact_nested_text(detail)
    assert FAKE not in json.dumps(redacted)
    assert redacted["exit_code"] == 1 and redacted["turn_usages"] == [{"input_tokens": 12}]
    assert redacted["notes"][1] is None


@pytest.mark.skipif(containment.sandbox_exec() is None,
                    reason="real worker containment requires macOS sandbox-exec")
def test_harness_credential_echo_is_redacted_in_adapter_and_controller_records(tmp_path):
    env = ns.native_env(tmp_path)
    controller = env["controller"]
    ops = [f"result {ns.b64(json.dumps({'answer': 5}))}", f"status failed api_key={FAKE}"]
    task = controller.submit("owner", "credential-echo", ns.native_spec(env, ops=ops))
    run = controller.run("owner", task, execution_host=ns.FAKE_HOST)

    errors = run["result"]["adapter_errors"]
    assert any("is not a completion" in item and "[REDACTED]" in item for item in errors)
    artifacts = Path(run["result"]["artifact_directory"])
    for name in ("adapter-result.json", "attempts.jsonl"):
        assert FAKE not in (artifacts / name).read_text()
    stored = controller.store.get(continuation.RESULT_KIND, task)
    assert FAKE not in json.dumps([stored["adapter_errors"], stored["adapter_warnings"]])
