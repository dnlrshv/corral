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
              "turn_usages": [{"input_tokens": 12}], "notes": (f"Bearer {FAKE}", None),
              f"password={FAKE}": "echoed as a key"}
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


@pytest.mark.skipif(containment.sandbox_exec() is None,
                    reason="real worker containment requires macOS sandbox-exec")
def test_rule_text_in_errors_is_redacted_like_other_diagnostics(tmp_path, monkeypatch):
    import yaml
    from corral.execution import prelaunch_refusal
    env = ns.native_env(tmp_path)
    monkeypatch.setenv("CORRAL_OVERLAY_FILE", str(tmp_path / "absent-overlay"))
    rules_name = f"api_key={FAKE}.yaml"
    (env["workspace"] / "corral.yaml").write_text(f"executor_brief: {{rules_file: {rules_name}}}")
    (env["workspace"] / rules_name).write_text(yaml.safe_dump({"schema_version": 1, "rules": [
        {"id": "project.test", "text": f"api_key={FAKE}", "reason": "Fixture credential refusal."}]}))
    controller = env["controller"]
    task = controller.submit("owner", "rule-diagnostic", ns.native_spec(env))
    with pytest.raises(PermissionError) as refusal:
        controller.run("owner", task, execution_host=ns.FAKE_HOST)
    assert FAKE in str(refusal.value)
    state = controller.store.get("state", task)
    assert "credential-shaped rule text refused" in state["reason"]
    assert FAKE not in json.dumps(state)
    with controller.store.transaction() as db:
        assert prelaunch_refusal.prove_db(db, task, 1, "workspace:" + str(env["workspace"].resolve()))
