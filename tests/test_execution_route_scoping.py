"""Route scope and credential forwarding refusals without provider calls."""
import dataclasses
import json
import os
import sys

import pytest

from corral.execution import routes
from corral.execution.adapter import _harness_env
from corral.execution.process import Process
from corral.execution.service import Service
from corral.execution.service_validation import validate

from . import native_support as ns


def _service(tmp_path, env, repository="fixture-native"):
    controller = {"state": str(tmp_path / "service-state"), "token": "owner",
                  "default_host": ns.FAKE_HOST,
                  "hosts": {ns.FAKE_HOST: env["host"]},
                  "profiles": [dataclasses.asdict(env["profile"])]}
    controller_path = tmp_path / "controller.json"
    controller_path.write_text(json.dumps(controller))
    config = {"controller_config": str(controller_path), "repositories": {
        repository: {"workspaces": {ns.FAKE_HOST: str(env["workspace"])},
                     "task_defaults": {"profile_id": env["profile"].id}}}}
    service_path = tmp_path / "service.json"
    service_path.write_text(json.dumps(config))
    return Service(service_path), service_path


def test_route_declaration_rejects_bad_scope_and_packet_limit(tmp_path):
    env = ns.native_env(tmp_path)
    raw = env["host"]["native_routes"][ns.FAKE_ROUTE]
    for change in ({"allowed_repositories": "fixture-native"},
                   {"allowed_repositories": [""]},
                   {"allowed_workdirs": ["relative"]},
                   {"allowed_workdirs": [str(tmp_path / ".." / "outside")]},
                   {"max_packet_bytes": True}, {"max_packet_bytes": 0}):
        with pytest.raises(PermissionError):
            routes.declare(ns.FAKE_ROUTE, {**raw, **change})


def test_scope_checks_service_admission_and_realpath_escape(tmp_path):
    env = ns.native_env(tmp_path)
    raw = env["host"]["native_routes"][ns.FAKE_ROUTE]
    raw.update(allowed_repositories=["other"], allowed_workdirs=[str(env["workspace"])])
    service, config = _service(tmp_path, env)
    with pytest.raises(PermissionError, match="repository profile"):
        service.submit("denied", "fixture-native", "Implement task")
    assert service.store.records("service_event") == {}
    report = validate(config)
    assert not report["valid"]
    assert any("excludes it" in error for error in report["errors"])
    raw["allowed_repositories"] = ["fixture-native"]
    outside = tmp_path / "outside"
    outside.mkdir()
    alias = env["workspace"] / "escape"
    alias.symlink_to(outside, target_is_directory=True)
    route = routes.declare(ns.FAKE_ROUTE, raw)
    with pytest.raises(PermissionError, match="workspace"):
        routes.enforce_scope(route, "fixture-native", alias)
    controller_path = tmp_path / "controller.json"
    controller_config = json.loads(controller_path.read_text())
    controller_config["hosts"][ns.FAKE_HOST]["native_routes"][ns.FAKE_ROUTE] = raw
    controller_path.write_text(json.dumps(controller_config))
    service_config = json.loads(config.read_text())
    service_config["repositories"]["fixture-native"]["workspaces"][ns.FAKE_HOST] = str(alias)
    config.write_text(json.dumps(service_config))
    scoped_service = Service(config)
    with pytest.raises(PermissionError, match="workspace"):
        scoped_service.submit("escaped", "fixture-native", "Implement task")


def test_validate_warns_on_unscoped_writer_and_rejects_packet_limit(tmp_path):
    env = ns.native_env(tmp_path)
    _, config = _service(tmp_path, env)
    report = validate(config)
    assert report["valid"] and any("no scoping" in item for item in report["warnings"])
    raw = json.loads(config.read_text())
    raw["repositories"]["fixture-native"]["max_packet_bytes"] = False
    config.write_text(json.dumps(raw))
    report = validate(config)
    assert not report["valid"]
    assert any("max_packet_bytes" in item for item in report["errors"])


def test_scope_drift_is_refused_before_launch(tmp_path):
    env = ns.native_env(tmp_path)
    raw = env["host"]["native_routes"][ns.FAKE_ROUTE]
    raw["allowed_repositories"] = ["fixture-native"]
    controller = env["controller"]
    task = controller.submit("owner", "scope-drift", ns.native_spec(env))
    raw["allowed_repositories"] = ["other"]
    with pytest.raises(PermissionError, match="repository profile"):
        controller.run("owner", task, execution_host=ns.FAKE_HOST)
    assert controller.store.get("state", task)["status"] == "refused-before-launch"
    assert controller.store.get("state", task).get("pid") is None


def test_process_scrubs_undeclared_credentials_and_forwards_declared(tmp_path, monkeypatch):
    monkeypatch.setenv("FOO_API_KEY", "parent-fixture")
    monkeypatch.setattr(Process, "_identity", lambda self, command: {})
    output = tmp_path / "env.json"
    code = ("import json, os, sys; "
            "json.dump({'key': os.getenv('FOO_API_KEY'), "
            "'password': os.getenv('DB_PASSWORD')}, open(sys.argv[1], 'w'))")
    for declared, expected in (((), None), (("FOO_API_KEY",), "parent-fixture")):
        with (tmp_path / "stdout").open("wb") as stdout, (tmp_path / "stderr").open("wb") as stderr:
            child = Process([sys.executable, "-c", code, str(output)], str(tmp_path),
                            stdout, stderr, env={"FOO_API_KEY": os.environ["FOO_API_KEY"],
                                                 "DB_PASSWORD": "fixture"},
                            credential_env_names=declared)
            assert child.wait() == 0
        assert json.loads(output.read_text()) == {"key": expected, "password": None}


def test_adapter_runtime_environment_cannot_bypass_credential_declaration(tmp_path, monkeypatch):
    monkeypatch.setenv("FOO_API_KEY", "declared-fixture")
    plan = {"route": {"runtime_env": ["FOO_API_KEY=runtime-fixture"]},
            "credential_env": []}
    env, forwarded, missing = _harness_env(plan, None, None, tmp_path)
    assert "FOO_API_KEY" not in env
    assert forwarded == missing == []
    plan["credential_env"] = ["FOO_API_KEY"]
    env, forwarded, missing = _harness_env(plan, None, None, tmp_path)
    assert env["FOO_API_KEY"] == "declared-fixture"
    assert forwarded == ["FOO_API_KEY"] and missing == []


def test_credential_names_cover_keys_sockets_and_credential_files():
    from corral.execution import credential_env

    for name in ("NVIDIA_KEY", "BROKER_TOKEN", "SERVICE_PASSWORD", "SSH_AUTH_SOCK",
                 "GOOGLE_APPLICATION_CREDENTIALS", "REGISTRY_PAT", "APP_SECRET_VALUE"):
        assert credential_env.is_credential(name), name
    for name in ("PATH", "HOME", "LANG", "TMPDIR", "PYTHONDONTWRITEBYTECODE"):
        assert not credential_env.is_credential(name), name
    assert credential_env.scrub({"NVIDIA_KEY": "x", "PATH": "/bin"}, ["NVIDIA_KEY"]) == {
        "NVIDIA_KEY": "x", "PATH": "/bin"}


def test_scope_names_profiles_and_requires_a_workspace(tmp_path):
    env = ns.native_env(tmp_path)
    raw = env["host"]["native_routes"][ns.FAKE_ROUTE]
    with pytest.raises(PermissionError, match="repository profiles"):
        routes.declare(ns.FAKE_ROUTE, {**raw, "allowed_repositories": ["owner/repo"]})
    route = routes.declare(ns.FAKE_ROUTE, {**raw, "allowed_workdirs": [str(env["workspace"])]})
    with pytest.raises(PermissionError, match="scoped workspace"):
        routes.enforce_scope(route, "fixture-native", None)


def test_git_environment_drops_redirects_and_credentials(monkeypatch):
    from corral.execution import git_hardening

    for name, value in {"GIT_DIR": "/elsewhere", "GIT_CONFIG_PARAMETERS": "'core.hooksPath'='x'",
                        "GIT_SSH_COMMAND": "ssh -o BatchMode=yes", "SSH_AUTH_SOCK": "/tmp/agent",
                        "SERVICE_API_KEY": "k", "GH_TOKEN": "t", "PATH": "/usr/bin"}.items():
        monkeypatch.setenv(name, value)
    env = git_hardening.git_environment({"GH_TOKEN": "explicit"})
    assert "GIT_DIR" not in env and "GIT_CONFIG_PARAMETERS" not in env
    assert "SERVICE_API_KEY" not in env
    assert env["GIT_SSH_COMMAND"] == "ssh -o BatchMode=yes"
    assert env["SSH_AUTH_SOCK"] == "/tmp/agent"
    assert env["GH_TOKEN"] == "explicit"
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
