import json

import pytest

from corral.execution import agent_cli, service_wave
from corral.execution.store import Store


class FakeService:
    config = {"wave_plans": {"usage": {
        "enabled": True, "allow_objective_override": True,
        "allow_host_override": True, "objective_task": "implement",
        "tasks": [
            {"name": "implement", "repository": "corral", "objective": "Improve usage",
             "candidate_paths": ["usage.py"], "workspace_key": "producer"},
            {"name": "verify", "repository": "corral", "objective": "Verify usage",
             "role": "adjudication", "dependencies": ["implement"],
             "workspace_key": "consumer"},
        ],
        "handoffs": [{"producer": "implement", "producer_path": "usage.py",
                      "consumer": "verify", "consumer_path": "usage.py"}],
    }}}

    def _repository(self, name):
        assert name == "corral"
        return {"enabled": True, "allowed_roles": ["implementation", "adjudication"],
                "default_host": "primary", "allowed_hosts": ["primary", "dev"],
                "workspaces": {"primary": "/work/primary", "dev": "/work/dev"},
                "wave_workspaces": {
                    "primary": {"producer": "/wave/primary-producer",
                                "consumer": "/wave/primary-consumer"},
                    "dev": {"producer": "/wave/dev-producer",
                            "consumer": "/wave/dev-consumer"}},
                "task_defaults": {"role": "implementation", "profile_id": "impl"}}

    def _host_workspace(self, repo, requested):
        host = requested or repo["default_host"]
        if host not in repo["allowed_hosts"]:
            raise PermissionError("unsupported host")
        return host, repo["workspaces"][host]


def test_named_plan_resolves_registered_defaults_dependencies_and_host():
    tasks, handoffs = service_wave.resolve(
        FakeService(), "usage", "wave-1", objective="Improve provider deltas", host="dev")
    assert [item["name"] for item in tasks] == ["implement", "verify"]
    assert tasks[0]["spec"]["objective"] == "Improve provider deltas"
    assert tasks[0]["spec"]["workspace"] == "/wave/dev-producer"
    assert tasks[1]["spec"]["workspace"] == "/wave/dev-consumer"
    assert tasks[1]["spec"]["dependencies"] == ["implement"]
    assert handoffs[0]["consumer"] == "verify"


def test_named_plan_refuses_unknown_dependency_or_unregistered_host():
    service = FakeService()
    service.config = json.loads(json.dumps(service.config))
    service.config["wave_plans"]["usage"]["tasks"][1]["dependencies"] = ["missing"]
    with pytest.raises(ValueError, match="unknown dependencies"):
        service_wave.resolve(service, "usage", "wave-1")
    with pytest.raises(PermissionError, match="unsupported host"):
        service_wave.resolve(FakeService(), "usage", "wave-1", host="other")


def test_cli_named_wave_uses_endpoint_without_spec_file(tmp_path, monkeypatch, capsys):
    calls = []

    class Client:
        def call(self, action, **payload):
            calls.append((action, payload))
            return {"accepted": True}

    monkeypatch.setattr(agent_cli, "from_path", lambda _path: Client())
    config = tmp_path / "client.json"
    config.write_text("{}")
    assert agent_cli.main(["--config", str(config), "wave", "--plan", "usage",
                           "--wave-id", "wave-2", "--host", "primary"]) == 0
    assert calls == [("wave-plan", {"wave_id": "wave-2", "plan": "usage",
                                     "objective": None, "host": "primary"})]
    assert json.loads(capsys.readouterr().out) == {"accepted": True}


def test_wave_dispatch_claim_is_idempotent_and_progress_is_detached(tmp_path, monkeypatch):
    launched = []

    class Controller:
        hosts = {"primary": {}}

        def context(self, task):
            return {"host": "primary", "workspace": str(tmp_path / task)}

        def capacity(self, _host):
            return {"cpu": 2, "memory_mb": 0}

        def status(self, _token, _task):
            return {"state": {"status": "submitted"}}

    class Service:
        store = Store(tmp_path / "state")
        controller = Controller()
        controller_path = tmp_path / "controller.json"
        token = "owner"
        execution_host = "primary"
        development_mode = False
        max_dispatch = 1

    service = Service()
    monkeypatch.setattr("corral.execution.service_dispatch.launch", lambda *_args, **_kwargs: (
        launched.append("one") or {"pid": 123, "process_start": "fixture"}))
    assert service_wave._dispatch(service, "wave-1", "task-1", "primary") == "active"
    assert service_wave._dispatch(service, "wave-1", "task-1", "primary") == "active"
    assert launched == ["one"]
    # The dispatch holds its capacity in the store until a controller dispatch adopts it.
    assert service.store.get("allocation", "task-1") == {
        "host": "primary", "cpu": 1, "memory_mb": 0, "active": True, "reservation": "task-1"}

    service.store.replace("wave_state", "wave-1", {"status": "running"})
    service.store.replace("wave", "wave-1", {"task_ids": ["task-2"]})
    observed = []
    monkeypatch.setattr(service_wave, "reconcile_dispatches", lambda _service: set())
    monkeypatch.setattr(service_wave.WaveRunner, "step", lambda _runner, wave, host, dispatcher,
                        running, capacity, dispatch_limit: (
        observed.append((wave, host, dispatcher("wave-1", "task-2", "primary"),
                         [item["reservation"] for item in running], capacity))
        or {"wave_id": wave, "status": "running"}))
    progressed = service_wave.progress(service)
    assert progressed == [{"wave_id": "wave-1", "status": "running"}]
    assert observed == [("wave-1", "primary", "active", ["task-1"], {"cpu": 2, "memory_mb": 0})]
    assert launched == ["one", "one"]


def test_busy_interactive_lane_cannot_starve_wave_lane(tmp_path):
    store = Store(tmp_path / "fair-state")
    lanes = [service_wave.select_lane(store, service_ready=True, wave_ready=True)
             for _ in range(6)]
    assert lanes == ["service", "wave", "service", "wave", "service", "wave"]
