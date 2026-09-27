import io
import json
import re
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from corral import __version__
from corral.cli import main as corral_main
from corral.execution import cli as controller_cli
from corral.execution import executor_endpoint, service_command, service_endpoint
from corral.execution.store import Store, StoreSchemaError
from corral.protocol import (
    PROTOCOL_VERSION,
    STORE_SCHEMA_VERSION,
    check_min_protocol,
    response_with_protocol,
)


def test_protocol_constants_and_version_commands(capsys):
    assert (PROTOCOL_VERSION, STORE_SCHEMA_VERSION) == (1, 1)
    assert service_command.main(["version"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "version": __version__, "protocol": 1, "store_schema": 1}
    assert corral_main(["version"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "version": __version__, "protocol": 1, "store_schema": 1}


def test_version_strings_and_changelog_match():
    root = Path(__file__).resolve().parents[1]
    project = (root / "pyproject.toml").read_text()
    match = re.search(r'^version = "([^"]+)"$', project, re.MULTILINE)
    assert match and match.group(1) == __version__
    changelog = (root / "CHANGELOG.md").read_text()
    section = re.search(rf"(?ms)^## \[{re.escape(__version__)}\].*?(?=^## \[|\Z)", changelog)
    assert section and "### Consumer action" in section.group()


def test_min_protocol_gate_and_object_response():
    for minimum in (None, 0, 1):
        request = {"action": "status"}
        if minimum is not None:
            request["min_protocol"] = minimum
        check_min_protocol(request)
        assert request == {"action": "status"}
    for minimum in (2, None, True, "1", -1):
        with pytest.raises(ValueError, match="protocol" if minimum == 2 else "integer"):
            check_min_protocol({"min_protocol": minimum})
    assert response_with_protocol({"ok": True}) == {"ok": True, "protocol": 1}
    assert response_with_protocol({"protocol": 9}) == {"protocol": 9}
    assert response_with_protocol([1]) == [1]


def test_service_endpoint_rejects_newer_request_before_opening_service(
        tmp_path, monkeypatch):
    config = tmp_path / "service.json"
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({
        "action": "status", "event_id": "existing", "min_protocol": 2})))
    monkeypatch.setattr(service_endpoint, "Service", lambda _path: pytest.fail("opened service"))
    with pytest.raises(ValueError, match="requires protocol 2"):
        service_endpoint.main(["--config", str(config)])


def test_service_endpoint_accepts_supported_request_and_emits_protocol(
        tmp_path, monkeypatch, capsys):
    class Service:
        def status(self, event_id):
            assert event_id == "existing"
            return {"event": {"status": "completed"}}

    monkeypatch.setattr(service_endpoint, "Service", lambda _path: Service())
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({
        "action": "status", "event_id": "existing", "min_protocol": 1})))
    assert service_endpoint.main(["--config", str(tmp_path / "service.json")]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "event": {"status": "completed"}, "protocol": 1}


def test_installed_service_validate_command(tmp_path, capsys):
    controller = tmp_path / "controller.json"
    controller.write_text(json.dumps({
        "state": str(tmp_path / "state"), "token": "fixture",
        "hosts": {"local": {}}, "default_host": "local"}))
    config = tmp_path / "service.json"
    config.write_text(json.dumps({"controller_config": str(controller)}))
    assert service_command.main(["--config", str(config), "validate"]) == 0
    assert json.loads(capsys.readouterr().out) == {"valid": True, "protocol": 1}


def test_controller_endpoint_rejects_newer_protocol(tmp_path, monkeypatch):
    config = tmp_path / "controller.json"
    config.write_text(json.dumps({
        "state": str(tmp_path / "state"), "token": "fixture",
        "hosts": {"local": {}}, "default_host": "local"}))
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({
        "action": "status", "task_id": "unused", "min_protocol": 2})))
    with pytest.raises(ValueError, match="requires protocol 2"):
        controller_cli.main(["--config", str(config)])


def test_executor_endpoint_protocol_gate_precedes_scope_lookup():
    with pytest.raises(ValueError, match="requires protocol 2"):
        executor_endpoint.validate({}, {"action": "status", "min_protocol": 2})
    with pytest.raises(PermissionError, match="exact task"):
        executor_endpoint.validate({}, {"action": "status", "min_protocol": 1})


def test_fresh_reopen_and_legacy_store_version(tmp_path):
    path = tmp_path / "store.sqlite"
    Store(path).put_once("fixture", "key", {"value": 1})
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
    assert Store(path).get("fixture", "key") == {"value": 1}
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        db.execute("PRAGMA user_version = 0")
    assert Store(path).get("fixture", "key") == {"value": 1}
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1


def test_newer_store_refuses_without_modification(tmp_path):
    path = tmp_path / "store.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version = 2")
    with pytest.raises(StoreSchemaError, match="newer Corral.*restore the pre-upgrade backup"):
        Store(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []


def test_concurrent_legacy_opens_set_version_once(tmp_path):
    path = tmp_path / "store.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE records(kind TEXT, key TEXT, value TEXT, PRIMARY KEY(kind,key))")
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert all(pool.map(lambda _: Store(path).path == path, range(4)))
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
