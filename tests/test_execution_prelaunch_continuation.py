"""Continuation after a conclusively never-launched controller refusal."""
import hashlib
import json
import stat

import pytest

from corral.execution import containment
from corral.execution.controller import Controller
from corral.execution.profiles import Profile
from corral.execution.store import digest

from . import native_support as ns

needs_seatbelt = pytest.mark.skipif(containment.sandbox_exec() is None,
                                    reason="real worker containment requires macOS sandbox-exec")


def refused_native_task(tmp_path):
    env = ns.native_env(tmp_path, extra_host={"protected_paths": [
        str(tmp_path / "host-secrets"), str(tmp_path / "fake-harness-home"),
        str(tmp_path / "workspace")]})
    controller = env["controller"]
    task = controller.submit("owner", "scanner-refusal", ns.native_spec(env, ops=[]))
    with pytest.raises(PermissionError, match="overlaps trusted state"):
        controller.run("owner", task, execution_host=ns.FAKE_HOST)
    state = controller.store.get("state", task)
    assert state["status"] == "refused-before-launch"
    assert not {"pid", "pgid", "worker_identity"} & set(state)
    assert controller.store.get("invocation", state["attempt"])["observed"] is None
    return env, task, state


@needs_seatbelt
def test_prelaunch_refusal_schedules_same_task_without_fabricating_result(tmp_path):
    env, task, refused = refused_native_task(tmp_path)
    controller = env["controller"]
    # Operator repairs the preflight configuration; the refused attempt remains unchanged.
    controller.hosts[ns.FAKE_HOST]["protected_paths"] = [
        str(env["secrets"]), str(env["fake_home"])]
    source = "def add(a, b):\n    return a + b\n"
    objective = "\n".join([
        "Implement add(a, b) correctly after the repaired preflight.",
        "@op write math_ops.py " + ns.b64(source),
        "@op result " + ns.b64(json.dumps({"fixed": True})),
    ])
    scheduled = controller.continue_task(
        "owner", task, "preflight-repaired", {"objective": objective})
    prior = scheduled["continuation"]["prior"]
    assert scheduled["task"] == task and scheduled["generation"] == 2
    assert prior["schema"] == "corral-prelaunch-refusal-checkpoint-v1"
    assert prior["process"] == "never-launched" and prior["accepted"] is None
    assert prior["refused_state"] == refused
    assert scheduled["result"] is None
    assert controller.store.get("state", task) == refused
    assert controller.store.get("result", task) is None

    completed = controller.run("owner", task, execution_host=ns.FAKE_HOST)
    assert completed["result"]["accepted"] is True
    assert completed["result"]["generation"] == 2
    assert (env["workspace"] / "math_ops.py").read_text() == source
    assert controller.store.get("invocation", refused["attempt"])["observed"] is None
    assert controller.continue_task(
        "owner", task, "preflight-repaired", {"objective": objective})["scheduled"] is False


@pytest.mark.parametrize("contradiction", ["observed-worker", "active-allocation",
                                            "publication-intent"])
@needs_seatbelt
def test_prelaunch_continuation_refuses_any_worker_or_effect_evidence(
        tmp_path, contradiction):
    env, task, state = refused_native_task(tmp_path)
    controller = env["controller"]
    if contradiction == "observed-worker":
        invocation = controller.store.get("invocation", state["attempt"])
        controller.store.replace("invocation", state["attempt"], {
            **invocation, "observed": {"model": "unexpected-worker"}})
        message = "never-launched invocation claim"
    elif contradiction == "active-allocation":
        allocation = controller.store.get("allocation", task)
        controller.store.replace("allocation", task, {**allocation, "active": True})
        message = "ownership or allocation"
    else:
        with controller.store.transaction() as db:
            db.execute("""INSERT INTO publication_intents(
                intent,resource,owner,epoch,head_sha,base_sha,status,payload,error,review_id,
                created_at,updated_at,attempt_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("effect", "pr:fixture/repo#1", "corral", 1, "a" * 40, "b" * 40,
                 "pending", "{}", None, None, 1.0, 1.0, state["attempt"]))
        message = "external effect"
    with pytest.raises(PermissionError, match=message):
        controller.continue_task(
            "owner", task, "must-refuse", {"objective": "Try again safely."})
    assert controller.store.records("continuation") == {}
    assert controller.store.records("amendment") == {}


def test_trusted_export_inspection_refusal_continues_without_a_test_verifier(tmp_path):
    workspace = tmp_path / "snapshot"
    workspace.mkdir()
    (workspace / "candidate.py").write_text("answer = 1\n")
    (workspace / "candidate.diff").write_text("+answer = 1\n")
    files = {name: {"digest": hashlib.sha256((workspace / name).read_bytes()).hexdigest(),
                    "mode": (workspace / name).stat().st_mode & 0o777}
             for name in ("candidate.py", "candidate.diff")}
    export = {"repository": "fixture/repo", "pr_number": 7, "head": "a" * 40,
              "base": "b" * 40, "policy_id": "advisory", "policy_digest": "c" * 64,
              "workspace": str(workspace), "selected_files": files,
              "selected_files_digest": digest(files), "diff_path": "candidate.diff",
              "diff_sha256": files["candidate.diff"]["digest"],
              "export_digest": digest(files), "auth_mode": "fixture"}
    export = {"export_id": digest(export), **export}
    harness = tmp_path / "packet-harness"
    harness.write_text("#!/bin/sh\nexit 1\n")
    harness.chmod(harness.stat().st_mode | stat.S_IEXEC)
    profile = Profile(id="inspection", model="fixture-model", effort="medium",
                      harness="packet", version="1", route="packet-route",
                      roles=("review",), tools=("inspect-packet", "report"), context=100,
                      provider="fixture", account_ref="fixture")
    host = {"routes": ["packet-route"], "harnesses": ["packet"], "cpu": 2,
            "memory_mb": 512, "native_routes": {"packet-route": {
                "harness": "packet", "binary": str(harness),
                "argv": ["--packet", "{packet_file}", "--result", "{result_file}",
                         "--model", "{model}", "--effort", "{effort}"],
                "envelope": "corral-inspection-report-v1", "provider": "fixture",
                "account_ref": "fixture", "endpoint": "fixture", "synthetic": True,
                "inspection_only": True, "supported_models": ["fixture-model"],
                "supported_efforts": ["medium"], "credential_env": ["FIXTURE_KEY"]}}}
    controller = Controller(tmp_path / "state", "owner", {"mini2": host},
                            default_host="mini2", profiles=[profile])
    controller.store.put_once("trusted_export", export["export_id"], export)
    controller.store.acquire("pr:fixture/repo#7", "corral")
    task = controller.submit("owner", "inspection-refusal", {
        "trusted_export_id": export["export_id"], "role": "review", "host": "mini2",
        "profile_id": profile.id, "objective": "Inspect the candidate.",
        "tools": ["inspect-packet", "report"]})
    request = controller.store.get("request", task)
    resource = "workspace:" + str(workspace.resolve())
    epoch = controller.store.acquire(resource, task)
    controller.store.transition_owner(resource, task, epoch, "released")
    controller.store.replace("allocation", task, {
        "host": "mini2", "cpu": 1, "memory_mb": 0, "active": False})
    attempt = "inspection-prelaunch-attempt"
    controller.store.put_once("claim", task, {"attempt": attempt, "generation": 1})
    controller.store.put_once("invocation", attempt, {
        "task": task, "generation": 1, "role": "review",
        "selection": request["selection"], "observed": None,
        "usage": "unknown-until-native-events"})
    controller.store.replace("state", task, {
        "status": "refused-before-launch", "attempt": attempt, "epoch": epoch,
        "host": "mini2", "profile": request["selection"], "process_status": None,
        "endpoint": "local", "generation": 1, "error": "PermissionError"})

    scheduled = controller.continue_task(
        "owner", task, "scanner-fixed", {"objective": "Inspect after scanner repair."})
    policy = scheduled["continuation"]["verifier_policy"]
    assert scheduled["task"] == task and scheduled["generation"] == 2
    assert policy["kind"] == "controller-inspection-report"
    assert policy["trusted_export_id"] == export["export_id"]
    assert policy["script"] is None and policy["verifier_paths"] == []
    assert scheduled["result"] is None
