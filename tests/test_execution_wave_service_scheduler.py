import json
import os
import time

from corral.execution import continuation, service_wave
from corral.execution.service import Service
from corral.execution.store import Store
from .test_execution_service_cli import configs, git_repo


def _wave_config(tmp_path):
    primary = git_repo(tmp_path / "primary")
    producer = git_repo(tmp_path / "producer")
    consumer = git_repo(tmp_path / "consumer")
    worker = (
        "import json; from pathlib import Path; "
        "value=Path('input.txt').read_text(); "
        "Path('output.txt').write_text('produced:' + value); "
        "Path('result.json').write_text(json.dumps({'copied': value}))"
    )
    path = configs(tmp_path, primary, worker=worker)
    raw = json.loads(path.read_text())
    repo = raw["repositories"]["demo"]
    repo["wave_workspaces"] = {
        "primary": {"producer": str(producer), "consumer": str(consumer)}
    }
    raw["wave_plans"] = {"usage": {
        "enabled": True,
        "tasks": [
            {"name": "producer", "repository": "demo", "objective": "produce",
             "workspace_key": "producer", "candidate_paths": ["output.txt"]},
            {"name": "consumer", "repository": "demo", "objective": "consume",
             "workspace_key": "consumer", "dependencies": ["producer"],
             "candidate_paths": ["output.txt"]},
        ],
        "handoffs": [{"producer": "producer", "producer_path": "output.txt",
                      "consumer": "consumer", "consumer_path": "input.txt"}],
    }}
    path.write_text(json.dumps(raw))
    return path, primary


def _complete_wave(service, wave_id):
    for _index in range(100):
        service.tick()
        state = service.store.get("wave_state", wave_id)
        if state.get("status") == "completed":
            return
        time.sleep(0.02)
    raise AssertionError(service.store.get("wave_state", wave_id))


def test_wave_reservation_blocks_interactive_host_oversubscription(tmp_path, monkeypatch):
    config, _workspace = _wave_config(tmp_path)
    service = Service(config)
    service.submit("interactive", "demo", "interactive work")
    service.store.replace("wave_dispatch", "wave-task", {
        "task": "wave-task", "host": "primary", "workspace": str(tmp_path / "other"),
        "cpu": 2, "memory_mb": 0, "status": "active",
    })
    launches = []
    monkeypatch.setattr("corral.execution.service_dispatch.launch",
                        lambda *_args, **_kwargs: launches.append(True))

    result = service.tick(now=10)

    assert result["dispatched"] == []
    assert launches == []
    assert service.status("interactive")["event"]["status"] == "prepared"


def test_wave_capacity_reads_controller_resolved_service_request(tmp_path, monkeypatch):
    config, workspace = _wave_config(tmp_path)
    service = Service(config)
    service.submit_wave_plan("usage", "wave-resolved")
    admitted = service.submit("trusted-shaped", "demo", "controller-resolved work")
    event = admitted["event"]
    service.store.replace("service_event", "trusted-shaped", {
        **event, "status": "dispatching",
        "resolved_spec": {"trusted_export_id": "opaque-admission-identity"},
    })
    observed = []
    monkeypatch.setattr(service_wave, "_reconcile_dispatches", lambda _service: None)
    monkeypatch.setattr(service_wave.WaveRunner, "step",
                        lambda _runner, *_args, **kwargs:
                        observed.extend(kwargs["additional_running"])
                        or {"wave_id": "wave-resolved", "status": "running"})

    service_wave.progress(service)

    assert observed[0]["workspace"] == str(workspace)
    assert observed[0]["host"] == "primary"


def test_wave_cancellation_waits_for_owner_and_allocation_release(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = Store(tmp_path / "state")
    task = "cancelled-wave-task"
    resource = "workspace:" + str(workspace.resolve())
    epoch = store.acquire(resource, task)
    assert store.allocate(task, "primary", 1, 0, {"cpu": 1, "memory_mb": 0})
    store.replace("state", task, {"status": "cancelled"})
    store.replace("wave_dispatch", task, {
        "task": task, "host": "primary", "workspace": str(workspace),
        "cpu": 1, "memory_mb": 0, "status": "active",
    })

    class Controller:
        def context(self, _task):
            return {"workspace": str(workspace)}

        def status(self, _token, _task):
            return {"state": store.get("state", task)}

    service = type("FixtureService", (), {
        "store": store, "controller": Controller(), "token": "owner",
    })()
    service_wave._reconcile_dispatches(service)
    assert store.get("wave_dispatch", task)["status"] == "active"

    store.transition_owner(resource, task, epoch, "released")
    store.release_allocation(task)
    service_wave._reconcile_dispatches(service)
    settled = store.get("wave_dispatch", task)
    assert settled["status"] == "terminal"
    assert settled["terminal_status"] == "cancelled"


def test_continuous_interactive_admissions_do_not_starve_dependency_wave(tmp_path):
    config, _workspace = _wave_config(tmp_path)
    service = Service(config)
    submitted = service.submit_wave_plan("usage", "wave-fair")
    assert submitted["wave"]["wave_id"] == "wave-fair"

    for index in range(120):
        event_id = f"interactive-{index}"
        service.submit(event_id, "demo", f"interactive work {index}")
        service.tick()
        state = service.store.get("wave_state", "wave-fair")
        if state.get("status") == "completed":
            break
        time.sleep(0.02)
    else:
        raise AssertionError(service.store.get("wave_state", "wave-fair"))

    wave = service.store.get("wave", "wave-fair")
    results = [service.store.get("result", task) for task in wave["task_ids"]]
    assert all(result and result.get("accepted") for result in results)
    assert len(service.store.records("invocation")) >= 2


def test_wave_continuation_uses_new_generation_dispatch_without_erasing_history(
        tmp_path, monkeypatch):
    config, _workspace = _wave_config(tmp_path)
    service = Service(config)
    wave = service.submit_wave_plan("usage", "wave-continuation")["wave"]
    _complete_wave(service, "wave-continuation")
    producer = wave["tasks"]["producer"]
    first_key = continuation.claim_key(producer, 1)
    assert service.store.get("wave_dispatch", first_key)["status"] == "terminal"
    service.controller.continue_task(
        service.token, producer, "amended-output", {"objective": "produce amended output"})
    launches = []
    monkeypatch.setattr("corral.execution.service_dispatch.launch",
                        lambda *_args, **_kwargs: launches.append(True)
                        or {"pid": os.getpid(), "process_start": "fixture"})

    result = service.tick()

    second_key = continuation.claim_key(producer, 2)
    assert result["waves"][0]["status"] == "running"
    assert service.store.get("wave_dispatch", first_key)["status"] == "terminal"
    assert service.store.get("wave_dispatch", second_key)["generation"] == 2
    assert launches == [True]


def test_uncertain_wave_dispatch_converges_from_late_terminal_without_relaunch(
        tmp_path, monkeypatch):
    config, _workspace = _wave_config(tmp_path)
    service = Service(config)
    wave = service.submit_wave_plan("usage", "wave-late-result")["wave"]
    _complete_wave(service, "wave-late-result")
    consumer = wave["tasks"]["consumer"]
    key = continuation.claim_key(consumer, 1)
    record = service.store.get("wave_dispatch", key)
    service.store.replace("wave_dispatch", key, {**record, "status": "uncertain"})
    service.store.replace("wave_state", "wave-late-result", {
        "wave_id": "wave-late-result", "status": "blocked",
    })
    monkeypatch.setattr("corral.execution.service_dispatch.launch",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(
                            AssertionError("terminal reconciliation must not relaunch")))

    progressed = service_wave.progress(service)

    assert progressed[0]["status"] == "completed"
    assert service.store.get("wave_dispatch", key)["status"] == "terminal"
    assert service.store.get("wave_state", "wave-late-result")["status"] == "completed"


def test_tick_lease_serializes_service_and_wave_lane_cursor(tmp_path, monkeypatch):
    config, _workspace = _wave_config(tmp_path)
    first, second = Service(config), Service(config)
    first.submit("interactive", "demo", "interactive work")
    first.store.replace("wave_state", "pending-wave", {"status": "running"})
    first.store.replace("wave", "pending-wave", {"task_ids": []})
    owner = "held-tick"
    first.store.put_once("service_tick_identity", owner, {
        "pid": os.getpid(), "process_start": "unknown-but-not-dead",
    })
    first.store.acquire("service-scheduler:tick", owner)
    monkeypatch.setattr("corral.execution.runtime_identity.process_status",
                        lambda _identity: "unknown")

    result = second.tick(now=10)

    assert result["busy"] is True
    assert second.store.get("scheduler_cursor", "service-wave") is None
