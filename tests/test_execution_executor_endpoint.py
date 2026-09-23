import json
import subprocess
import sys

import pytest

from corral.execution import executor_endpoint


def endpoint(tmp_path):
    return {
        "controller_config": str(tmp_path / "controller.json"),
        "executor_host": "dev",
        "allowed_repositories": ["corral"],
        "allowed_workspaces": [str(tmp_path / "registered")],
    }


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
    request = {"action": "status", "task_id": "c" * 64}
    assert executor_endpoint.validate(config, request) is request


def test_executor_endpoint_invokes_pinned_module_in_isolated_mode(tmp_path, monkeypatch, capsys):
    raw = endpoint(tmp_path)
    config = tmp_path / "endpoint.json"
    config.write_text(json.dumps(raw))
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
