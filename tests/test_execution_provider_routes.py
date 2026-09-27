"""Provider declarations and service budgets without contacting providers."""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import stat
import subprocess
import threading
from pathlib import Path

import pytest

from corral.execution import adapter, native, routes, service_dispatch, service_wave
from corral.execution.service import Service
from corral.execution.service_validation import validate

from . import native_support as ns
from .test_execution_service_cli import git_repo


def test_every_example_route_declares():
    for path in sorted((Path(__file__).parents[1] / "examples" / "routes").glob("*.jsonc")):
        raw = re.sub(r"(?m)^\s*//.*\n", "", path.read_text())
        assert routes.declare(path.stem, json.loads(raw)).id == path.stem


def test_model_denials_and_version_declaration(tmp_path):
    env = ns.native_env(tmp_path)
    base = env["host"]["native_routes"][ns.FAKE_ROUTE]
    for change in ({"denied_models": [ns.FAKE_MODEL]},
                   {"denied_models": [ns.FAKE_MODEL[:3] + "*"]},
                   {"denied_models": ["bad*middle"]},
                   {"denied_models": [ns.FAKE_MODEL.upper()]},
                   {"denied_models": [ns.FAKE_MODEL[:3].upper() + "*"]},
                   {"min_cli_version": "1.2"},
                   {"min_cli_version": "1.2.3"},
                   {"version_argv": ["--version", "{prompt}"]}):
        with pytest.raises(PermissionError):
            routes.declare(ns.FAKE_ROUTE, {**base, **change})
    route = routes.declare(ns.FAKE_ROUTE, {**base, "min_cli_version": "1.2.3",
                                          "version_argv": ["--version"],
                                          "denied_models": ["retired-*"]})
    routes.enforce_min_version(route, "1.2.3")
    with pytest.raises(PermissionError, match="below required"):
        routes.enforce_min_version(route, "1.2.2")
    with pytest.raises(PermissionError, match="unparsable"):
        routes.enforce_min_version(route, None)


def test_version_probe_reprobes_changed_binary(tmp_path):
    binary = tmp_path / "version-cli"
    binary.write_text("#!/bin/sh\necho cli 1.2.3 later 9.9.9\n")
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    assert routes.probe_version(str(binary), ("--version",)) == "1.2.3"
    binary.write_text("#!/bin/sh\necho cli 1.2.4\n")
    changed = binary.stat().st_mtime_ns + 1_000_000_000
    os.utime(binary, ns=(changed, changed))
    assert routes.probe_version(str(binary), ("--version",)) == "1.2.4"


def test_version_probe_retries_timeout_and_unparsable_output(tmp_path, monkeypatch):
    binary = tmp_path / "version-cli"
    binary.write_text("#!/bin/sh\necho 1.2.3\n")
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    calls = []

    def probe(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(args[0], 5)
        if len(calls) == 2:
            return subprocess.CompletedProcess(args[0], 0, "unparsable", "")
        return subprocess.CompletedProcess(args[0], 0, "cli 1.2.3", "")

    monkeypatch.setattr(routes.subprocess, "run", probe)
    assert routes.probe_version(str(binary), ("--version",)) is None
    assert routes.probe_version(str(binary), ("--version",)) is None
    assert routes.probe_version(str(binary), ("--version",)) == "1.2.3"
    assert routes.probe_version(str(binary), ("--version",)) == "1.2.3"
    assert len(calls) == 3


def test_version_probe_resolves_path_and_rejects_spoofed_mtime(tmp_path, monkeypatch):
    binary = tmp_path / "version-cli"
    binary.write_text("#!/bin/sh\necho cli 1.2.3\n")
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    assert routes.probe_version(binary.name, ("--version",)) == "1.2.3"
    original = binary.stat()
    replacement = tmp_path / "replacement"
    replacement.write_text("#!/bin/sh\necho cli 2.0.0\n")
    replacement.chmod(replacement.stat().st_mode | stat.S_IEXEC)
    os.utime(replacement, ns=(original.st_atime_ns, original.st_mtime_ns))
    os.replace(replacement, binary)
    assert binary.stat().st_mtime_ns == original.st_mtime_ns
    assert routes.probe_version(binary.name, ("--version",)) == "2.0.0"


def test_envelope_tail_is_bounded_and_hashes_decoded_text(tmp_path, monkeypatch):
    output = tmp_path / "harness.stdout"
    with output.open("wb") as handle:
        handle.seek(8_000_000)
        handle.write(b"end:\xff")
    monkeypatch.setattr(Path, "read_bytes", lambda _path: pytest.fail("unbounded output read"))
    tail = adapter._read_tail_bytes(output, 8)
    assert tail == b"\0\0\0end:\xff"
    text, envelope_hash = adapter._decode_envelope(tail)
    assert text == "\0\0\0end:\ufffd"
    assert envelope_hash == hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert envelope_hash != hashlib.sha256(tail).hexdigest()


def test_old_cli_is_refused_before_worker_launch_with_reason(tmp_path, monkeypatch):
    env = ns.native_env(tmp_path)
    route = env["host"]["native_routes"][ns.FAKE_ROUTE]
    route.update(min_cli_version="2.0.0", version_argv=["--version"])
    monkeypatch.setattr(native.containment, "require", lambda _boundary: {"passed": True})
    monkeypatch.setattr(routes, "probe_version", lambda *_args, **_kwargs: "1.2.3")
    controller = env["controller"]
    task = controller.submit("owner", "old-cli", ns.native_spec(env))
    with pytest.raises(routes.CliVersionError, match="below required"):
        controller.run("owner", task, execution_host=ns.FAKE_HOST)
    state = controller.store.get("state", task)
    assert state["status"] == "refused-before-launch"
    assert "1.2.3" in state["reason"] and "2.0.0" in state["reason"]
    assert state.get("pid") is None
    assert controller.store.get("allocation", task)["active"] is False


def _service(tmp_path):
    env = ns.native_env(tmp_path)
    alternate = dataclasses.replace(env["profile"], id="alternate-profile",
                                    route="alternate-route", provider="alternate",
                                    account_ref="alternate-account")
    host = env["host"]
    host["cpu"] = 2
    host["routes"].append(alternate.route)
    host["native_routes"][alternate.route] = {
        **host["native_routes"][ns.FAKE_ROUTE], "provider": alternate.provider,
        "account_ref": alternate.account_ref}
    controller = {"state": str(tmp_path / "state"), "token": "owner",
                  "default_host": ns.FAKE_HOST, "hosts": {ns.FAKE_HOST: host},
                  "profiles": [dataclasses.asdict(env["profile"]), dataclasses.asdict(alternate)]}
    controller_path = tmp_path / "controller.json"
    controller_path.write_text(json.dumps(controller))
    repos = {}
    for name, profile in (("first", env["profile"]), ("second", env["profile"]),
                          ("third", alternate)):
        workspace = env["workspace"] if name == "first" else git_repo(tmp_path / name)
        repos[name] = {"workspaces": {ns.FAKE_HOST: str(workspace)},
                       "allowed_routes": [profile.route],
                       "task_defaults": {"profile_id": profile.id}}
    config = {"controller_config": str(controller_path), "repositories": repos,
              "max_dispatch_per_tick": 3, "provider_concurrency": {"fixture": 1}}
    config_path = tmp_path / "service.json"
    config_path.write_text(json.dumps(config))
    return Service(config_path), config_path


def test_repository_route_allowlist_and_validation(tmp_path):
    service, config = _service(tmp_path)
    assert validate(config)["valid"]
    with pytest.raises(PermissionError, match="excludes route"):
        service.submit("wrong", "first", "review", profile_id="alternate-profile")
    assert service.store.get("service_event", "wrong") is None
    raw = json.loads(config.read_text())
    raw["repositories"]["first"]["allowed_routes"] = ["alternate-route"]
    config.write_text(json.dumps(raw))
    assert any("excludes it" in item for item in validate(config)["errors"])


def test_provider_budget_spans_repositories_and_skips_busy_provider(tmp_path, monkeypatch):
    service, _ = _service(tmp_path)
    launched = []
    monkeypatch.setattr(service_dispatch, "launch", lambda _c, _s, task, _h, **_k:
                        launched.append(task) or {"pid": os.getpid()})
    for name in ("first", "second", "third"):
        service.submit(name, name, "inspect candidate")
    first = service.store.get("service_event", "first")["task_id"]
    second = service.store.get("service_event", "second")["task_id"]
    third = service.store.get("service_event", "third")["task_id"]
    assert set(service.tick(now=10)["dispatched"]) == {"first", "third"}
    assert service.status("second")["event"]["status"] == "prepared"
    assert service.store.get("allocation", first)["provider"] == "fixture"
    assert service.store.get("allocation", third)["provider"] == "alternate"
    # Terminal completion releases the provider allocation before the next claim.
    service.store.release_allocation(first)
    service.store.replace("service_event", "first", {
        **service.store.get("service_event", "first"), "status": "completed"})
    assert service.tick(now=11)["dispatched"] == ["second"]
    assert service.store.get("allocation", second)["provider"] == "fixture"
    assert launched == [first, third, second]


def test_wave_dispatch_uses_controller_resolved_provider_budget(tmp_path, monkeypatch):
    service, _ = _service(tmp_path)
    launched = []
    monkeypatch.setattr(service_dispatch, "launch", lambda _c, _s, task, _h, **_k:
                        launched.append(task) or {"pid": os.getpid()})
    task_ids = []
    for name in ("first", "second"):
        repo = service.repositories[name]
        task_ids.append(service.controller.submit(service.token, f"wave-{name}", {
            "repo": name, "service_repository_profile": name,
            "workspace": repo["workspaces"][ns.FAKE_HOST], "host": ns.FAKE_HOST,
            "objective": f"Inspect {name}", "candidate_paths": [],
            "profile_id": repo["task_defaults"]["profile_id"],
        }))
    first, second = task_ids
    assert service.controller.context(first)["selection"]["profile"]["provider"] == "fixture"
    assert service_wave._dispatch(service, "wave-1", first, ns.FAKE_HOST) == "active"
    assert service_wave._dispatch(service, "wave-1", second, ns.FAKE_HOST) == "capacity-unavailable"
    assert launched == [first]
    assert service.store.get("allocation", first)["provider"] == "fixture"
    assert service.store.get("allocation", second) is None


def test_invalid_provider_budget_is_reported(tmp_path):
    _service_instance, config = _service(tmp_path)
    raw = json.loads(config.read_text())
    raw["provider_concurrency"] = {"fixture": True}
    config.write_text(json.dumps(raw))
    assert any("provider_concurrency" in item for item in validate(config)["errors"])
    with pytest.raises(ValueError, match="provider_concurrency"):
        Service(config)


def test_validate_checks_version_fields_without_running_binary(tmp_path):
    _service_instance, config = _service(tmp_path)
    raw = json.loads(config.read_text())
    controller_path = Path(raw["controller_config"])
    controller = json.loads(controller_path.read_text())
    route = controller["hosts"][ns.FAKE_HOST]["native_routes"][ns.FAKE_ROUTE]
    marker = tmp_path / "version-was-run"
    binary = tmp_path / "version-probe"
    binary.write_text(f"#!/bin/sh\ntouch {marker}\necho 1.2.3\n")
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    route.update(binary=str(binary), min_cli_version="1.2.3", version_argv=["--version"])
    controller_path.write_text(json.dumps(controller))
    assert validate(config)["valid"]
    assert not marker.exists()
    route["version_argv"] = ["{prompt}"]
    controller_path.write_text(json.dumps(controller))
    assert any("version_argv" in item for item in validate(config)["errors"])
    assert not marker.exists()


def test_status_surfaces_result_provenance(tmp_path):
    service, _config = _service(tmp_path)
    service.submit("one", "first", "inspect one")
    task = service.store.get("service_event", "one")["task_id"]
    record = {"provider": "fixture", "route": ns.FAKE_ROUTE}
    service.store.put_once("result", task, {"provenance": record})
    assert service.status("one")["provenance"] == record


def test_concurrent_claims_share_one_provider_slot(tmp_path):
    first, config = _service(tmp_path)
    second = Service(config)
    first.submit("one", "first", "inspect one")
    first.submit("two", "second", "inspect two")
    barrier = threading.Barrier(3)
    claimed = []

    def claim(service, event_id):
        barrier.wait()
        claimed.append(service._claim(event_id, 10, {"pid": os.getpid()}) is not None)

    threads = [threading.Thread(target=claim, args=(first, "one")),
               threading.Thread(target=claim, args=(second, "two"))]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()
    assert sorted(claimed) == [False, True]
    active = [item for item in first.store.records("allocation").values()
              if item.get("active") and item.get("provider") == "fixture"]
    assert len(active) == 1
