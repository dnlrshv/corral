"""Reconciliation re-runs a native verifier inside the dispatch's verifier boundary."""
from __future__ import annotations

import dataclasses
import json
import subprocess
import sys
from pathlib import Path

import pytest

from corral.execution import containment, recovery, verifier, verifier_containment
from corral.execution.controller import Controller
from corral.execution.profiles import Profile

from . import native_support as ns
from .test_execution_regressions import bind_interrupted_attempt

STOPPED = {"execution_stopped": True, "effects_reconciled": True}
CORRECT = "def add(a, b):\n    return a + b\n"


def _secret_file(tmp_path: Path) -> Path:
    path = tmp_path / "provider-secrets.json"
    path.write_text(json.dumps({"FIXTURE_PROVIDER_KEY": "fixture-only"}))
    path.chmod(0o600)
    return path


def _interrupted_native(tmp_path, *, verify=None, extra_host=None):
    """An interrupted native attempt whose dispatcher handed it over for reconciliation."""
    env = ns.native_env(tmp_path, extra_host=extra_host)
    secret = _secret_file(tmp_path)
    controller = Controller(env["state"], "owner", {ns.FAKE_HOST: env["host"]},
                            default_host=ns.FAKE_HOST, profiles=[env["profile"]],
                            secret_env=str(secret))
    spec = ns.native_spec(env, ops=[])
    if verify is not None:
        spec["verify"] = verify
    task = controller.submit("owner", "interrupted-native", spec)
    resource = "workspace:" + str(env["workspace"].resolve())
    epoch = controller.store.acquire(resource, task)
    controller.store.transition_owner(resource, task, epoch, "uncertain")
    workspace = str(env["workspace"])
    policy = verifier.policy(controller.context(task), workspace,
                             host=env["host"], worker_writable=(workspace,))
    # Dispatch persisted the verifier bundle digest before launching the worker.
    state = {"status": "uncertain", "attempt": "native-attempt", "epoch": epoch, "generation": 1,
             "verifier_bundle": verifier.bound_bundle(policy, workspace)}
    bind_interrupted_attempt(controller, task, state)
    controller.store.replace("state", task, state)
    (env["workspace"] / "math_ops.py").write_text(CORRECT)
    return controller, task, env, secret, resource


@pytest.mark.parametrize("network", [False, True], ids=["default-deny", "host-opt-in"])
def test_native_reconciliation_verifier_gets_the_dispatch_boundary(tmp_path, monkeypatch, network):
    controller, task, env, secret, resource = _interrupted_native(
        tmp_path, extra_host={"verifier_network": network} if network else None)
    prepared_calls, executed = [], []
    scratch = tmp_path / "verifier-scratch"
    scratch.mkdir()
    boundary = containment.Boundary(workspace=str(env["workspace"]), scratch=str(scratch),
                                    tmpdir=str(scratch), deny=(), network=network)

    def prepare(**kwargs):
        prepared_calls.append(kwargs)
        return verifier_containment.Prepared(boundary=boundary, profile="(fixture-profile)",
                                             evidence={"passed": True, "fixture": True})

    def execute(policy, workspace, **kwargs):
        executed.append(kwargs)
        return verifier.Receipt(payload={"exit_code": 0, "unchanged": True, "policy_ok": True,
                                         "verifier_intact": None,
                                         "verifier_containment": kwargs.get("containment_evidence")})

    monkeypatch.setattr(verifier_containment, "prepare", prepare)
    monkeypatch.setattr(verifier, "execute", execute)
    recovery.reconcile(controller, "owner", task, lambda *_: STOPPED)

    [call] = prepared_calls
    # The provider secret file and every host-protected path are denied, as at dispatch.
    assert str(secret.resolve()) in call["protected_paths"]
    assert {str(env["secrets"]), str(env["fake_home"])} <= set(call["protected_paths"])
    assert call["network"] is network
    assert Path(call["state_dir"]) == controller.store.path.parent
    [run] = executed
    assert run["seatbelt_profile"] == "(fixture-profile)"
    assert run["containment_evidence"] == {"passed": True, "fixture": True}
    assert run["containment_env"] == {"TMPDIR": str(scratch), "HOME": str(scratch)}


def test_native_reconciliation_without_containment_runs_nothing(tmp_path, monkeypatch):
    controller, task, _env, _secret, resource = _interrupted_native(tmp_path)

    def unavailable(**_kwargs):
        raise PermissionError("verifier containment unavailable on this host")

    def must_not_run(*_args, **_kwargs):
        raise AssertionError("a native verifier ran without its containment")

    monkeypatch.setattr(verifier_containment, "prepare", unavailable)
    monkeypatch.setattr(verifier, "execute", must_not_run)
    with pytest.raises(PermissionError, match="containment unavailable"):
        recovery.reconcile(controller, "owner", task, lambda *_: STOPPED)
    assert controller.store.get("result", task) is None
    assert controller.store.ownership(resource)[2] == "uncertain"


def test_inspection_only_reconciliation_refuses_to_execute_candidate_code(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=f@example.invalid",
                    "commit", "--allow-empty", "-qm", "fixture"], cwd=workspace, check=True)
    profile = Profile(id="inspection-medium", model="review-model", effort="medium",
                      harness="corral-inspection-packet", version="1", route="packet-route",
                      roles=("review",), tools=("inspect-packet", "report"), context=100000,
                      provider="fixture-provider", account_ref="fixture-account")
    host = {"routes": ["packet-route"], "harnesses": ["corral-inspection-packet"], "cpu": 2,
            "memory_mb": 512, "native_routes": {"packet-route": {
                "harness": "corral-inspection-packet", "binary": str(tmp_path / "packet"),
                "argv": ["--packet", "{packet_file}", "--result", "{result_file}",
                         "--model", "{model}", "--effort", "{effort}"],
                "envelope": "corral-inspection-report-v1", "provider": "fixture-provider",
                "account_ref": "fixture-account", "endpoint": "https://provider.invalid/v1",
                "supported_models": ["review-model"], "supported_efforts": ["medium"],
                "credential_env": ["FIXTURE_INSPECTION_KEY"], "inspection_only": True,
                "synthetic": True, "version": "1"}}}
    controller = Controller(tmp_path / "state", "owner", {"fixture": host},
                            default_host="fixture", profiles=[profile])
    marker = workspace / "candidate-executed"
    task = "inspection-task"
    controller.store.put_once("request", task, {
        "repo": "fixture", "workspace": str(workspace), "host": "fixture", "endpoint": "local",
        "candidate_paths": [], "verifier_paths": [], "role": "review",
        "verify": [sys.executable, "-c", f"open({str(marker)!r}, 'w').write('ran')"],
        "selection": {"profile": dataclasses.asdict(profile)}})
    controller.store.replace("state", task, {"status": "uncertain", "attempt": "a", "epoch": 1,
                                             "generation": 1})
    monkeypatch.setattr(verifier, "execute", lambda *_a, **_k: pytest.fail("candidate executed"))
    with pytest.raises(PermissionError, match="inspection-only tasks never execute candidate code"):
        recovery.reconcile(controller, "owner", task, lambda *_: STOPPED)
    assert not marker.exists()
    assert controller.store.get("result", task) is None


@pytest.mark.skipif(containment.sandbox_exec() is None,
                    reason="real verifier containment requires macOS sandbox-exec")
def test_real_reconciliation_verifier_cannot_read_the_provider_secret(tmp_path):
    secret = tmp_path / "provider-secrets.json"
    probe = tmp_path / "verifiers" / "check_secret_denied.py"
    probe.parent.mkdir(parents=True)
    probe.write_text(
        "import sys\n"
        "try:\n"
        f"    open({str(secret)!r}).read()\n"
        "except PermissionError:\n"
        "    print('provider secret: denied')\n"
        "else:\n"
        "    print('provider secret: READ')\n"
        "    sys.exit(3)\n"
        "namespace = {}\n"
        "exec(compile(open(sys.argv[1]).read(), sys.argv[1], 'exec'), namespace)\n"
        "assert namespace['add'](2, 3) == 5\n")
    controller, task, env, created, _resource = _interrupted_native(
        tmp_path, verify=[sys.executable, str(probe), "math_ops.py"])
    assert created == secret
    result = recovery.reconcile(controller, "owner", task, lambda *_: STOPPED)
    directory = Path(result["artifact_directory"])
    assert (directory / "recovery-verify.stdout").read_text().strip() == "provider secret: denied"
    assert result["accepted"] is True
    evidence = result["receipt"]["verifier_containment"]
    assert evidence["passed"] is True
    assert "network*" in evidence["contained_operations"]
