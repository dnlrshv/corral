"""A launcher killed mid-attempt leaves a settleable attempt, never a permanent fence."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time

import pytest

from corral.execution.reconciliation_api import reconcile_local
from corral.execution.runtime_identity import process_status

from .test_execution_regressions import setup, spec  # noqa: F401 - pytest fixture
from .test_execution_service_dispatch import _two_repositories


def _wait(predicate, what, seconds=60):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"condition not reached: {what}")


def _group_gone(pgid):
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def _sigkill_child(pid):
    """SIGKILL a direct child of this test process and reap it, so its PID is truly gone."""
    os.kill(pid, signal.SIGKILL)
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass  # Already reaped by the subprocess module.


def _resource(service):
    spec = service.store.get("request", service.store.get("service_event", "crash")["task_id"])
    return "workspace:" + os.path.realpath(spec["workspace"])


def test_sigkilled_launcher_attempt_settles_and_frees_the_workspace(tmp_path):
    hold = tmp_path / "hold"
    hold.write_text("hold")
    worker = ("import time; from pathlib import Path\n"
              "Path('output.txt').write_text('partial')\n"
              f"while Path({str(hold)!r}).exists(): time.sleep(0.05)")
    # One task uses the whole host (cpu 2 of 2), so a leaked allocation blocks all later work.
    service = _two_repositories(tmp_path, cpu=2, worker=worker, reader=True)
    service.submit("crash", "demo", "run until the launcher is killed")
    assert service.tick()["dispatched"] == ["crash"]
    event = service.store.get("service_event", "crash")
    task, launcher = event["task_id"], event["launcher_identity"]
    state = {}
    try:
        def running():
            state.update(service.store.get("state", task) or {})
            return state.get("status") == "running" and state.get("pgid")

        _wait(running, "the real launcher started its worker")
        dispatcher = state["dispatcher_identity"]
        # The launcher process itself dispatched the attempt, and it is alive.
        assert dispatcher["pid"] == launcher["pid"]
        assert process_status(dispatcher) == "alive"
        with pytest.raises(PermissionError, match="not proven dead"):
            reconcile_local(service.controller, service.token, task)

        _sigkill_child(launcher["pid"])
        _wait(lambda: process_status(dispatcher) == "dead", "the killed launcher is observed dead")
        epoch = state["epoch"]
        assert service.store.ownership(_resource(service)) == (task, epoch, "active")
        assert service.store.get("allocation", task)["active"] is True
        # The orphaned worker still runs, so nothing may settle yet.
        service.tick()
        assert service.store.get("service_event", "crash")["status"] == "dispatching"
        with pytest.raises(PermissionError, match="remains live"):
            reconcile_local(service.controller, service.token, task)

        os.killpg(state["pgid"], signal.SIGKILL)
        _wait(lambda: _group_gone(state["pgid"]), "the orphaned worker group is gone")
        service.tick()
        lost = service.store.get("service_event", "crash")
        assert lost["status"] == "uncertain" and "reconcile task" in lost["error"]
        # Before the fix this is where the attempt stuck: the workspace stays fenced and the
        # host's whole capacity stays allocated, so no later event can dispatch.
        service.submit("next", "demo", "run after the crash is settled")
        assert service.tick()["dispatched"] == []

        service.cancel("crash")
        result = reconcile_local(service.controller, service.token, task)
    finally:
        for pid in (launcher["pid"],):
            if process_status(launcher) == "alive":
                _sigkill_child(pid)
        if state.get("pgid") and not _group_gone(state["pgid"]):
            os.killpg(state["pgid"], signal.SIGKILL)

    receipt = result["receipt"]
    assert result["terminal_status"] == "cancelled" and result["accepted"] is False
    assert receipt["verifier_executed"] is False
    assert receipt["dispatcher"]["status"] == "dead"
    assert receipt["dispatcher"]["identity"] == dispatcher
    assert receipt["observation"]["process"]["status"] == "absent"
    # Ownership and allocation are released together, exactly once, with the result.
    assert service.store.get("state", task)["status"] == "cancelled"
    assert service.store.ownership(_resource(service)) == (task, epoch, "released")
    assert service.store.get("allocation", task)["active"] is False
    assert [key for key, value in service.store.records("invocation").items()
            if value.get("task") == task] == [state["attempt"]]
    assert reconcile_local(service.controller, service.token, task) == result

    hold.unlink()
    service.tick()
    assert service.store.get("service_event", "crash")["status"] == "cancelled"

    def next_completed():
        service.tick()
        return service.status("next")["event"]["status"] == "completed"

    _wait(next_completed, "the next event dispatches and completes on the freed workspace")
    assert service.status("next")["task"]["result"]["accepted"] is True


def test_open_attempt_without_a_recorded_dispatcher_is_never_settled(setup):  # noqa: F811
    controller, repo = setup
    task = controller.submit("owner", "legacy-open", spec(repo))
    resource = "workspace:" + str(repo.resolve())
    epoch = controller.store.acquire(resource, task)
    controller.store.allocate(task, "fixture", 1, 0, {"cpu": 4, "memory_mb": 1024})
    state = {"status": "running", "attempt": "legacy", "epoch": epoch, "generation": 1,
             "pid": 99999997, "pgid": 99999997}
    controller.store.put_once("claim", task, {"attempt": "legacy", "generation": 1})
    controller.store.put_once("invocation", "legacy", {"task": task, "generation": 1})
    controller.store.replace("state", task, state)
    controller.cancel("owner", task)
    for identity in (None, {"pid": os.getpid(), "process_start": "not this process",
                            "host": "another-host"}):
        if identity is not None:
            controller.store.replace("state", task, {**state, "dispatcher_identity": identity})
        with pytest.raises(PermissionError, match="not proven dead"):
            reconcile_local(controller, "owner", task)
        assert controller.store.ownership(resource) == (task, epoch, "active")
        assert controller.store.get("allocation", task)["active"] is True
        assert controller.store.get("result", task) is None


#: A real dispatcher process that stops at a chosen point of ``Controller.run`` until killed.
BLOCKING_LAUNCHER = """
import json, sys, time
from pathlib import Path
from corral.execution import controller as module

config, task, gate, stage = sys.argv[1:5]

def block(*_args, **_kwargs):
    Path(gate).write_text(stage)
    while True:
        time.sleep(0.05)

if stage == "preparation":
    module.verifier.policy = block   # before any worker can exist
else:
    module.Process = block           # after the launch mark, before a worker PID is recorded
raw = json.loads(Path(config).read_text())
controller = module.Controller(raw["state"], raw["token"], raw["hosts"],
                               default_host=raw["default_host"])
controller.run(raw["token"], task, execution_host=raw["default_host"])
"""


def _killed_mid_dispatch(controller, repo, tmp_path, stage):
    launcher = tmp_path / "blocking_launcher.py"
    launcher.write_text(BLOCKING_LAUNCHER)
    config = tmp_path / "controller.json"
    config.write_text(json.dumps({"state": str(controller.store.path.parent), "token": "owner",
                                  "hosts": controller.hosts, "default_host": "fixture"}))
    task = controller.submit("owner", "killed-" + stage, spec(repo))
    gate = tmp_path / ("gate-" + stage)
    process = subprocess.Popen([sys.executable, str(launcher), str(config), task, str(gate), stage],
                               stdin=subprocess.DEVNULL)
    try:
        _wait(gate.exists, "the dispatcher reached " + stage)
        state = controller.store.get("state", task)
        assert state["status"] == "dispatching"
        assert state["dispatcher_identity"]["pid"] == process.pid
        with pytest.raises(PermissionError, match="not proven dead"):
            reconcile_local(controller, "owner", task)
    finally:
        process.kill()
        process.wait()
    _wait(lambda: process_status(state["dispatcher_identity"]) == "dead", "the dispatcher is dead")
    resource = "workspace:" + str(repo.resolve())
    assert controller.store.ownership(resource) == (task, state["epoch"], "active")
    assert controller.store.get("allocation", task)["active"] is True
    controller.cancel("owner", task)
    return task, state, resource


def test_dispatcher_killed_before_worker_launch_settles_as_never_launched(setup, tmp_path):  # noqa: F811
    controller, repo = setup
    task, state, resource = _killed_mid_dispatch(controller, repo, tmp_path, "preparation")
    assert "worker_launch" not in state

    result = reconcile_local(controller, "owner", task)
    observation = result["receipt"]["observation"]
    assert result["terminal_status"] == "cancelled" and result["accepted"] is False
    assert observation["process"]["basis"] == "dispatcher-died-before-worker-launch"
    assert observation["artifact"] == {
        "status": "unavailable", "reason": "worker-never-launched", "attempt": state["attempt"],
        "generation": 1, "directory": str(controller.artifacts / task)}
    assert controller.store.ownership(resource) == (task, state["epoch"], "released")
    assert controller.store.get("allocation", task)["active"] is False
    follow = controller.submit("owner", "after-killed-preparation", spec(repo))
    assert controller.run("owner", follow, execution_host="fixture")["result"]["accepted"]


def test_dispatcher_killed_after_the_launch_mark_stays_fenced(setup, tmp_path):  # noqa: F811
    controller, repo = setup
    task, state, resource = _killed_mid_dispatch(controller, repo, tmp_path, "launch")
    # A worker may exist without a recorded identity, so nothing can prove it stopped.
    assert controller.store.get("state", task)["worker_launch"] == "started"
    with pytest.raises(PermissionError, match="lacks a recorded process identity"):
        reconcile_local(controller, "owner", task)
    assert controller.store.ownership(resource) == (task, state["epoch"], "active")
    assert controller.store.get("allocation", task)["active"] is True
    assert controller.store.get("result", task) is None
