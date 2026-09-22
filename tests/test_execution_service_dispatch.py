"""Service dispatch: one capacity authority, generation-bound reconcile, bounded tick state."""
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from corral.execution import service_dispatch
from corral.execution.reconciliation_api import reconcile_local
from corral.execution.runtime_identity import launched, process_status
from corral.execution.service import Service

from .test_execution_service_cli import configs, git_repo

RUNTIME = {"pid": os.getpid()}


def _two_repositories(tmp_path, *, cpu=None, worker=None, reader=False):
    first, second = git_repo(tmp_path / "first"), git_repo(tmp_path / "second")
    config = configs(tmp_path, first, worker=worker)
    raw = json.loads(config.read_text())
    host = raw["repositories"]["demo"]["default_host"]
    raw["repositories"]["other"] = json.loads(json.dumps(raw["repositories"]["demo"]))
    raw["repositories"]["other"]["workspaces"][host] = str(second)
    if cpu is not None:
        raw["repositories"]["demo"]["task_defaults"]["cpu"] = cpu
    config.write_text(json.dumps(raw))
    if reader:
        controller_path = Path(raw["controller_config"])
        controller = json.loads(controller_path.read_text())
        controller["hosts"][host]["reconciliation_delivery_reader"] = "controller-records-only"
        controller_path.write_text(json.dumps(controller))
    return Service(config)


def _task(service, event_id):
    return service.store.get("service_event", event_id)["task_id"]


def _wait(predicate, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not reached")


# --------------------------------------------------------------------- one capacity authority

def test_claim_reserves_store_capacity_that_the_controller_dispatch_adopts(tmp_path):
    service = _two_repositories(tmp_path, cpu=2)
    service.submit("big", "demo", "use the whole host")
    service.submit("small", "other", "wait for capacity")
    big, small = _task(service, "big"), _task(service, "small")

    assert service._claim("big", 10, RUNTIME) is not None
    assert service.store.get("allocation", big) == {
        "host": service.execution_host, "cpu": 2, "memory_mb": 0, "active": True,
        "reservation": "big"}
    # Refused admission is "not dispatched, nothing acquired".
    assert service._claim("small", 10, RUNTIME) is None
    assert service.store.get("allocation", small) is None
    assert service.status("small")["event"]["status"] == "prepared"

    assert service.controller.run(service.token, big,
                                  execution_host=service.execution_host)["result"]["accepted"]
    assert service.store.get("allocation", big) == {
        "host": service.execution_host, "cpu": 2, "memory_mb": 0, "active": False}
    assert service._claim("small", 11, RUNTIME) is not None


def test_direct_controller_allocation_is_visible_to_service_admission(tmp_path, monkeypatch):
    service = _two_repositories(tmp_path)
    launches = []
    monkeypatch.setattr(service_dispatch, "launch", lambda _c, _s, task, host, **_k:
                        launches.append(task) or {"pid": os.getpid(), "host": host})
    assert service.store.allocate("direct-cli-task", service.execution_host, 2, 0,
                                  {"cpu": 2, "memory_mb": 1024})
    service.submit("queued", "demo", "wait behind a direct dispatch")
    assert service.tick(now=10)["dispatched"] == []
    assert service.store.get("allocation", _task(service, "queued")) is None
    service.store.release_allocation("direct-cli-task")
    assert service.tick(now=11)["dispatched"] == ["queued"]
    assert launches == [_task(service, "queued")]


def test_failed_launch_returns_its_reservation(tmp_path, monkeypatch):
    service = _two_repositories(tmp_path)

    def cannot_launch(*_args, **_kwargs):
        raise OSError("launcher executable unavailable")

    monkeypatch.setattr(service_dispatch, "launch", cannot_launch)
    service.submit("unlaunched", "demo", "launcher fails")
    assert service.tick(now=10)["dispatched"] == ["unlaunched"]
    assert service.status("unlaunched")["event"]["status"] == "uncertain"
    assert service.store.get("allocation", _task(service, "unlaunched"))["active"] is False


# --------------------------------------------------------------------- dispatched generation

class StandInLaunchers:
    """Stand-in for the detached launcher: the test decides when each one claims or exits."""

    def __init__(self):
        self.procs = []

    def __call__(self, _config, _state, task_id, host, *, development_mode=False):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        self.procs.append(proc)
        return {**launched(proc.pid, str(Path(sys.executable).resolve()), host),
                "argv_module": "stand-in", "task": task_id}

    def exit(self, index):
        self.procs[index].kill()
        self.procs[index].wait()

    def close(self):
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


def _generation_two_dispatched(tmp_path, monkeypatch, hold):
    """Generation 1 completes and schedules generation 2, whose launcher has not claimed it."""
    workspace = git_repo(tmp_path / "repo")
    config = configs(tmp_path, workspace)
    raw = json.loads(config.read_text())
    # The verifier waits while the hold file exists, so "worker exited, generation still
    # verifying" is a window the test controls instead of a timing accident.
    raw["repositories"]["demo"]["task_defaults"]["verify"] = [sys.executable, "-c", (
        "import time; from pathlib import Path\n"
        f"while Path({str(hold)!r}).exists(): time.sleep(0.01)\n"
        "assert Path('output.txt').is_file()")]
    config.write_text(json.dumps(raw))
    service = Service(config)
    launchers = StandInLaunchers()
    monkeypatch.setattr(service_dispatch, "launch", launchers)
    task = service.submit("amended", "demo", "write original behavior")["event"]["task_id"]
    service.tick()
    assert len(launchers.procs) == 1
    # Launcher 1's work: generation 1 completes, then the amendment schedules generation 2.
    service.controller.run(service.token, task, execution_host=service.execution_host)
    service.controller.continue_task(service.token, task, "auto-amend-g2",
                                     {"objective": "write amended behavior"})
    launchers.exit(0)
    service.tick()
    assert len(launchers.procs) == 2
    assert service.status("amended")["event"]["status"] == "dispatching"
    return service, launchers, task


def test_unclaimed_generation_is_not_redispatched_while_its_launcher_lives(tmp_path, monkeypatch):
    service, launchers, _task_id = _generation_two_dispatched(tmp_path, monkeypatch, tmp_path / "hold")
    try:
        service.tick()
        event = service.status("amended")["event"]
        assert len(launchers.procs) == 2, "generation 2 dispatched twice while its launcher lived"
        assert event["status"] == "dispatching"
        assert event["launcher_identity"]["pid"] == launchers.procs[1].pid
    finally:
        launchers.close()


def test_live_generation_two_is_not_reported_as_lost(tmp_path, monkeypatch):
    hold = tmp_path / "hold"
    service, launchers, task = _generation_two_dispatched(tmp_path, monkeypatch, hold)
    errors = []
    try:
        service.tick()
        hold.write_text("hold")

        def launcher_two():
            try:
                service.controller.run(service.token, task, execution_host=service.execution_host)
            except Exception as error:  # noqa: BLE001 - surfaced by the assertion below
                errors.append(error)

        runner = threading.Thread(target=launcher_two)
        runner.start()

        def verifying():
            state = service.controller.status(service.token, task)["state"]
            return (state.get("generation") == 2 and state.get("status") == "running"
                    and process_status(state.get("worker_identity") or {}) == "dead")

        _wait(verifying)
        # A duplicate launcher (if any was dispatched) finds the generation claimed and exits.
        for index in range(2, len(launchers.procs)):
            launchers.exit(index)
        assert launchers.procs[1].poll() is None  # the real generation-2 launcher lives
        service.tick()
        mid = service.status("amended")["event"]
        hold.unlink()
        runner.join(30)
        assert not errors, errors
        assert mid["status"] == "dispatching", mid.get("error")
        launchers.exit(1)
        service.tick()
        final = service.status("amended")
        assert final["event"]["status"] == "completed"
        assert sorted(final["task"]["results"]) == ["1", "2"]
        assert len(service.store.records("invocation")) == 2
    finally:
        if hold.exists():
            hold.unlink()
        launchers.close()


def test_launcher_lost_before_claim_is_uncertain_and_returns_its_reservation(tmp_path, monkeypatch):
    service, launchers, task = _generation_two_dispatched(tmp_path, monkeypatch, tmp_path / "hold")
    try:
        launchers.exit(1)
        service.tick()
        event = service.status("amended")["event"]
        assert event["status"] == "uncertain" and "generation 2" in event["error"]
        assert len(launchers.procs) == 2  # never re-dispatched blindly
        assert service.store.get("allocation", task)["active"] is False
    finally:
        launchers.close()


def test_cancel_with_launcher_lost_before_claim_is_cancelled(tmp_path, monkeypatch):
    service, launchers, task = _generation_two_dispatched(tmp_path, monkeypatch, tmp_path / "hold")
    try:
        service.cancel("amended")
        launchers.exit(1)
        service.tick()
        assert service.status("amended")["event"]["status"] == "cancelled"
        assert service.store.get("allocation", task)["active"] is False
        assert service.store.get("claim", f"{task}:g2") is None
    finally:
        launchers.close()


# --------------------------------------------------------------------- cancellation

def test_service_cancel_of_running_event_converges_through_reconcile(tmp_path):
    worker = ("import json, time; from pathlib import Path; Path('output.txt').write_text('x')\n"
              "while True: time.sleep(0.05)")
    service = _two_repositories(tmp_path, worker=worker, reader=True)
    task = service.submit("cancel-live", "demo", "cancel while running")["event"]["task_id"]
    assert service._claim("cancel-live", time.time(), RUNTIME)
    runner = threading.Thread(target=service.controller.run, args=(service.token, task),
                              kwargs={"execution_host": service.execution_host})
    runner.start()
    _wait(lambda: (service.store.get("state", task) or {}).get("status") == "running")
    service.cancel("cancel-live")
    runner.join(30)
    service._reconcile(RUNTIME)
    unresolved = service.status("cancel-live")["event"]
    assert unresolved["status"] == "uncertain" and "unresolved" in unresolved["error"]

    assert reconcile_local(service.controller, service.token, task)["terminal_status"] == "cancelled"
    service._reconcile(RUNTIME)
    assert service.status("cancel-live")["event"]["status"] == "cancelled"
    assert service.store.get("allocation", task)["active"] is False
    service.submit("next", "demo", "next objective")
    assert service._claim("next", time.time(), RUNTIME)


def test_cancel_after_completion_is_post_terminal(tmp_path):
    service = _two_repositories(tmp_path)
    task = service.submit("late", "demo", "finish then cancel")["event"]["task_id"]
    assert service._claim("late", time.time(), RUNTIME)
    assert service.controller.run(service.token, task, execution_host=service.execution_host)["result"]["accepted"]
    service.cancel("late")
    service._reconcile(RUNTIME)
    assert service.status("late")["event"]["status"] == "completed"
    service.submit("next", "demo", "next objective")
    assert service._claim("next", time.time(), RUNTIME)


# --------------------------------------------------------------------- tick state

def test_tick_identities_are_not_retained_after_ticks(tmp_path):
    service = _two_repositories(tmp_path)
    for now in (10, 11, 12):
        service.tick(now=now)
    assert service.store.records("service_tick_identity") == {}


def test_busy_tick_forgets_only_its_own_identity(tmp_path):
    from corral.execution.service_scheduler import TICK_RESOURCE

    service = _two_repositories(tmp_path)
    holder = {"pid": os.getpid(), "process_start": launched(os.getpid(), "", "")["process_start"]}
    service.store.put_once("service_tick_identity", "live-holder", holder)
    service.store.acquire(TICK_RESOURCE, "live-holder")
    assert service.tick(now=10)["busy"] is True
    assert service.store.records("service_tick_identity") == {"live-holder": holder}
