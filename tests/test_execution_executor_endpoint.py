import json
import subprocess
import sys

import pytest

from corral.execution import executor_endpoint
from corral.execution.store import Store


def endpoint(tmp_path):
    controller = tmp_path / "controller.json"
    if not controller.exists():
        controller.write_text(json.dumps({"state": str(tmp_path / "executor-state")}))
    return {
        "controller_config": str(controller),
        "executor_host": "dev",
        "allowed_repositories": ["corral"],
        "allowed_workspaces": [str(tmp_path / "registered")],
    }


def admitted(tmp_path, task_id, **overrides):
    """Record a request in the executor controller's store as its submit would."""
    spec = {"host": "dev", "repo": "corral", "workspace": str(tmp_path / "registered"),
            "logical_parent": "b" * 64, "authority_epoch": 1, **overrides}
    Store(tmp_path / "executor-state" / "controller.sqlite").put_once("request", task_id, spec)
    return spec


def submission(tmp_path):
    return {
        "action": "submit", "request_id": "controller:" + "a" * 64,
        "spec": {
            "host": "dev", "repo": "corral", "workspace": str(tmp_path / "registered"),
            "logical_parent": "b" * 64, "authority_epoch": 1,
        },
    }


def test_executor_endpoint_accepts_only_controller_bound_registered_submission(tmp_path):
    config, request = endpoint(tmp_path), submission(tmp_path)
    assert executor_endpoint.validate(config, request) is request
    for field, value, message in (
        ("host", "other", "host"),
        ("repo", "other", "repository"),
        ("workspace", str(tmp_path / "other"), "workspace"),
        ("logical_parent", "not-a-task", "authority"),
    ):
        changed = json.loads(json.dumps(request))
        changed["spec"][field] = value
        with pytest.raises(PermissionError, match=message):
            executor_endpoint.validate(config, changed)


def test_executor_endpoint_refuses_unscoped_actions_and_task_ids(tmp_path):
    config = endpoint(tmp_path)
    with pytest.raises(PermissionError, match="action"):
        executor_endpoint.validate(config, {"action": "run-wave", "wave_id": "wave"})
    with pytest.raises(PermissionError, match="exact task"):
        executor_endpoint.validate(config, {"action": "status", "task_id": "short"})
    admitted(tmp_path, "c" * 64)
    request = {"action": "status", "task_id": "c" * 64}
    assert executor_endpoint.validate(config, request) is request


@pytest.mark.parametrize("action", sorted(executor_endpoint.ACTIONS - {"submit"}))
def test_executor_endpoint_scopes_every_task_action_to_the_admitted_request(tmp_path, action):
    config = endpoint(tmp_path)
    # Before anything is admitted the store does not exist; the lookup must not create it.
    with pytest.raises(PermissionError, match="not registered"):
        executor_endpoint.validate(config, {"action": action, "task_id": "0" * 64})
    assert not (tmp_path / "executor-state").exists()
    admitted(tmp_path, "1" * 64)
    admitted(tmp_path, "2" * 64, repo="other-repository")
    admitted(tmp_path, "3" * 64, workspace=str(tmp_path / "unregistered"))
    admitted(tmp_path, "4" * 64, host="other-host")
    local = admitted(tmp_path, "5" * 64)
    local.pop("logical_parent")
    Store(tmp_path / "executor-state" / "controller.sqlite").replace("request", "5" * 64, local)

    request = {"action": action, "task_id": "1" * 64}
    assert executor_endpoint.validate(config, request) is request
    for task_id, message in (("0" * 64, "not registered"), ("2" * 64, "repository"),
                             ("3" * 64, "workspace"), ("4" * 64, "host"),
                             ("5" * 64, "logical authority")):
        with pytest.raises(PermissionError, match=message):
            executor_endpoint.validate(config, {"action": action, "task_id": task_id})


def test_executor_endpoint_refuses_cross_repository_task_before_running_controller(
        tmp_path, monkeypatch):
    raw = endpoint(tmp_path)
    config = tmp_path / "endpoint.json"
    config.write_text(json.dumps(raw))
    admitted(tmp_path, "e" * 64, repo="other-repository")
    request = {"action": "cancel", "task_id": "e" * 64}

    class Input:
        def read(self, _limit):
            return json.dumps(request).encode()

    def run(*_args, **_kwargs):
        raise AssertionError("an out-of-scope task must not reach the controller")

    monkeypatch.setattr(sys, "stdin", type("Stdin", (), {"buffer": Input()})())
    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(PermissionError, match="repository"):
        executor_endpoint.main(["--config", str(config)])


def test_executor_endpoint_invokes_pinned_module_in_isolated_mode(tmp_path, monkeypatch, capsys):
    raw = endpoint(tmp_path)
    config = tmp_path / "endpoint.json"
    config.write_text(json.dumps(raw))
    admitted(tmp_path, "d" * 64)
    request = {"action": "status", "task_id": "d" * 64}
    observed = {}

    class Input:
        def read(self, _limit):
            return json.dumps(request).encode()

    def run(command, **kwargs):
        observed.update(command=command, kwargs=kwargs)
        return subprocess.CompletedProcess(command, 0, stdout=b'{"ok":true}\n', stderr=b"")

    monkeypatch.setattr(sys, "stdin", type("Stdin", (), {"buffer": Input()})())
    monkeypatch.setattr(subprocess, "run", run)
    assert executor_endpoint.main(["--config", str(config)]) == 0
    assert observed["command"][1:4] == ["-I", "-m", "corral.execution.cli"]
    assert observed["kwargs"]["cwd"] == "/"
    assert "PYTHONPATH" not in observed["kwargs"]["env"]
    assert json.loads(capsys.readouterr().out) == {"ok": True}
