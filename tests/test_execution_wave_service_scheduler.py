import json
import os
import sys
import time
from pathlib import Path

from corral.execution import continuation, service_wave
from corral.execution.runtime_identity import launched
from corral.execution.service import Service
from corral.execution.store import Store
from .test_execution_service_cli import configs, git_repo


def _wave_config(tmp_path, *, cpu=None):
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
    if cpu is not None:
        repo["task_defaults"]["cpu"] = cpu
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


def _live_launcher(*_args, **_kwargs):
    """A launcher identity observed alive: the test process itself."""
    return launched(os.getpid(), sys.executable, "primary")


def _dead_launcher(*_args, **_kwargs):
    """A launcher identity whose PID now belongs to a different process birth."""
    return {"pid": os.getpid(), "process_start": "an earlier process with this pid"}


def test_wave_reservation_blocks_interactive_host_oversubscription(tmp_path, monkeypatch):
    config, _workspace = _wave_config(tmp_path, cpu=2)
    service = Service(config)
    wave = service.submit_wave_plan("usage", "wave-held")["wave"]
    producer = wave["tasks"]["producer"]
    monkeypatch.setattr("corral.execution.service_dispatch.launch", _live_launcher)

    first = service.tick(now=10)

    assert first["waves"][0]["dispatched"] == [{"task": producer, "status": "active"}]
    # The launched wave task holds the whole host in the store, the one capacity authority.
    assert service.store.get("allocation", producer) == {
        "host": "primary", "cpu": 2, "memory_mb": 0, "active": True, "reservation": producer}
    service.submit("interactive", "demo", "interactive work")
    launches = []
    monkeypatch.setattr("corral.execution.service_dispatch.launch",
                        lambda *_args, **_kwargs: launches.append(True))

    second = service.tick(now=11)

    assert second["waves"] == []
    assert second["dispatched"] == []
    assert launches == []
    assert service.status("interactive")["event"]["status"] == "prepared"
    assert service.store.get("allocation", "interactive") is None


def test_wave_capacity_reads_store_allocations_of_resolved_service_requests(
        tmp_path, monkeypatch):
    config, _workspace = _wave_config(tmp_path)
    service = Service(config)
    service.submit_wave_plan("usage", "wave-resolved")
    admitted = service.submit("trusted-shaped", "demo", "controller-resolved work")
    event = admitted["event"]
    service.store.replace("service_event", "trusted-shaped", {
        **event, "resolved_spec": {"trusted_export_id": "opaque-admission-identity"},
    })
    # Service admission reserves from the controller-resolved request, not the event copy.
    assert service._claim("trusted-shaped", 10, {"pid": os.getpid()},
                          scheduler_host="primary") is not None
    observed = []
    monkeypatch.setattr(service_wave.WaveRunner, "step",
                        lambda _runner, *_args, **kwargs:
                        observed.append((kwargs["running"], kwargs["capacity"]))
                        or {"wave_id": "wave-resolved", "status": "running"})

    service_wave.progress(service)

    running, capacity = observed[0]
    assert running == [{"host": "primary", "cpu": 1, "memory_mb": 0, "active": True,
                         "reservation": "trusted-shaped"}]
    assert capacity == service.controller.capacity("primary")


def test_wave_dispatch_refused_for_capacity_writes_nothing(tmp_path, monkeypatch):
    config, _workspace = _wave_config(tmp_path)
    service = Service(config)
    wave = service.submit_wave_plan("usage", "wave-full")["wave"]
    producer = wave["tasks"]["producer"]
    assert service.store.allocate("other-task", "primary", 2, 0,
                                  service.controller.capacity("primary"))
    launches = []
    monkeypatch.setattr("corral.execution.service_dispatch.launch",
                        lambda *_args, **_kwargs: launches.append(True))

    assert service_wave._dispatch(service, "wave-full", producer, "primary") \
        == "capacity-unavailable"

    assert launches == []
    assert service.store.records("wave_dispatch") == {}
    assert service.store.get("allocation", producer) is None


def test_wave_and_service_admission_share_one_workspace_fence(tmp_path, monkeypatch):
    config, primary = _wave_config(tmp_path)
    raw = json.loads(config.read_text())
    raw["wave_plans"]["same"] = {"enabled": True, "tasks": [
        {"name": "solo", "repository": "demo", "objective": "same workspace"}]}
    config.write_text(json.dumps(raw))
    service = Service(config)
    solo = service.submit_wave_plan("same", "wave-same")["wave"]["tasks"]["solo"]
    service.submit("interactive", "demo", "interactive work")
    assert service._claim("interactive", 10, {"pid": os.getpid()},
                          scheduler_host="primary") is not None
    monkeypatch.setattr("corral.execution.service_dispatch.launch", _live_launcher)

    # A dispatched service event fences its workspace against a wave task.
    assert service_wave._dispatch(service, "wave-same", solo, "primary") == "workspace-busy"
    assert service.store.records("wave_dispatch") == {}
    assert service.store.get("allocation", solo) is None

    # And a launched wave task fences it against service admission.
    service.store.release_reservation(
        service.status("interactive")["event"]["task_id"], "interactive")
    event = service.store.get("service_event", "interactive")
    service.store.replace("service_event", "interactive", {**event, "status": "prepared"})
    assert service_wave._dispatch(service, "wave-same", solo, "primary") == "active"
    assert service._claim("interactive", 11, {"pid": os.getpid()},
                          scheduler_host="primary") is None
    assert service.store.get("service_event", "interactive")["status"] == "prepared"
    assert Path(service.store.get("wave_dispatch", solo)["workspace"]) == primary


def test_failed_wave_launch_returns_its_reservation(tmp_path, monkeypatch):
    config, _workspace = _wave_config(tmp_path)
    service = Service(config)
    producer = service.submit_wave_plan("usage", "wave-fails")["wave"]["tasks"]["producer"]

    def refuse(*_args, **_kwargs):
        raise OSError("launcher could not start")

    monkeypatch.setattr("corral.execution.service_dispatch.launch", refuse)

    assert service_wave._dispatch(service, "wave-fails", producer, "primary") == "uncertain"

    record = service.store.get("wave_dispatch", producer)
    assert record["status"] == "uncertain"
    assert record["error"] == "launcher could not start"
    assert service.store.get("allocation", producer)["active"] is False


def test_dead_launcher_before_claim_releases_reservation_on_a_service_lane_tick(
        tmp_path, monkeypatch):
    config, _workspace = _wave_config(tmp_path)
    service = Service(config)
    producer = service.submit_wave_plan("usage", "wave-lost")["wave"]["tasks"]["producer"]
    monkeypatch.setattr("corral.execution.service_dispatch.launch", _dead_launcher)
    assert service.tick(now=10)["waves"][0]["dispatched"] == [
        {"task": producer, "status": "active"}]
    assert service.store.get("allocation", producer)["active"] is True
    service.submit("interactive", "demo", "interactive work")

    # Both lanes have work and the cursor selects the service lane, yet the wave dispatch
    # is reconciled first: its launcher died without claiming, so nothing can adopt it.
    result = service.tick(now=11)

    assert result["waves"] == []
    assert result["dispatched"] == ["interactive"]
    record = service.store.get("wave_dispatch", producer)
    assert record["status"] == "uncertain"
    assert "before the launcher claimed generation 1" in record["error"]
    assert service.store.get("allocation", producer)["active"] is False
    step = service_wave.progress(service)[0]
    assert step["status"] == "blocked"
    assert step["task_summary"][producer]["blocker"] == record["error"]


def test_oversized_wave_task_blocks_instead_of_running_forever(tmp_path, monkeypatch):
    config, _workspace = _wave_config(tmp_path, cpu=3)
    service = Service(config)
    producer = service.submit_wave_plan("usage", "wave-huge")["wave"]["tasks"]["producer"]
    monkeypatch.setattr("corral.execution.service_dispatch.launch",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(
                            AssertionError("an oversized task must not launch")))

    step = service.tick(now=10)["waves"][0]

    assert step["status"] == "blocked"
    assert step["is_terminal"] is True
    assert step["task_summary"][producer]["blocker"] == "exceeds registered host capacity"


def test_wave_cancellation_waits_for_owner_and_allocation_release(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = Store(tmp_path / "state")
    task = "cancelled-wave-task"
    resource = "workspace:" + str(workspace.resolve())
    epoch = store.acquire(resource, task)
    assert store.allocate(task, "primary", 1, 0, {"cpu": 1, "memory_mb": 0})
    store.replace("state", task, {"status": "cancelled"})
    store.put_once("claim", task, {"attempt": "attempt-1", "generation": 1})
    store.replace("wave_dispatch", task, {
        "task": task, "host": "primary", "workspace": str(workspace),
        "cpu": 1, "memory_mb": 0, "status": "active",
    })

    class Controller:
        hosts = {"primary": {}}

        def context(self, _task):
            return {"workspace": str(workspace), "host": "primary"}

        def status(self, _token, _task):
            return {"state": store.get("state", task)}

    service = type("FixtureService", (), {
        "store": store, "controller": Controller(), "token": "owner",
    })()
    service_wave.reconcile_dispatches(service)
    assert store.get("wave_dispatch", task)["status"] == "active"

    store.transition_owner(resource, task, epoch, "released")
    store.release_allocation(task)
    service_wave.reconcile_dispatches(service)
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
    # Each wave task's controller dispatch adopted its reservation and released it when
    # the attempt finished, so no wave capacity is left held.
    for task in wave["task_ids"]:
        allocation = service.store.get("allocation", task)
        assert allocation["active"] is False
        assert "reservation" not in allocation


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


def test_legacy_wave_action_is_an_alias_of_the_async_submit(tmp_path, monkeypatch, capsys):
    import io

    from corral.execution import client, service_endpoint

    config, primary = _wave_config(tmp_path)
    defaults = json.loads(config.read_text())["repositories"]["demo"]["task_defaults"]
    tasks = [{"name": "solo", "request_id": "endpoint-wave:solo", "spec": {
        **defaults, "repo": "demo", "workspace": str(primary), "host": "primary",
        "mode": "wave"}}]
    monkeypatch.setattr(client.Client, "call", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("the wave alias must not start a controller wave runner")))

    def call(action):
        request = {"action": action, "wave_id": "legacy", "tasks": tasks}
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(request)))
        assert service_endpoint.main(["--config", str(config)]) == 0
        return json.loads(capsys.readouterr().out)

    legacy = call("wave")
    assert call("wave-advanced") == legacy
    service = Service(config)
    assert service.store.get("wave", "legacy") == legacy
    assert service.store.records("wave_dispatch") == {}

    _complete_wave(service, "legacy")
    assert service.store.get("result", legacy["tasks"]["solo"])["accepted"] is True
