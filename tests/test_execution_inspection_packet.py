import hashlib
import io
import json
import stat
import subprocess
from pathlib import Path

import pytest

from corral.execution import containment, envelopes, native, routes, workspace_contract
from corral.execution.agent import AgentConfig, CorralAgent
from corral.execution.controller import Controller
from corral.execution.inspection_packet import bind_candidate, build, persist
from corral.execution.inspection_transport import invoke
from corral.execution.internal_review import record as record_internal_review
from corral.execution.profiles import Profile
from corral.execution.store import Store, digest
from corral.execution.workspace import file_digest


HEAD = "a" * 40
BASE = "b" * 40


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


def _register_export(root: Path, state_path: Path) -> tuple[dict, Store]:
    base_spec = {
        "role": "review", "candidate_paths": ["candidate.py", "candidate.diff"],
        "inspection_paths": ["candidate.py"], "inspection_diff_path": "candidate.diff",
        "workspace_kind": "immutable_snapshot", "inspection_pr": 12,
    }
    files = {name: {"digest": file_digest(root / name),
                    "mode": (root / name).stat().st_mode & 0o777}
             for name in base_spec["candidate_paths"]}
    core = {"repository": "example/repo", "pr_number": 12, "head": HEAD,
            "base": BASE, "policy_id": "policy-12", "policy_digest": "d" * 64,
            "workspace": str(root.resolve()), "selected_files": files,
            "selected_files_digest": digest(files), "diff_path": "candidate.diff",
            "diff_sha256": files["candidate.diff"]["digest"],
            "export_digest": digest(files), "auth_mode": "user-token"}
    export_id = digest(core)
    store = Store(state_path)
    store.put_once("trusted_export", export_id, {"export_id": export_id, **core})
    spec = workspace_contract.resolve_spec(
        store, {"role": "review", "trusted_export_id": export_id})
    return spec, store


def _workspace(tmp_path: Path, *, snapshot: bool = True) -> tuple[Path, dict, Store | None]:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "candidate.py").write_text("VALUE = 7\n")
    (root / "candidate.diff").write_text("+VALUE = 7\n")
    if snapshot:
        spec, store = _register_export(root, tmp_path / "state.sqlite")
    else:
        spec = {"role": "review", "candidate_paths": ["candidate.py", "candidate.diff"],
                "inspection_paths": ["candidate.py"], "inspection_diff_path": "candidate.diff",
                "workspace_kind": "checkout", "inspection_pr": 12}
        store = None
    return root, spec, store


def _built(root: Path, spec: dict, objective: str, store: Store | None) -> dict:
    workspace_provenance = workspace_contract.preflight(spec, root, store=store)
    binding = bind_candidate(spec, root, workspace_provenance)
    context = {"task": "task", "attempt": "attempt", "generation": 1,
               "objective": objective, "workspace_provenance": workspace_provenance}
    return build(spec, context, root, binding)


def _packet(tmp_path: Path, objective: str = "Inspect the source") -> Path:
    root, spec, store = _workspace(tmp_path)
    value = _built(root, spec, objective, store)
    return persist(value, tmp_path / "scratch")


def test_transport_sends_one_stateless_tool_free_request_and_records_usage(tmp_path):
    packet = _packet(tmp_path)
    result_path = tmp_path / "result.json"
    seen = {}

    def opener(request):
        seen.update(json.loads(request.data))
        payload = {"id": "response-1", "model": "review-model",
                   "choices": [{"finish_reason": "stop", "message": {
                       "role": "assistant", "content": json.dumps({
                           "verdict": "PASS", "report": "The source is internally consistent."}),
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
    assert result["identity"] == {"model": "review-model", "harness": "inspection-packet-http",
                                  "version": "1"}
    assert result["observed"]["identity_coverage"] == {
        "model": "provider-response", "provider": "configured-https-endpoint-route",
        "account_ref": "configured-credential-reference", "route": "configured-route",
        "effort": "requested-not-attested", "harness": "local-transport"}
    assert result["usage"] == {"input_tokens": 20, "output_tokens": 7,
                               "total_tokens": 27, "thinking_tokens": 3}
    stored = json.loads(result_path.read_text())
    assert stored["verdict"] == "PASS"
    assert stored["report"].startswith("The source")


def test_transport_rejects_prose_only_verdict_before_report_persistence(tmp_path):
    packet = _packet(tmp_path)
    result_path = tmp_path / "must-not-exist.json"

    def opener(_request):
        payload = {"id": "response-prose", "model": "review-model",
                   "choices": [{"finish_reason": "stop", "message": {
                       "content": "PASS: looks good"}}],
                   "usage": {"prompt_tokens": 3, "completion_tokens": 2,
                             "total_tokens": 5}}
        return _Response(json.dumps(payload).encode())

    result = invoke(packet_path=packet, result_path=result_path,
                    endpoint="https://provider.invalid/v1", credential="fixture-secret",
                    model="review-model", effort="medium", provider="fixture-provider",
                    account_ref="fixture-account", route="inspection-route", opener=opener)
    assert result["status"] == "failed"
    assert "exact verdict/report JSON" in result["error"]
    assert result["observed"]["response_id"] == "response-prose"
    assert result["usage"] == {"input_tokens": 3, "output_tokens": 2,
                               "total_tokens": 5}
    assert not result_path.exists()


def test_tool_call_is_rejected_before_report_persistence(tmp_path):
    packet = _packet(tmp_path, "Run the candidate tests and then review the source")
    result_path = tmp_path / "must-not-exist.json"

    def opener(_request):
        payload = {
            "id": "response-2", "model": "review-model",
            "choices": [{"finish_reason": "tool_calls", "message": {
                "content": "", "tool_calls": [{"id": "call-1", "function": {
                    "name": "run_tests", "arguments": "{}"}}]} }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": -2,
                      "prompt_tokens_details": {"cached_tokens": True},
                      "completion_tokens_details": {"reasoning_tokens": -1}},
        }
        return _Response(json.dumps(payload).encode())

    result = invoke(packet_path=packet, result_path=result_path,
                    endpoint="https://provider.invalid/v1", credential="fixture-secret",
                    model="review-model", effort="medium", provider="fixture-provider",
                    account_ref="fixture-account", route="inspection-route", opener=opener)
    assert result["status"] == "failed"
    assert "attempted a tool call" in result["error"]
    assert result["observed"]["response_id"] == "response-2"
    assert result["usage"] == {"input_tokens": 1, "output_tokens": 1}
    assert not result_path.exists()
    value = json.loads(packet.read_text())
    assert value["capability"]["execution_request_detected"] is True
    assert value["capability"]["tools_supplied"] == []


def test_model_misroute_retains_failed_usage_and_observed_identity(tmp_path):
    packet = _packet(tmp_path)
    result_path = tmp_path / "must-not-exist.json"

    def opener(_request):
        return _Response(json.dumps({
            "id": "response-misroute", "model": "unexpected-model",
            "choices": [{"finish_reason": "stop", "message": {
                "content": "report"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
        }).encode())

    result = invoke(packet_path=packet, result_path=result_path,
                    endpoint="https://provider.invalid/v1", credential="fixture-secret",
                    model="review-model", effort="medium", provider="fixture-provider",
                    account_ref="fixture-account", route="inspection-route", opener=opener)
    assert result["status"] == "failed" and not result_path.exists()
    assert result["identity"]["model"] == "unexpected-model"
    assert result["observed"]["response_id"] == "response-misroute"
    parsed = envelopes.parse("corral-inspection-report-v1", stdout_text=json.dumps(result),
                             stderr_text="", exit_code=1, invocation="attempt",
                             synthetic_expected=False)
    assert parsed.identity["model"] == "unexpected-model"
    assert "provider" not in parsed.identity and "account_ref" not in parsed.identity
    assert parsed.usage_events[0]["counters"] == {
        "input_tokens": 5, "output_tokens": 2, "total_tokens": 7}
    assert parsed.detail["provider_receipt"]["response_id"] == "response-misroute"
    assert parsed.detail["identity_coverage"]["effort"] == "requested-not-attested"


def test_packet_refuses_credential_shaped_candidate_content(tmp_path):
    root, spec, store = _workspace(tmp_path)
    (root / "candidate.py").write_text('api_key = "sk-proj-secretvalue123"\n')
    spec, store = _register_export(root, tmp_path / "secret-state.sqlite")
    with pytest.raises(PermissionError, match="credential-shaped"):
        _built(root, spec, "Inspect the source", store)


def test_packet_refuses_credential_shaped_objective(tmp_path):
    root, spec, store = _workspace(tmp_path)
    with pytest.raises(PermissionError, match="credential-shaped inspection objective"):
        _built(root, spec, "Review this with api_key=sk-proj-secretvalue123", store)


def test_packet_refuses_file_change_after_controller_preflight(tmp_path):
    root, spec, store = _workspace(tmp_path)
    workspace_provenance = workspace_contract.preflight(spec, root, store=store)
    binding = bind_candidate(spec, root, workspace_provenance)
    (root / "candidate.py").write_text("VALUE = 8\n")
    with pytest.raises(PermissionError, match="changed after controller preflight"):
        build(spec, {"task": "task", "attempt": "attempt", "generation": 1,
                     "objective": "Inspect the source",
                     "workspace_provenance": workspace_provenance}, root, binding)


def test_trusted_export_rejects_caller_candidate_override_and_tampered_registry(tmp_path):
    root, spec, store = _workspace(tmp_path)
    with pytest.raises(PermissionError, match="cannot override controller fields"):
        workspace_contract.resolve_spec(
            store, {"role": "review", "trusted_export_id": spec["trusted_export_id"],
                    "workspace": str(root), "inspection_pr": 999})
    record = store.get("trusted_export", spec["trusted_export_id"])
    store.replace("trusted_export", spec["trusted_export_id"],
                  {**record, "head": "f" * 40})
    with pytest.raises(PermissionError, match="identity digest is invalid"):
        workspace_contract.resolve_spec(
            store, {"role": "review", "trusted_export_id": spec["trusted_export_id"]})


def test_checkout_head_is_rechecked_immediately_before_packet(tmp_path):
    root, spec, _store = _workspace(tmp_path, snapshot=False)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "remote", "add", "origin", "https://github.com/example/repo.git"],
                   cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=f@example.invalid",
                    "commit", "-qm", "candidate"], cwd=root, check=True)
    old_head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    spec["inspection_base_ref"] = old_head
    workspace_provenance = workspace_contract.preflight(spec, root)
    subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=f@example.invalid",
                    "commit", "--allow-empty", "-qm", "moved"], cwd=root, check=True)
    with pytest.raises(PermissionError, match="candidate binding is invalid"):
        bind_candidate(spec, root, workspace_provenance)


def test_snapshot_uses_controller_document_digest_without_git(tmp_path, monkeypatch):
    root, spec, store = _workspace(tmp_path)

    def refuse_git(*_args, **_kwargs):
        raise AssertionError("snapshot preparation must not call Git")

    monkeypatch.setattr("corral.execution.inspection_packet.subprocess.check_output", refuse_git)
    packet = _built(root, spec, "Inspect the source", store)
    provenance = packet["provenance"]
    assert provenance["git_metadata_required"] is False
    assert len(provenance["export_digest"]) == 64
    assert provenance["trusted_export_id"] == spec["trusted_export_id"]
    assert len(provenance["documents_digest"]) == 64


def test_agent_submit_builds_bound_inspection_spec_without_pilot_json(tmp_path):
    captured = {}

    class FakeClient:
        def call(self, action, **payload):
            captured.update({"action": action, **payload})
            return {"task": "inspection-task"}

    agent = CorralAgent(AgentConfig(controller_config=tmp_path / "controller.json",
                                    default_host="mini2"))
    agent.client = FakeClient()
    task = agent.submit_export("c" * 64, "Inspect the supplied change",
                               profile_id="inspection-medium")
    assert task == "inspection-task"
    spec = captured["spec"]
    assert spec["host"] == "mini2"
    assert spec == {"trusted_export_id": "c" * 64,
                    "objective": "Inspect the supplied change", "host": "mini2",
                    "role": "review", "tools": ["inspect-packet", "report"],
                    "profile_id": "inspection-medium"}


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
                    route="packet-route", roles=("review",), tools=("shell", "test"), context=10,
                    provider="fixture", account_ref="fixture")
    with pytest.raises(PermissionError, match="packet/report profile"):
        routes.authorize(route, broad, host_routes=("packet-route",))
    with pytest.raises(PermissionError, match="runtime hooks"):
        routes.declare("bad", {**raw, "runtime_read": [str(tmp_path)]})
    with pytest.raises(PermissionError, match="runtime hooks"):
        routes.declare("bad", {**raw, "runtime_write": [str(tmp_path)]})
    with pytest.raises(PermissionError, match="runtime hooks"):
        routes.declare("bad", {**raw, "runtime_write_files": [str(tmp_path / "history.jsonl")]})
    with pytest.raises(PermissionError, match="only packet"):
        routes.declare("bad", {**raw, "argv": [*raw["argv"], "--prompt", "{prompt}"]})
    with pytest.raises(PermissionError, match="only packet"):
        routes.declare("bad", {**raw, "argv": ["--workspace", "{workspace}"]})
    with pytest.raises(PermissionError, match="only packet"):
        routes.declare("bad", {**raw, "argv": ["--workspace={workspace}"]})
    with pytest.raises(PermissionError, match="must bind packet"):
        routes.declare("bad", {**raw, "argv": ["--result", "{result_file}",
                                                    "--model", "{model}",
                                                    "--effort", "{effort}"]})


def test_packet_boundary_allows_only_controller_derived_transport_modules(tmp_path):
    source = Path(__file__).resolve().parents[1]
    boundary, _scratch, _grants = native.build_boundary(
        workspace=str(tmp_path / "candidate"), state_dir=tmp_path / "state",
        artifacts=tmp_path / "artifacts", task_dir=tmp_path / "task",
        source_root=source, verifier_roots=(), host_protected=(), task_id="packet-self-code",
        packet_only=True,
    )
    allowed = {Path(item).resolve() for item in boundary.trusted_read_allow}
    expected = {
        source / "corral" / "__init__.py", source / "corral" / "execution" / "__init__.py",
        source / "corral" / "execution" / "inspection_transport.py",
        source / "corral" / "execution" / "inspection_packet.py",
        source / "corral" / "execution" / "store.py", source / "corral" / "execution" / "workspace.py",
        source / "corral" / "redaction.py",
    }
    assert allowed == {item.resolve() for item in expected}
    profile = containment.build_profile(boundary)
    assert f'(deny file-read* file-write* (subpath "{source}"))' in profile
    assert f'(allow file-read* (subpath "{source}"))' not in profile
    for item in allowed:
        assert f'(allow file-read* (literal "{item}"))' in profile
    metadata = {Path(item).resolve() for item in boundary.trusted_metadata_allow}
    assert metadata == {source, source / "corral", source / "corral" / "execution"}
    for item in metadata:
        assert f'(allow file-read-metadata (literal "{item}"))' in profile
        assert f'(allow file-read-metadata (subpath "{item}"))' in profile
    code_root = (source / "corral").resolve()
    assert boundary.trusted_read_roots == (str(code_root),)
    assert f'(allow file-read* (subpath "{code_root}"))' in profile


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
result = {"verdict": "PASS", "report": "Inspected copied source and diff without executing candidate code.",
          "packet_digest": packet["digest"], "workspace_read": workspace_read,
          "provenance": packet["provenance"], "capability": packet["capability"],
          "task": packet["task"], "attempt": packet["attempt"], "generation": packet["generation"]}
Path(named["--result"]).write_text(json.dumps(result))
print(json.dumps({"schema": "corral-inspection-report-v1", "status": "completed",
                  "synthetic": True, "narrative": result["report"], "result": result,
                  "identity": {"model": named["--model"], "provider": "fixture-provider",
                               "account_ref": "fixture-account", "route": "packet-route",
                               "harness": "inspection-packet-http", "version": "1"},
                  "requested": {"model": named["--model"], "effort": named["--effort"]},
                  "observed": {"response_model": named["--model"],
                               "effort_attested": False, "session_mode": "stateless"},
                  "usage": {"input_tokens": 9, "output_tokens": 4, "total_tokens": 13}}))
'''


@pytest.mark.skipif(containment.sandbox_exec() is None, reason="macOS Seatbelt required")
def test_controller_adapter_denies_candidate_execution_and_workspace_access(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    marker = workspace / "candidate-executed"
    candidate = workspace / "candidate.py"
    candidate.write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n")
    (workspace / "candidate.diff").write_text("+candidate code\n")
    harness = tmp_path / "host-bin" / "packet-fixture"
    harness.parent.mkdir()
    harness.write_text((_FIXTURE_HARNESS % str(candidate)).replace('"PASS"', '"CHANGES_REQUIRED"'))
    harness.chmod(harness.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    profile = Profile(id="inspection-medium", model="review-model", effort="medium",
                      harness="corral-inspection-packet", version="1", route="packet-route",
                      roles=("review",), tools=("inspect-packet", "report"), context=100000,
                      provider="fixture-provider", account_ref="fixture-account")
    host = {"routes": ["packet-route"], "harnesses": ["corral-inspection-packet"], "cpu": 2,
            "memory_mb": 512, "native_routes": {"packet-route": {
                "harness": "corral-inspection-packet", "binary": str(harness),
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
    export_spec, _store = _register_export(workspace, controller.store.path)
    epoch = controller.store.acquire("pr:example/repo#12", "corral")
    spec = {"trusted_export_id": export_spec["trusted_export_id"], "host": "fixture",
            "pr_owner": "corral", "pr_owner_epoch": epoch,
            "role": "review", "profile_id": profile.id,
            "objective": "Run the candidate tests, then inspect the supplied source and diff.",
            "tools": ["inspect-packet", "report"]}
    task = controller.submit("owner", "inspection-fixture", spec)
    run = controller.run("owner", task, execution_host="fixture")
    assert run["result"]["accepted"] is True
    assert run["result"]["structured"]["verdict"] == "CHANGES_REQUIRED"
    assert run["result"]["receipt"]["accepted"] is True
    assert run["result"]["receipt"]["verifier_executed"] is False
    validation = run["result"]["inspection_validation"]
    assert validation["accepted"] is True
    assert validation["verdict"] == "CHANGES_REQUIRED"
    key = f"{task}:{run['state']['attempt']}:g1"
    assert controller.store.get("inspection_validation", key) == validation
    structured = run["result"]["structured"]
    assert structured["workspace_read"] in ("EPERM", "EACCES")
    assert structured["capability"]["execution_request_detected"] is True
    assert structured["capability"]["tools_supplied"] == []
    assert not marker.exists()
    assert run["result"]["observed"]["model"] == "review-model"
    assert run["result"]["observed"].get("effort") is None
    assert run["result"]["usage"]["measured_fields"] == {
        "input_tokens": 9, "output_tokens": 4, "total_tokens": 13}
    internal = record_internal_review(controller.store, task)
    assert internal["verdict"] == "CHANGES_REQUIRED"
    assert internal["export_id"] == export_spec["trusted_export_id"]
    assert internal["identity"]["configured"]["provider"] == "fixture-provider"


def test_post_inference_recheck_refuses_mutated_packet_document(tmp_path):
    from corral.execution.inspection_packet import recheck_documents

    workspace = tmp_path / "candidate"
    workspace.mkdir()
    source = workspace / "candidate.py"
    source.write_text("answer = 1\n")
    record = {"documents": [{"path": "candidate.py", "sha256": hashlib.sha256(
        source.read_bytes()).hexdigest(), "bytes": source.stat().st_size}]}
    recheck_documents(record, workspace)
    source.write_text("answer = 2\n")
    with pytest.raises(PermissionError, match="changed after inference"):
        recheck_documents(record, workspace)
