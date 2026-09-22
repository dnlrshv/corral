"""Dispatch lifecycle: nothing is stranded by a refusal, a cancellation or a hung verifier."""
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from corral.execution import prelaunch_refusal, verifier, workspace_contract
from corral.execution.controller import Controller
from corral.execution.demo import fixture_profiles
from corral.execution.reconciliation_api import reconcile_local
from corral.execution.recovery import reconcile
from tests.test_execution_regressions import setup, spec  # noqa: F401 - pytest fixture

UNTIL_KILLED = "import time\nwhile True: time.sleep(0.05)"


def _resource(repo):
    return "workspace:" + str(repo.resolve())


def _wait_status(store, task, status, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if (store.get("state", task) or {}).get("status") == status:
            return
        time.sleep(0.02)
    raise AssertionError(f"task never reached {status!r}: {store.get('state', task)}")


def _assert_follow_up_dispatch_succeeds(controller, repo, name):
    follow = controller.submit("owner", name, spec(repo))
    assert controller.run("owner", follow, execution_host="fixture")["result"]["accepted"]
    assert controller.store.ownership(_resource(repo))[:1] == (follow,)
    assert controller.store.get("allocation", follow)["active"] is False


# --------------------------------------------------------------------- prelaunch refusals

def test_capacity_refusal_acquires_nothing_and_later_dispatch_succeeds(setup):  # noqa: F811
    controller, repo = setup
    assert controller.store.allocate("elsewhere", "fixture", 4, 0, {"cpu": 4, "memory_mb": 1024})
    task = controller.submit("owner", "no-capacity", spec(repo))
    with pytest.raises(PermissionError, match="capacity unavailable"):
        controller.run("owner", task, execution_host="fixture")
    assert controller.store.ownership(_resource(repo)) is None
    assert controller.store.get("allocation", task) is None
    assert controller.store.get("claim", task) is None
    assert controller.store.get("state", task) is None
    controller.store.release_allocation("elsewhere")
    _assert_follow_up_dispatch_succeeds(controller, repo, "after-capacity")
    # The refused task itself was never dispatched, so it can still run once capacity frees.
    assert controller.run("owner", task, execution_host="fixture")["result"]["accepted"]


@pytest.mark.parametrize("resources", [{"cpu": 0}, {"memory_mb": -1}], ids=["cpu", "memory"])
def test_invalid_resource_refusal_acquires_nothing(setup, resources):  # noqa: F811
    controller, repo = setup
    task = controller.submit("owner", "invalid-resources", {**spec(repo), **resources})
    with pytest.raises(ValueError, match="invalid resource request"):
        controller.run("owner", task, execution_host="fixture")
    assert controller.store.ownership(_resource(repo)) is None
    assert controller.store.get("allocation", task) is None
    assert controller.store.get("state", task) is None
    _assert_follow_up_dispatch_succeeds(controller, repo, "after-invalid")


def test_duplicate_generation_claim_acquires_nothing(setup, monkeypatch):  # noqa: F811
    """A second client passes the no-state check, then the first finishes the generation."""
    controller, repo = setup
    task = controller.submit("owner", "raced-claim", spec(repo))
    original = workspace_contract.preflight
    raced = []

    def first_client_completes_meanwhile(*args, **kwargs):
        if not raced:
            raced.append(True)
            assert controller.run("owner", task, execution_host="fixture")["result"]["accepted"]
        return original(*args, **kwargs)

    monkeypatch.setattr(workspace_contract, "preflight", first_client_completes_meanwhile)
    second = controller.run("owner", task, execution_host="fixture")
    assert raced and second["state"]["status"] == "completed"
    assert controller.store.ownership(_resource(repo)) == (task, 1, "released")
    assert controller.store.get("allocation", task)["active"] is False
    assert len(controller.store.records("invocation")) == 1
    monkeypatch.setattr(workspace_contract, "preflight", original)
    _assert_follow_up_dispatch_succeeds(controller, repo, "after-duplicate")


def test_filesystem_error_before_worker_is_a_released_refusal(setup):  # noqa: F811
    controller, repo = setup
    task = controller.submit("owner", "artifact-io", spec(repo))
    (controller.artifacts / task).write_text("not a directory")
    with pytest.raises(FileExistsError):
        controller.run("owner", task, execution_host="fixture")
    state = controller.store.get("state", task)
    assert state["status"] == "refused-before-launch" and state["error"] == "FileExistsError"
    assert controller.store.ownership(_resource(repo)) == (task, state["epoch"], "released")
    assert controller.store.get("allocation", task)["active"] is False
    # The refusal is conclusive enough to continue the same task after the operator repair.
    with controller.store.transaction() as db:
        proof = prelaunch_refusal.prove_db(db, task, 1, _resource(repo))
    assert proof["process"] == "never-launched"
    _assert_follow_up_dispatch_succeeds(controller, repo, "after-io")


def test_prelaunch_refusal_never_releases_an_epoch_it_did_not_acquire(setup):  # noqa: F811
    controller, repo = setup
    task = controller.submit("owner", "reentrant", {**spec(repo), "verify": None})
    epoch = controller.store.acquire(_resource(repo), task)
    with pytest.raises(PermissionError, match="no verification command"):
        controller.run("owner", task, execution_host="fixture")
    assert controller.store.get("state", task)["status"] == "refused-before-launch"
    assert controller.store.ownership(_resource(repo)) == (task, epoch, "active")
    assert controller.store.get("allocation", task)["active"] is False


def test_concurrent_client_of_a_running_task_leaves_its_fences_alone(setup, tmp_path):  # noqa: F811
    controller, repo = setup
    gate = tmp_path / "gate"
    worker = ("import time; from pathlib import Path\n"
              f"while not Path({str(gate)!r}).exists(): time.sleep(0.02)\n"
              "open('result.json','w').write('{\"ok\":true}')")
    task = controller.submit("owner", "live", spec(repo, worker))
    runner = threading.Thread(target=controller.run, args=("owner", task),
                              kwargs={"execution_host": "fixture"})
    runner.start()
    try:
        _wait_status(controller.store, task, "running")
        epoch = controller.store.get("state", task)["epoch"]
        assert controller.run("owner", task, execution_host="fixture")["state"]["status"] == "running"
        assert controller.store.ownership(_resource(repo)) == (task, epoch, "active")
        assert controller.store.get("allocation", task)["active"] is True
    finally:
        gate.write_text("go")
        runner.join(30)
    assert controller.status("owner", task)["result"]["accepted"]
    assert len(controller.store.records("invocation")) == 1


# --------------------------------------------------------------------- cancellation

def _run_then_cancel(controller, task):
    errors = []

    def run():
        try:
            controller.run("owner", task, execution_host="fixture")
        except Exception as error:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(error)

    runner = threading.Thread(target=run)
    runner.start()
    _wait_status(controller.store, task, "running")
    controller.cancel("owner", task)
    runner.join(30)
    assert not runner.is_alive() and not errors, errors


def test_real_run_cancel_reconcile_releases_owner_and_allocation(setup):  # noqa: F811
    controller, repo = setup
    task = controller.submit("owner", "cancel-live", spec(repo, UNTIL_KILLED))
    _run_then_cancel(controller, task)

    # The run ends fenced and reconcilable, never as a completed attempt.
    state = controller.store.get("state", task)
    assert state["status"] == "uncertain" and state["cancellation"]["group_stopped"] is True
    assert controller.store.ownership(_resource(repo)) == (task, state["epoch"], "uncertain")
    assert controller.store.get("allocation", task)["active"] is True
    assert controller.store.get("result", task) is None
    artifacts = controller.artifacts / task
    assert (artifacts / "context.json").is_file()
    assert not (artifacts / "adapter-result.json").exists()
    assert not (artifacts / "receipt.json").exists()

    result = reconcile_local(controller, "owner", task)
    artifact = result["receipt"]["observation"]["artifact"]
    assert result["terminal_status"] == "cancelled" and result["accepted"] is False
    assert result["receipt"]["verifier_executed"] is False
    assert artifact["status"] == "unavailable" and artifact["reason"] == "adapter-result-absent"
    assert result["receipt"]["observation"]["process"]["status"] == "absent"
    assert controller.store.get("state", task)["status"] == "cancelled"
    assert controller.store.ownership(_resource(repo)) == (task, state["epoch"], "released")
    assert controller.store.get("allocation", task)["active"] is False
    assert controller.store.records("reconciliation")[task + ":g1"]["event"] == "cancelled-reconciled"
    assert reconcile_local(controller, "owner", task) == result
    _assert_follow_up_dispatch_succeeds(controller, repo, "after-cancel")


def test_cancelled_native_adapter_without_completion_is_unavailable_evidence(setup):  # noqa: F811
    controller, repo = setup
    task = controller.submit("owner", "cancel-adapter", spec(repo, UNTIL_KILLED))
    _run_then_cancel(controller, task)
    attempt = controller.store.get("state", task)["attempt"]
    adapter = controller.artifacts / task / "adapter-result.json"
    adapter.write_text(json.dumps({"task": task, "attempt": attempt, "status": "failed"}))
    artifact = reconcile_local(controller, "owner", task)["receipt"]["observation"]["artifact"]
    assert artifact["status"] == "unavailable" and artifact["reason"] == "adapter-result-incomplete"
    assert artifact["adapter_status"] == "failed" and artifact["adapter_sha256"]


def test_cancelled_attempt_with_adapter_of_another_attempt_is_refused(setup):  # noqa: F811
    controller, repo = setup
    task = controller.submit("owner", "cancel-foreign", spec(repo, UNTIL_KILLED))
    _run_then_cancel(controller, task)
    (controller.artifacts / task / "adapter-result.json").write_text(
        json.dumps({"task": task, "attempt": "someone-else", "status": "failed"}))
    with pytest.raises(PermissionError, match="do not bind"):
        reconcile_local(controller, "owner", task)
    assert controller.store.ownership(_resource(repo))[2] == "uncertain"


def test_cancel_after_worker_exit_releases_owner_and_allocation_together(setup, monkeypatch):  # noqa: F811
    controller, repo = setup
    task = controller.submit("owner", "late-cancel", spec(repo))
    original = verifier.execute

    def cancel_during_verification(*args, **kwargs):
        controller.cancel("owner", task)
        return original(*args, **kwargs)

    monkeypatch.setattr(verifier, "execute", cancel_during_verification)
    finished = controller.run("owner", task, execution_host="fixture")
    assert finished["state"]["status"] == "completed"
    assert finished["result"]["accepted"] is False  # cancel still wins acceptance
    assert controller.store.ownership(_resource(repo))[2] == "released"
    assert controller.store.get("allocation", task)["active"] is False


# --------------------------------------------------------------------- reconcile finalization

def _interrupted(controller, repo, name):
    task = controller.submit("owner", name, spec(repo))
    epoch = controller.store.acquire(_resource(repo), task)
    controller.store.allocate(task, "fixture", 1, 0, {"cpu": 4, "memory_mb": 1024})
    controller.store.transition_owner(_resource(repo), task, epoch, "uncertain")
    state = {"status": "uncertain", "attempt": name + "-attempt", "epoch": epoch, "generation": 1}
    controller.store.replace("state", task, state)
    (repo / "result.json").write_text('{"ok":true}')
    return task, state


STOPPED = {"execution_stopped": True, "effects_reconciled": True}


def test_reconcile_finalization_refuses_state_changed_during_verification(setup):  # noqa: F811
    controller, repo = setup
    task, _state = _interrupted(controller, repo, "changed")

    def probe(_task, observed):
        controller.store.replace("state", task, {**observed, "error": "newer controller fact"})
        return STOPPED

    with pytest.raises(PermissionError, match="state changed"):
        reconcile(controller, "owner", task, probe)
    assert controller.store.get("result", task) is None
    assert controller.store.ownership(_resource(repo))[2] == "uncertain"
    assert controller.store.get("allocation", task)["active"] is True


def test_reconcile_finalization_is_all_or_nothing(setup):  # noqa: F811
    controller, repo = setup
    task, state = _interrupted(controller, repo, "fenced")
    # Another owner took the workspace after the attempt's fence went stale.
    controller.store.transition_owner(_resource(repo), task, state["epoch"], "released")
    controller.store.acquire(_resource(repo), "someone-else")
    with pytest.raises(PermissionError, match="stale ownership fence"):
        reconcile(controller, "owner", task, lambda *_: STOPPED)
    assert controller.store.get("result", task) is None
    assert controller.store.get("state", task) == state
    assert controller.store.get("allocation", task)["active"] is True
    assert controller.store.ownership(_resource(repo))[0] == "someone-else"


def test_reconcile_finalization_records_result_state_and_releases(setup):  # noqa: F811
    controller, repo = setup
    task, state = _interrupted(controller, repo, "settles")
    result = reconcile(controller, "owner", task, lambda *_: STOPPED)
    assert result["accepted"] and controller.store.get("result", task) == result
    assert controller.store.get("state", task)["status"] == "reconciled"
    assert controller.store.ownership(_resource(repo)) == (task, state["epoch"], "released")
    assert controller.store.get("allocation", task)["active"] is False


# --------------------------------------------------------------------- verifier bound

def _hanging_verifier(pid_file):
    return [sys.executable, "-c", (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
        "time.sleep(120)")]


def _assert_dead(pid_file):
    pid = int(pid_file.read_text())
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.02)
    raise AssertionError("verifier descendant survived the timeout")


def test_verifier_timeout_kills_its_process_group_and_receipts_exit_124(tmp_path):
    from tests.test_execution_service_cli import git_repo

    workspace = git_repo(tmp_path / "workspace")
    pid_file = tmp_path / "grandchild.pid"
    policy = verifier.policy({"verify": _hanging_verifier(pid_file)}, workspace)
    started = time.monotonic()
    record = verifier.execute(policy, workspace, candidate_paths=[], task="t", attempt="a",
                              pre_verifier_manifest=None, timeout=2)
    assert time.monotonic() - started < 30
    assert record.payload["exit_code"] == verifier.TIMEOUT_EXIT_CODE == 124
    assert record.payload["timed_out"] is True and record.payload["timeout_seconds"] == 2
    assert "wall-clock bound" in record.payload["refused"]
    _assert_dead(pid_file)


def test_controller_verifier_timeout_is_a_rejected_completed_attempt(tmp_path):
    from tests.test_execution_service_cli import git_repo

    repo = git_repo(tmp_path / "repo")
    host = {"routes": ["fixture"], "harnesses": ["synthetic"], "cpu": 4, "memory_mb": 1024,
            "verifier_timeout_seconds": 2}
    controller = Controller(tmp_path / "state", "owner", {"fixture": host},
                            default_host="fixture", profiles=fixture_profiles())
    pid_file = tmp_path / "grandchild.pid"
    task = controller.submit("owner", "hung-verifier",
                             {**spec(repo), "verify": _hanging_verifier(pid_file)})
    finished = controller.run("owner", task, execution_host="fixture")
    receipt = finished["result"]["receipt"]
    assert finished["result"]["accepted"] is False and receipt["timed_out"] is True
    assert receipt["exit_code"] == 124
    assert controller.store.ownership(_resource(repo))[2] == "released"
    assert controller.store.get("allocation", task)["active"] is False
    _assert_dead(pid_file)


@pytest.mark.parametrize("bound", [0, -1, "60", True])
def test_invalid_verifier_bound_is_refused_before_launch(tmp_path, bound):
    from tests.test_execution_service_cli import git_repo

    repo = git_repo(tmp_path / "repo")
    host = {"routes": ["fixture"], "harnesses": ["synthetic"], "cpu": 4, "memory_mb": 1024,
            "verifier_timeout_seconds": bound}
    controller = Controller(tmp_path / "state", "owner", {"fixture": host},
                            default_host="fixture", profiles=fixture_profiles())
    task = controller.submit("owner", "bad-bound", spec(repo))
    with pytest.raises(ValueError, match="verifier_timeout_seconds"):
        controller.run("owner", task, execution_host="fixture")
    assert controller.store.get("state", task)["status"] == "refused-before-launch"
    assert not Path(repo / "result.json").exists()
