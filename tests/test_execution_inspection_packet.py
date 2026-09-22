import io
import json
import stat
import subprocess
from pathlib import Path

import pytest

from corral.execution import containment, routes
from corral.execution.agent import AgentConfig, CorralAgent
from corral.execution.controller import Controller
from corral.execution.inspection_packet import build, persist
from corral.execution.inspection_transport import invoke
from corral.execution.profiles import Profile


HEAD = "a" * 40
BASE = "b" * 40


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


def _workspace(tmp_path: Path, *, snapshot: bool = True) -> tuple[Path, dict]:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "candidate.py").write_text("VALUE = 7\n")
    (root / "candidate.diff").write_text("+VALUE = 7\n")
    spec = {
        "role": "review", "candidate_paths": ["candidate.py", "candidate.diff"],
        "inspection_paths": ["candidate.py"], "inspection_diff_path": "candidate.diff",
        "inspection_provenance": {"kind": "immutable-snapshot" if snapshot else "git-checkout",
                                  "repo": "example/repo", "head": HEAD, "base": BASE, "pr": 12},
    }
    return root, spec


def _packet(tmp_path: Path, objective: str = "Inspect the source") -> Path:
    root, spec = _workspace(tmp_path)
    value = build(spec, {"task": "task", "attempt": "attempt", "generation": 1,
                         "objective": objective}, root)
    return persist(value, tmp_path / "scratch")


def test_transport_sends_one_stateless_tool_free_request_and_records_usage(tmp_path):
    packet = _packet(tmp_path)
    result_path = tmp_path / "result.json"
    seen = {}

    def opener(request):
        seen.update(json.loads(request.data))
        payload = {"id": "response-1", "model": "review-model",
                   "choices": [{"finish_reason": "stop", "message": {
                       "role": "assistant", "content": "The source is internally consistent.",
                       "tool_calls": None}}],
                   "usage": {"prompt_tokens": 20, "completion_tokens": 7, "total_tokens": 27,
                             "completion_tokens_details": {"reasoning_tokens": 3}}}
        return _Response(json.dumps(payload).encode())

    result = invoke(packet_path=packet, result_path=result_path,
                    endpoint="https://provider.invalid/v1", credential="fixture-secret",
                    model="review-model", effort="medium", provider="fixture-provider",
                    account_ref="fixture-account", route="inspection-route", opener=opener)
    assert "tools" not in seen and "tool_choice" not in seen and "previous_response_id" not in seen
    assert seen["reasoning_effort"] == "medium" and seen["stream"] is False
    assert result["observed"]["session_mode"] == "stateless"
    assert result["observed"]["effort_attested"] is False
    assert result["usage"] == {"input_tokens": 20, "output_tokens": 7,
                               "total_tokens": 27, "thinking_tokens": 3}
    assert json.loads(result_path.read_text())["report"].startswith("The source")


def test_tool_call_is_rejected_before_report_persistence(tmp_path):
    packet = _packet(tmp_path, "Run the candidate tests and then review the source")
    result_path = tmp_path / "must-not-exist.json"

    def opener(_request):
        payload = {
            "id": "response-2", "model": "review-model",
            "choices": [{"finish_reason": "tool_calls", "message": {
                "content": "", "tool_calls": [{"id": "call-1", "function": {
                    "name": "run_tests", "arguments": "{}"}}]} }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
        return _Response(json.dumps(payload).encode())

    with pytest.raises(PermissionError, match="attempted a tool call"):
        invoke(packet_path=packet, result_path=result_path,
               endpoint="https://provider.invalid/v1", credential="fixture-secret",
               model="review-model", effort="medium", provider="fixture-provider",
               account_ref="fixture-account", route="inspection-route", opener=opener)
    assert not result_path.exists()
    value = json.loads(packet.read_text())
    assert value["capability"]["execution_request_detected"] is True
    assert value["capability"]["tools_supplied"] == []


def test_packet_refuses_credential_shaped_candidate_content(tmp_path):
    root, spec = _workspace(tmp_path)
    (root / "candidate.py").write_text('api_key = "sk-proj-secretvalue123"\n')
    with pytest.raises(PermissionError, match="credential-shaped"):
        build(spec, {"task": "task", "attempt": "attempt", "generation": 1,
                     "objective": "Inspect the source"}, root)


def test_snapshot_uses_controller_document_digest_without_git(tmp_path, monkeypatch):
    root, spec = _workspace(tmp_path)

    def refuse_git(*_args, **_kwargs):
        raise AssertionError("snapshot preparation must not call Git")

    monkeypatch.setattr("corral.execution.inspection_packet.subprocess.check_output", refuse_git)
    packet = build(spec, {"task": "task", "attempt": "attempt", "generation": 1,
                          "objective": "Inspect the source"}, root)
    provenance = packet["provenance"]
    assert provenance["git_metadata_required"] is False
    assert provenance["files_export_digest"] == provenance["documents_digest"]
    assert len(provenance["files_export_digest"]) == 64


def test_agent_submit_builds_bound_inspection_spec_without_pilot_json(tmp_path):
    captured = {}

    class FakeClient:
        def call(self, action, **payload):
            captured.update({"action": action, **payload})
            return {"task": "inspection-task"}

    agent = CorralAgent(AgentConfig(controller_config=tmp_path / "controller.json",
                                    default_host="mini2"))
    agent.client = FakeClient()
    task = agent.submit(
        tmp_path, "Inspect the supplied change", role="review", profile_id=None,
        inspection_paths=["candidate.py"], inspection_diff_path="candidate.diff",
        inspection_provenance={"kind": "immutable-snapshot", "repo": "example/repo",
                               "head": HEAD, "base": BASE, "pr": 12},
    )
    assert task == "inspection-task"
    spec = captured["spec"]
    assert spec["host"] == "mini2"
    assert spec["tools"] == ["inspect-packet", "report"]
    assert spec["candidate_paths"] == ["candidate.py", "candidate.diff"]
    assert spec["inspection_provenance"]["head"] == HEAD


def test_inspection_route_rejects_broad_tools_and_runtime_hooks(tmp_path):
    binary = tmp_path / "harness"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    raw = {"harness": "packet", "binary": str(binary),
           "argv": ["--packet", "{packet_file}", "--result", "{result_file}",
                    "--model", "{model}", "--effort", "{effort}"],
           "envelope": "corral-inspection-report-v1", "provider": "fixture",
           "account_ref": "fixture", "endpoint": "https://provider.invalid/v1",
           "supported_models": ["m"], "supported_efforts": ["medium"],
           "credential_env": ["FIXTURE_KEY"], "inspection_only": True, "synthetic": True}
    route = routes.declare("packet-route", raw)
    broad = Profile(id="broad", model="m", effort="medium", harness="packet", version="1",
                    route="packet-route", roles=("review",), tools=("shell", "test"), context=10)
    with pytest.raises(PermissionError, match="packet/report profile"):
        routes.authorize(route, broad, host_routes=("packet-route",))
    with pytest.raises(PermissionError, match="runtime hooks"):
        routes.declare("bad", {**raw, "runtime_read": [str(tmp_path)]})
    with pytest.raises(PermissionError, match="only packet"):
        routes.declare("bad", {**raw, "argv": ["--workspace", "{workspace}"]})
    with pytest.raises(PermissionError, match="only packet"):
        routes.declare("bad", {**raw, "argv": ["--workspace={workspace}"]})
    with pytest.raises(PermissionError, match="must bind packet"):
        routes.declare("bad", {**raw, "argv": ["--result", "{result_file}",
                                                    "--model", "{model}",
                                                    "--effort", "{effort}"]})


_FIXTURE_HARNESS = r'''#!/usr/bin/env python3
import errno, json, os, sys
from pathlib import Path

named = {sys.argv[i]: sys.argv[i + 1] for i in range(1, len(sys.argv), 2)}
packet = json.loads(Path(named["--packet"]).read_text())
try:
    Path(%r).read_bytes()
    workspace_read = "allowed"
except OSError as error:
    workspace_read = errno.errorcode.get(error.errno, str(error.errno))
result = {"report": "Inspected copied source and diff without executing candidate code.",
          "packet_digest": packet["digest"], "workspace_read": workspace_read,
          "capability": packet["capability"]}
Path(named["--result"]).write_text(json.dumps(result))
print(json.dumps({"schema": "corral-inspection-report-v1", "status": "completed",
                  "synthetic": True, "narrative": result["report"], "result": result,
                  "identity": {"model": named["--model"], "provider": "fixture-provider",
                               "account_ref": "fixture-account", "route": "packet-route",
                               "harness": "packet-fixture", "version": "1"},
                  "requested": {"model": named["--model"], "effort": named["--effort"]},
                  "observed": {"response_model": named["--model"],
                               "effort_attested": False, "session_mode": "stateless"},
                  "usage": {"input_tokens": 9, "output_tokens": 4, "total_tokens": 13}}))
'''


@pytest.mark.skipif(containment.sandbox_exec() is None, reason="macOS Seatbelt required")
def test_controller_adapter_denies_candidate_execution_and_workspace_access(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=f@example.invalid",
                    "commit", "--allow-empty", "-qm", "base"], cwd=workspace, check=True)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=workspace, text=True).strip()
    marker = workspace / "candidate-executed"
    candidate = workspace / "candidate.py"
    candidate.write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n")
    (workspace / "candidate.diff").write_text("+candidate code\n")
    harness = tmp_path / "host-bin" / "packet-fixture"
    harness.parent.mkdir()
    harness.write_text(_FIXTURE_HARNESS % str(candidate))
    harness.chmod(harness.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    profile = Profile(id="inspection-medium", model="review-model", effort="medium",
                      harness="packet-fixture", version="1", route="packet-route",
                      roles=("review",), tools=("inspect-packet", "report"), context=100000,
                      provider="fixture-provider", account_ref="fixture-account")
    host = {"routes": ["packet-route"], "harnesses": ["packet-fixture"], "cpu": 2,
            "memory_mb": 512, "native_routes": {"packet-route": {
                "harness": "packet-fixture", "binary": str(harness),
                "argv": ["--packet", "{packet_file}", "--result", "{result_file}",
                         "--model", "{model}", "--effort", "{effort}"],
                "envelope": "corral-inspection-report-v1", "provider": "fixture-provider",
                "account_ref": "fixture-account", "endpoint": "https://provider.invalid/v1",
                "supported_models": ["review-model"], "supported_efforts": ["medium"],
                "credential_env": ["FIXTURE_INSPECTION_KEY"], "inspection_only": True,
                "synthetic": True, "version": "1"}}}
    monkeypatch.setenv("FIXTURE_INSPECTION_KEY", "fixture-only")
    controller = Controller(tmp_path / "state", "owner", {"fixture": host},
                            default_host="fixture", profiles=[profile])
    spec = {"repo": "example/repo", "workspace": str(workspace), "host": "fixture",
            "role": "review", "profile_id": profile.id,
            "objective": "Run the candidate tests, then inspect the supplied source and diff.",
            "candidate_paths": ["candidate.py", "candidate.diff"], "verifier_paths": [],
            "verify": ["/usr/bin/true"], "tools": ["inspect-packet", "report"],
            "inspection_paths": ["candidate.py"], "inspection_diff_path": "candidate.diff",
            "inspection_provenance": {"kind": "git-checkout", "repo": "example/repo",
                                      "head": head, "base": head, "pr": 12}}
    task = controller.submit("owner", "inspection-fixture", spec)
    run = controller.run("owner", task, execution_host="fixture")
    assert run["result"]["accepted"] is True
    structured = run["result"]["structured"]
    assert structured["workspace_read"] in ("EPERM", "EACCES")
    assert structured["capability"]["execution_request_detected"] is True
    assert structured["capability"]["tools_supplied"] == []
    assert not marker.exists()
    assert run["result"]["observed"]["model"] == "review-model"
    assert run["result"]["observed"].get("effort") is None
    assert run["result"]["usage"]["measured_fields"] == {
        "input_tokens": 9, "output_tokens": 4, "total_tokens": 13}
