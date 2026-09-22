"""Concurrent scheduler ownership and controller-bound service completion."""
import json
import os
import socket
import threading
import time
from pathlib import Path

from corral.execution import continuation
from corral.execution.runtime_identity import process_start
from corral.execution.service import Service
from corral.execution.service_scheduler import TICK_RESOURCE
from .test_execution_service_cli import configs, git_repo


def test_service_dispatch_rotates_hosts_under_global_limit(tmp_path, monkeypatch):
    first = git_repo(tmp_path / "first")
    second = git_repo(tmp_path / "second")
    config = configs(tmp_path, first)
    service_raw = json.loads(config.read_text())
    controller_path = Path(service_raw["controller_config"])
    controller_raw = json.loads(controller_path.read_text())
    controller_raw["hosts"]["remote"] = {
        "routes": ["deterministic"], "harnesses": [], "cpu": 2, "memory_mb": 1024,
        "executor": {"declared": "fixture"},
    }
    controller_path.write_text(json.dumps(controller_raw))
    repo = service_raw["repositories"]["demo"]
    repo["allowed_hosts"] = ["mini2", "remote"]
    repo["workspaces"]["remote"] = str(second)
    config.write_text(json.dumps(service_raw))
    service = Service(config)
    service.submit("first-host", "demo", "first", host="mini2")
    service.submit("second-host", "demo", "second", host="remote")

    launches = []

    def launch(_config, _state, task, host, **_kwargs):
        launches.append((task, host))
        return {"pid": 100 + len(launches), "process_start": "fixture", "host": host}

    monkeypatch.setattr("corral.execution.service_dispatch.launch", launch)
    monkeypatch.setattr("corral.execution.runtime_identity.alive", lambda identity: bool(identity))
    assert service.tick(now=10)["dispatched"] == ["first-host"]
    assert service.tick(now=11)["dispatched"] == ["second-host"]
    assert [host for _task, host in launches] == ["mini2", "remote"]
    assert service.store.get("service_scheduler", "host_cursor") == {"last_host": "remote"}


def test_lost_launcher_ack_converges_from_uncertain_when_task_finishes(tmp_path):
    workspace = git_repo(tmp_path / "repo")
    service = Service(configs(tmp_path, workspace))
    submitted = service.submit("lost-ack", "demo", "finish after service restart")
    task = submitted["event"]["task_id"]
    claimed = service._claim("lost-ack", time.time(), {"pid": os.getpid()})
    assert claimed and "launcher_identity" not in claimed

    service._reconcile({"pid": os.getpid()})
    assert service.status("lost-ack")["event"]["status"] == "uncertain"
    assert service.controller.run(service.token, task, execution_host="mini2")["result"]["accepted"]

    service._reconcile({"pid": os.getpid()})
    assert service.status("lost-ack")["event"]["status"] == "completed"
    assert len(service.store.records("invocation")) == 1


def test_controller_worker_birth_identity_keeps_running_event_fenced(tmp_path):
    from corral.execution.process import _boot_identity, _os_process_identity

    workspace = git_repo(tmp_path / "repo")
    service = Service(configs(tmp_path, workspace))
    submitted = service.submit("controller-worker", "demo", "active worker")
    task = submitted["event"]["task_id"]
    event = service.store.get("service_event", "controller-worker")
    service.store.replace("service_event", "controller-worker", {
        **event, "status": "dispatching",
        "launcher_identity": {"pid": 987654321, "process_start": "absent"},
    })
    observed = _os_process_identity(os.getpid())
    service.store.replace("state", task, {
        "status": "running", "generation": 1,
        "worker_identity": {
            "pid": os.getpid(), "host": socket.gethostname(),
            "os_started": observed["started"], "boot_identity": _boot_identity(),
            "birth_identity_observed": True,
        },
    })

    service._reconcile({"pid": os.getpid()})

    assert service.status("controller-worker")["event"]["status"] == "dispatching"


def test_terminal_event_keeps_launcher_and_controller_worker_identities_separate(tmp_path):
    workspace = git_repo(tmp_path / "repo")
    service = Service(configs(tmp_path, workspace))
    submitted = service.submit("identities", "demo", "record the worker")
    task = submitted["event"]["task_id"]
    event = service.store.get("service_event", "identities")
    launcher = {"pid": 7001, "process_start": "launcher", "host": "mini2"}
    service.store.replace("service_event", "identities", {
        **event, "status": "dispatching", "launcher_identity": launcher,
    })
    service.store.replace("state", task, {
        "status": "completed", "attempt": "attempt-1", "generation": 1,
        "host": "mini2", "worker_identity": {
            "pid": 7002, "process_start": "worker", "birth_identity_observed": True,
        },
    })
    continuation.record_result(
        service.store, task, 1, {"accepted": True, "generation": 1}
    )

    service._reconcile({"pid": os.getpid()})
    terminal = service.status("identities")["event"]
    assert terminal["launcher_identity"] == launcher
    assert terminal["worker_identity"]["attempt"] == "attempt-1"
    assert terminal["worker_identity"]["pid"] == 7002


def test_amended_generation_must_match_before_service_event_completes(tmp_path):
    workspace = git_repo(tmp_path / "repo")
    service = Service(configs(tmp_path, workspace))
    submitted = service.submit("amend-race", "demo", "old objective")
    task = submitted["event"]["task_id"]
    event = service.store.get("service_event", "amend-race")
    service.store.replace("service_event", "amend-race", {
        **event, "status": "dispatching", "launcher_identity": {
            "pid": os.getpid(), "process_start": process_start(os.getpid()),
        },
    })
    continuation.record_result(service.store, task, 1, {
        "accepted": True, "generation": 1, "amendment_pending": True,
    })
    service.store.replace("state", task, {
        "status": "completed", "attempt": "attempt-1", "generation": 1,
        "amended_objective_pending": True,
    })

    service._reconcile({"pid": os.getpid()})
    assert service.status("amend-race")["event"]["status"] == "dispatching"

    service.store.put_once("continuation", f"{task}:auto-amend", {
        "task": task, "continuation_id": "auto-amend", "generation": 2,
    })
    service._reconcile({"pid": os.getpid()})
    assert service.status("amend-race")["event"]["status"] == "prepared"


def test_active_cancellation_is_uncertain_until_controller_releases_owner(tmp_path):
    workspace = git_repo(tmp_path / "repo")
    service = Service(configs(tmp_path, workspace))
    submitted = service.submit("cancel-active", "demo", "cancel running task")
    task = submitted["event"]["task_id"]
    resource = "workspace:" + str(workspace.resolve())
    epoch = service.store.acquire(resource, task)
    assert service.store.allocate(task, "mini2", 1, 0, {"cpu": 2, "memory_mb": 1024})
    service.store.replace("state", task, {
        "status": "completed", "attempt": "cancelled-attempt", "epoch": epoch,
        "generation": 1, "host": "mini2",
    })
    event = service.store.get("service_event", "cancel-active")
    service.store.replace("service_event", "cancel-active", {**event, "status": "dispatching"})
    service.controller.cancel(service.token, task)

    service._reconcile({"pid": os.getpid()})
    unresolved = service.status("cancel-active")["event"]
    assert unresolved["status"] == "uncertain"
    assert "ownership is unresolved" in unresolved["error"]

    service.store.transition_owner(resource, task, epoch, "released")
    service.store.release_allocation(task)
    service.store.replace("state", task, {
        "status": "cancelled", "attempt": "cancelled-attempt", "epoch": epoch,
        "generation": 1, "host": "mini2",
    })
    service._reconcile({"pid": os.getpid()})
    assert service.status("cancel-active")["event"]["status"] == "cancelled"


def test_concurrent_ticks_are_serialized_and_same_workspace_is_reserved(tmp_path, monkeypatch):
    workspace = git_repo(tmp_path / "repo")
    config = configs(tmp_path, workspace)
    first, second = Service(config), Service(config)
    first.submit("one", "demo", "first")
    first.submit("two", "demo", "second")
    entered, release = threading.Event(), threading.Event()

    def launch(_config, _state, _task, host, **_kwargs):
        entered.set()
        assert release.wait(5)
        return {"pid": os.getpid(), "process_start": "fixture", "host": host}

    monkeypatch.setattr("corral.execution.service_dispatch.launch", launch)
    worker_result = {}

    def run_first():
        worker_result.update(first.tick(now=10))

    thread = threading.Thread(target=run_first)
    thread.start()
    assert entered.wait(5)
    overlapping = second.tick(now=10)
    assert overlapping["busy"] is True and overlapping["dispatched"] == []
    release.set()
    thread.join(5)
    assert not thread.is_alive() and worker_result["dispatched"] == ["one"]

    monkeypatch.setattr("corral.execution.runtime_identity.alive", lambda identity: bool(identity))
    after = second.tick(now=11)
    assert after["dispatched"] == []
    assert second.status("two")["event"]["status"] == "prepared"


def test_atomic_claim_refuses_host_oversubscription_across_service_instances(tmp_path):
    first_workspace = git_repo(tmp_path / "first")
    second_workspace = git_repo(tmp_path / "second")
    config = configs(tmp_path, first_workspace)
    raw = json.loads(config.read_text())
    raw["repositories"]["other"] = json.loads(json.dumps(raw["repositories"]["demo"]))
    raw["repositories"]["other"]["workspaces"]["mini2"] = str(second_workspace)
    for repository in raw["repositories"].values():
        repository["task_defaults"]["cpu"] = 2
    config.write_text(json.dumps(raw))
    first, second = Service(config), Service(config)
    first.submit("cpu-one", "demo", "first")
    second.submit("cpu-two", "other", "second")
    runtime = {"pid": os.getpid()}
    assert first._claim("cpu-one", 10, runtime, scheduler_host="mini2") is not None
    assert second._claim("cpu-two", 10, runtime, scheduler_host="mini2") is None
    assert second.status("cpu-two")["event"]["status"] == "prepared"


def test_tick_lease_unknown_process_identity_stays_busy(tmp_path, monkeypatch):
    workspace = git_repo(tmp_path / "repo")
    service = Service(configs(tmp_path, workspace))
    owner = "prior-tick"
    service.store.put_once("service_tick_identity", owner, {"pid": os.getpid()})
    service.store.acquire(TICK_RESOURCE, owner)
    assert service.tick(now=10)["busy"] is True
    assert service.store.ownership(TICK_RESOURCE)[0] == owner

    identity = {"pid": os.getpid(), "process_start": "recorded"}
    service.store.replace("service_tick_identity", owner, identity)
    monkeypatch.setattr("corral.execution.runtime_identity.process_start", lambda _pid: None)
    assert service.tick(now=11)["busy"] is True
    assert service.store.ownership(TICK_RESOURCE)[0] == owner


def test_atomic_claim_canonicalizes_workspace_aliases(tmp_path):
    workspace = git_repo(tmp_path / "repo")
    alias = tmp_path / "repo-alias"
    alias.symlink_to(workspace, target_is_directory=True)
    config = configs(tmp_path, workspace)
    raw = json.loads(config.read_text())
    raw["repositories"]["alias"] = json.loads(json.dumps(raw["repositories"]["demo"]))
    raw["repositories"]["alias"]["workspaces"]["mini2"] = str(alias)
    config.write_text(json.dumps(raw))
    first, second = Service(config), Service(config)
    first.submit("canonical", "demo", "first")
    second.submit("alias", "alias", "second")
    runtime = {"pid": os.getpid()}
    assert first._claim("canonical", 10, runtime, scheduler_host="mini2") is not None
    assert second._claim("alias", 10, runtime, scheduler_host="mini2") is None
