import json
import subprocess

import pytest

from corral.execution.agent import AgentConfig
from corral.execution.client import Client


def test_installed_remote_executor_uses_isolated_module_without_source_cwd(monkeypatch):
    observed = {}

    def run(command, **kwargs):
        observed.update(command=command, kwargs=kwargs)
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps({"ok": True}), stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    client = Client({"python": "/opt/corral/bin/python", "transport": "ssh",
                     "ssh_host": "registered-dev", "ssh_options": ["-o", "ConnectTimeout=120"],
                     "controller_config": "/private/corral/controller.json"})
    assert client.call("status", task_id="task") == {"ok": True}
    remote = observed["command"][-1]
    assert " -I -m corral.execution.cli " in f" {remote} "
    assert not remote.startswith("cd ")
    assert observed["kwargs"]["cwd"] is None


def test_source_executor_requires_explicit_development_mode():
    with pytest.raises(ValueError, match="development_mode"):
        Client({"python": "python3", "transport": "local", "source": "/checkout",
                "controller_config": "/private/controller.json"}).call("status", task_id="task")


def test_agent_config_defaults_to_installed_runtime_without_checkout_source(tmp_path):
    production = AgentConfig(controller_config=tmp_path / "controller.json")
    assert production.as_client_config() == {
        "python": production.python,
        "controller_config": str(tmp_path / "controller.json"),
        "transport": "local",
    }
    development = AgentConfig(controller_config=tmp_path / "controller.json",
                              development_mode=True)
    assert development.as_client_config()["development_mode"] is True
    assert "source" in development.as_client_config()
