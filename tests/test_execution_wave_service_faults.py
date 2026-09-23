"""A failing, waiting or never-admissible wave neither fails service ticks nor starves others."""
import io
import json
import os
import subprocess
import sys
import time

import pytest

from corral.execution import continuation, service_endpoint, service_wave
from corral.execution.runtime_identity import process_start, process_status
from corral.execution.service import Service
from corral.execution.store import Store
from corral.execution.wave import WaveRunner
from .test_execution_wave_service_scheduler import (
    _complete_wave, _live_launcher, _wave_config)


def _defaults(config):
    return json.loads(config.read_text())["repositories"]["demo"]["task_defaults"]


def _solo_plans(config, *, max_dispatch=None):
    """Two one-task plans whose tasks use separate registered workspaces."""
    raw = json.loads(config.read_text())
    for name, key in (("left", "producer"), ("right", "consumer")):
        raw["wave_plans"][name] = {"enabled": True, "tasks": [
            {"name": name, "repository": "demo", "objective": f"{name} work",
             "workspace_key": key}]}
    if max_dispatch is not None:
        raw["max_dispatch_per_tick"] = max_dispatch
    config.write_text(json.dumps(raw))


def _ticks(service, count, start=10):
    return [service.tick(now=start + index) for index in range(count)]


def _consumer_workspace(config):
    raw = json.loads(config.read_text())
    return raw["repositories"]["demo"]["wave_workspaces"]["primary"]["consumer"]


def _wait_accepted(service, task, seconds=20):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        service.tick()
        result = continuation.current_result(service.store, task)
        if result and result.get("accepted"):
            return
        time.sleep(0.02)
    raise AssertionError(service.store.get("wave_dispatch", task))


# -- B1: a wave step failure never escapes the service tick ------------------------------

@pytest.mark.parametrize("resources", [
    {"cpu": 0}, {"cpu": -1}, {"cpu": True}, {"cpu": 1.5}, {"memory_mb": -1},
    {"memory_mb": "1024"},
], ids=["cpu-zero", "cpu-negative", "cpu-bool", "cpu-fraction", "memory-negative",
        "memory-text"])
def test_wave_admission_refuses_invalid_resource_requests(tmp_path, resources):
    config, primary = _wave_config(tmp_path)
    service = Service(config)
    tasks = [{"name": "solo", "request_id": "bad:solo", "spec": {
        **_defaults(config), "repo": "demo", "workspace": str(primary), "host": "primary",
        "mode": "wave", **resources}}]

    with pytest.raises(ValueError, match="invalid wave task"):
        service_wave.submit_advanced(service, "bad", tasks)

    assert service.store.get("wave", "bad") is None
    assert service.store.records("request") == {}


def test_named_wave_plan_with_invalid_repository_resources_is_refused(tmp_path):
    config, _primary = _wave_config(tmp_path, cpu=0)
    service = Service(config)

    with pytest.raises(ValueError, match="invalid wave task CPU request"):
        service.submit_wave_plan("usage", "bad-plan")

    assert service.store.get("wave", "bad-plan") is None
    assert service.store.get("service_wave_plan", "bad-plan") is None


def test_stored_invalid_resource_wave_blocks_its_task_without_failing_ticks(
        tmp_path, monkeypatch):
    config, primary = _wave_config(tmp_path)
    service = Service(config)
    tasks = [{"name": "solo", "request_id": "stored:solo", "spec": {
        **_defaults(config), "repo": "demo", "workspace": str(primary), "host": "primary",
        "mode": "wave", "cpu": 0}}]
    # Admitted by another path that does not apply the service's admission checks.
    task = WaveRunner(service.controller, service.token).submit_wave(
        "stored", tasks)["tasks"]["solo"]
    monkeypatch.setattr("corral.execution.service_dispatch.launch",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(
                            AssertionError("an invalid request must not launch")))

    ticks = _ticks(service, 5)

    steps = [step for tick in ticks for step in tick["waves"]]
    assert steps[0]["status"] == "blocked"
    assert steps[0]["task_summary"][task]["blocker"].startswith("invalid resource request")
    assert service.store.get("wave_state", "stored")["status"] == "blocked"
    assert service.store.records("wave_dispatch") == {}


def test_handoff_into_a_workspace_held_by_another_owner_waits_then_binds(tmp_path):
    config, _primary = _wave_config(tmp_path)
    service = Service(config)
    wave = service.submit_wave_plan("usage", "wave-held-consumer")["wave"]
    producer, consumer = wave["tasks"]["producer"], wave["tasks"]["consumer"]
    resource = "workspace:" + str(os.path.realpath(_consumer_workspace(config)))
    epoch = service.store.acquire(resource, "another-controller-task")

    _wait_accepted(service, producer)
    ticks = _ticks(service, 5, start=100)

    steps = [step for tick in ticks for step in tick["waves"]]
    assert steps and all(step["status"] == "running" for step in steps)
    summary = service.store.get("wave_state", "wave-held-consumer")["summary"][consumer]
    assert summary["status"] == "handoff-waiting"
    assert summary["blocked"] is False
    assert summary["waiting"] == "consumer workspace is held by another owner"
    assert service.store.ownership(resource) == ("another-controller-task", epoch, "active")

    service.store.transition_owner(resource, "another-controller-task", epoch, "released")
    _complete_wave(service, "wave-held-consumer")
    assert service.store.get("result", consumer)["accepted"] is True


def test_handoff_refused_for_staged_changes_blocks_the_wave_until_resumed(tmp_path):
    config, _primary = _wave_config(tmp_path)
    service = Service(config)
    wave = service.submit_wave_plan("usage", "wave-staged")["wave"]
    producer, consumer = wave["tasks"]["producer"], wave["tasks"]["consumer"]
    workspace = _consumer_workspace(config)
    (tmp_path / "consumer" / "notes.txt").write_text("unrelated work in progress\n")
    subprocess.run(["git", "add", "notes.txt"], cwd=workspace, check=True)

    _wait_accepted(service, producer)
    ticks = _ticks(service, 20, start=100)

    assert all(tick["busy"] is False for tick in ticks)
    state = service.store.get("wave_state", "wave-staged")
    assert state["status"] == "blocked"
    blocker = state["summary"][consumer]["blocker"]
    assert blocker.startswith("unrelated staged changes in consumer workspace")
    # The refusal came before anything was written, so the workspace lock was released.
    resource = "workspace:" + str(os.path.realpath(workspace))
    assert service.store.ownership(resource)[2] == "released"

    subprocess.run(["git", "reset", "-q", "notes.txt"], cwd=workspace, check=True)
    _ticks(service, 3, start=200)
    assert service.store.get("wave_state", "wave-staged")["status"] == "blocked"

    resumed = service_wave.resume(service, "wave-staged")
    assert resumed["state"]["status"] == "running"
    _complete_wave(service, "wave-staged")
    assert service.store.get("result", consumer)["accepted"] is True


def test_handoff_failing_after_it_wrote_leaves_its_consumer_blocked_as_uncertain(
        tmp_path, monkeypatch):
    config, _primary = _wave_config(tmp_path)
    service = Service(config)
    wave = service.submit_wave_plan("usage", "wave-torn")["wave"]
    producer, consumer = wave["tasks"]["producer"], wave["tasks"]["consumer"]

    def torn(*_args, **_kwargs):
        raise OSError("write interrupted")

    # Only the in-process handoff is affected; the producer runs in its own worker process.
    monkeypatch.setattr("corral.execution.handoff.apply_manifest", torn)
    _wait_accepted(service, producer)
    ticks = _ticks(service, 20, start=100)

    assert all(tick["busy"] is False for tick in ticks)
    state = service.store.get("wave_state", "wave-torn")
    assert state["status"] == "blocked"
    assert state["summary"][consumer]["blocker"] == \
        "artifact handoff failed: write interrupted"
    resource = "workspace:" + str(os.path.realpath(_consumer_workspace(config)))
    assert service.store.ownership(resource)[2] == "uncertain"

    # Resuming re-evaluates the wave but never retries a handoff of unknown outcome, and the
    # recorded failure stays its reason.
    service_wave.resume(service, "wave-torn")
    step = _ticks(service, 1, start=200)[0]["waves"][0]
    assert step["status"] == "blocked"
    assert step["task_summary"][consumer]["blocker"] == \
        "artifact handoff failed: write interrupted"
    assert service.store.ownership(resource)[2] == "uncertain"
    assert service.store.get("result", consumer) is None


def test_handoff_interrupted_without_a_record_blocks_its_consumer_as_uncertain(tmp_path):
    from corral.execution.wave import handoff_key

    config, _primary = _wave_config(tmp_path)
    service = Service(config)
    wave = service.submit_wave_plan("usage", "wave-crashed")["wave"]
    producer, consumer = wave["tasks"]["producer"], wave["tasks"]["consumer"]
    # A service that stopped mid-transfer leaves the handoff's own ownership uncertain.
    resource = "workspace:" + str(os.path.realpath(_consumer_workspace(config)))
    owner = "wave-handoff:" + handoff_key(producer, "output.txt", consumer, "input.txt")
    service.store.transition_owner(resource, owner, service.store.acquire(resource, owner),
                                   "uncertain")

    _wait_accepted(service, producer)
    ticks = _ticks(service, 10, start=100)

    assert all(tick["busy"] is False for tick in ticks)
    state = service.store.get("wave_state", "wave-crashed")
    assert state["status"] == "blocked"
    assert state["summary"][consumer]["blocker"] == (
        "artifact handoff into input.txt has an uncertain outcome; "
        "reconcile the consumer workspace before retry")
    assert service.store.get("result", consumer) is None


def test_failing_wave_step_blocks_that_wave_and_the_others_still_advance(
        tmp_path, monkeypatch):
    config, _primary = _wave_config(tmp_path)
    _solo_plans(config)
    service = Service(config)
    service.submit_wave_plan("left", "w-1")
    right = service.submit_wave_plan("right", "w-2")["wave"]["tasks"]["right"]
    original = WaveRunner.step

    def step(runner, wave_id, *args, **kwargs):
        if wave_id == "w-1":
            raise RuntimeError("store record is unreadable")
        return original(runner, wave_id, *args, **kwargs)

    monkeypatch.setattr(WaveRunner, "step", step)
    monkeypatch.setattr("corral.execution.service_dispatch.launch", _live_launcher)

    ticks = _ticks(service, 5)

    assert service.store.get("wave_state", "w-1") == {
        "wave_id": "w-1", "status": "blocked",
        "error": "wave step failed: RuntimeError: store record is unreadable"}
    assert service.store.get("wave_dispatch", right)["status"] == "active"
    # The blocked wave is no longer stepped, and no tick raised.
    assert [step["wave_id"] for tick in ticks for step in tick["waves"]
            if step["status"] == "blocked"] == ["w-1"]
    assert service_wave.pending(service) is True
    assert service_wave.status(service, "w-1")["state"]["status"] == "blocked"


# -- B2: every eligible wave advances on a wave lane tick ---------------------------------

def test_two_waves_with_free_capacity_dispatch_concurrently(tmp_path, monkeypatch):
    config, _primary = _wave_config(tmp_path)
    _solo_plans(config)
    service = Service(config)
    left = service.submit_wave_plan("left", "w-1")["wave"]["tasks"]["left"]
    right = service.submit_wave_plan("right", "w-2")["wave"]["tasks"]["right"]
    monkeypatch.setattr("corral.execution.service_dispatch.launch", _live_launcher)

    first, second = _ticks(service, 2)

    # One launch per tick (max_dispatch_per_tick is 1), offered to each wave in turn.
    assert [d for step in first["waves"] for d in step["dispatched"]] == [
        {"task": left, "status": "active"}]
    assert [d for step in second["waves"] for d in step["dispatched"]] == [
        {"task": right, "status": "active"}]
    for task in (left, right):
        assert service.store.get("wave_dispatch", task)["status"] == "active"
        assert service.store.get("allocation", task)["active"] is True
    assert {service.store.get("wave_state", w)["status"] for w in ("w-1", "w-2")} == {"running"}


def test_two_waves_share_one_tick_dispatch_budget(tmp_path, monkeypatch):
    config, _primary = _wave_config(tmp_path)
    _solo_plans(config, max_dispatch=2)
    service = Service(config)
    left = service.submit_wave_plan("left", "w-1")["wave"]["tasks"]["left"]
    right = service.submit_wave_plan("right", "w-2")["wave"]["tasks"]["right"]
    monkeypatch.setattr("corral.execution.service_dispatch.launch", _live_launcher)

    first = service.tick(now=10)

    assert sorted(d["task"] for step in first["waves"] for d in step["dispatched"]) == \
        sorted([left, right])


def test_paused_wave_task_does_not_hold_back_another_wave(tmp_path, monkeypatch):
    config, _primary = _wave_config(tmp_path)
    _solo_plans(config)
    service = Service(config)
    left = service.submit_wave_plan("left", "w-1")["wave"]["tasks"]["left"]
    right = service.submit_wave_plan("right", "w-2")["wave"]["tasks"]["right"]
    service.controller.steer(service.token, left, "hold", {"pause_dispatch": True})
    monkeypatch.setattr("corral.execution.service_dispatch.launch", _live_launcher)

    _ticks(service, 4)

    # The paused task is still a live candidate, so its wave waits instead of blocking.
    assert service.store.get("wave_dispatch", left) is None
    assert service.store.get("wave_state", "w-1")["status"] == "running"
    assert service.store.get("wave_dispatch", right)["status"] == "active"

    service.controller.steer(service.token, left, "release", {"pause_dispatch": False})
    _ticks(service, 2, start=20)
    assert service.store.get("wave_dispatch", left)["status"] == "active"


def test_wave_route_the_host_does_not_serve_blocks_instead_of_waiting(tmp_path, monkeypatch):
    config, primary = _wave_config(tmp_path)
    service = Service(config)
    tasks = [{"name": "solo", "request_id": "route:solo", "spec": {
        **_defaults(config), "repo": "demo", "workspace": str(primary), "host": "primary",
        "mode": "wave"}}]
    task = service_wave.submit_advanced(service, "route", tasks)["tasks"]["solo"]
    # Submission resolves a profile against the host's routes; the host configuration can
    # still stop serving that route after the wave was admitted.
    request = service.store.get("request", task)
    service.store.replace("request", task, {
        **request, "selection": {"profile": {"route": "unserved-route"}}})
    monkeypatch.setattr("corral.execution.service_dispatch.launch", _live_launcher)

    step = service.tick(now=10)["waves"][0]

    assert step["status"] == "blocked"
    assert step["task_summary"][task]["blocker"] == \
        "route unserved-route is not served by execution host primary"


def test_service_leaves_a_wave_owned_by_the_controller_runner_alone(tmp_path, monkeypatch):
    config, _primary = _wave_config(tmp_path)
    _solo_plans(config)
    service = Service(config)
    left = service.submit_wave_plan("left", "w-1")["wave"]["tasks"]["left"]
    epoch = service.store.acquire("wave_runner:w-1", "controller-runner")
    monkeypatch.setattr("corral.execution.service_dispatch.launch", _live_launcher)

    assert service_wave.pending(service) is False
    assert _ticks(service, 2)[1]["waves"] == []
    assert service.store.get("wave_dispatch", left) is None
    assert service_wave.status(service, "w-1")["controller_runner"] is True

    service.store.transition_owner("wave_runner:w-1", "controller-runner", epoch, "released")
    service.tick(now=30)
    assert service.store.get("wave_dispatch", left)["status"] == "active"


# -- Nonblocking follow-ups --------------------------------------------------------------

def test_cancelled_wave_task_result_keeps_its_cancelled_label(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = Store(tmp_path / "state")
    task = "cancelled-with-result"
    store.replace("state", task, {"status": "cancelled"})
    store.put_once("claim", task, {"attempt": "attempt-1", "generation": 1})
    store.put_once("result", task, {"accepted": False, "terminal_status": "cancelled"})
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

    settled = store.get("wave_dispatch", task)
    assert settled["status"] == "terminal"
    assert settled["terminal_status"] == "cancelled"


def test_process_start_identity_does_not_depend_on_time_zone_or_locale(monkeypatch):
    pid = os.getpid()
    monkeypatch.setenv("TZ", "UTC")
    utc = process_start(pid)
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    monkeypatch.setenv("LC_ALL", "C")

    assert utc is not None
    assert process_start(pid) == utc
    assert process_status({"pid": pid, "process_start": utc}) == "alive"
    # An identity recorded before normalization, in this environment, still matches.
    legacy = process_start(pid, normalized=False)
    assert process_status({"pid": pid, "process_start": legacy}) == "alive"
    assert process_status({"pid": pid, "process_start": "Thu Jan  1 00:00:00 1970"}) == "dead"


def test_wave_status_and_resume_are_service_endpoint_actions(tmp_path, monkeypatch, capsys):
    config, _primary = _wave_config(tmp_path)
    _solo_plans(config)
    service = Service(config)
    left = service.submit_wave_plan("left", "w-1")["wave"]["tasks"]["left"]
    service.store.replace("wave_state", "w-1", {
        "wave_id": "w-1", "status": "blocked", "error": "operator fixed the cause"})

    def call(action):
        request = {"action": action, "wave_id": "w-1"}
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(request)))
        assert service_endpoint.main(["--config", str(config)]) == 0
        return json.loads(capsys.readouterr().out)

    status = call("wave-status")
    assert status["state"]["status"] == "blocked"
    assert status["plan"]["plan"] == "left"
    assert status["wave"]["tasks"] == {"left": left}

    resumed = call("wave-resume")
    assert resumed["state"] == {"wave_id": "w-1", "status": "running"}
    assert service_wave.pending(Service(config)) is True


def test_dead_before_claim_dispatch_recovers_through_a_controller_dispatch(
        tmp_path, monkeypatch):
    from corral.execution import service_dispatch

    from .test_execution_wave_service_scheduler import _dead_launcher

    config, _primary = _wave_config(tmp_path)
    service = Service(config)
    producer = service.submit_wave_plan("usage", "wave-recover")["wave"]["tasks"]["producer"]
    launch = service_dispatch.launch
    monkeypatch.setattr("corral.execution.service_dispatch.launch", _dead_launcher)
    _ticks(service, 2)
    assert service.store.get("wave_dispatch", producer)["status"] == "uncertain"
    assert service.store.get("wave_state", "wave-recover")["status"] == "blocked"

    # No worker can have started without a claim, so the task is dispatched directly
    # (what the controller's ``dispatch`` action runs), and reconciliation settles the
    # wave dispatch from its result and reopens the wave.
    assert service.controller.run(service.token, producer,
                                  execution_host="primary")["result"]["accepted"]
    monkeypatch.setattr("corral.execution.service_dispatch.launch", launch)
    _complete_wave(service, "wave-recover")
    assert service.store.get("wave_dispatch", producer)["terminal_status"] == "completed"


def test_agent_cli_wave_status_and_resume_call_the_service(monkeypatch, capsys):
    from corral.execution import agent_cli

    calls = []

    class Client:
        def call(self, action, **payload):
            calls.append((action, payload))
            return {"action": action}

    monkeypatch.setattr(agent_cli, "from_path", lambda _path: Client())
    for command in ("wave-status", "wave-resume"):
        assert agent_cli.main(["--config", "client.json", command, "--wave-id", "w-1"]) == 0
        assert json.loads(capsys.readouterr().out) == {"action": command}

    assert calls == [("wave-status", {"wave_id": "w-1"}), ("wave-resume", {"wave_id": "w-1"})]
