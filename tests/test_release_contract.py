import io
import json
import re
import sqlite3
import sys
from dataclasses import asdict
from importlib import metadata
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from corral import __version__
from corral.cli import main as corral_main
from corral.execution import cli as controller_cli
from corral.execution import executor_endpoint, service_command, service_endpoint
from corral.execution.profiles import STANDARD_NATIVE_PROFILES
from corral.execution.store import Store, StoreSchemaError
from corral.protocol import (
    PROTOCOL_VERSION,
    STORE_SCHEMA_VERSION,
    check_min_protocol,
    response_with_protocol,
)


def test_protocol_constants_and_version_commands(capsys, monkeypatch):
    class Distribution:
        def read_text(self, _filename):
            return None

    monkeypatch.setattr(metadata, "distribution", lambda _name: Distribution())
    assert (PROTOCOL_VERSION, STORE_SCHEMA_VERSION) == (1, 1)
    assert service_command.main(["version"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "version": __version__, "commit": None, "protocol": 1, "store_schema": 1}
    assert corral_main(["version"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "version": __version__, "commit": None, "protocol": 1, "store_schema": 1}


@pytest.mark.parametrize("direct_url,expected", [
    ('{"url":"https://github.com/example/corral","vcs_info":'
     '{"vcs":"git","commit_id":"abc123"}}', "abc123"),
    ('{"url":"file:///checkout","dir_info":{"editable":true}}', None),
    ('{"vcs_info":{"vcs":"git","commit_id":"abc123"}}', None),
    ('{malformed', None),
    (None, None),
])
def test_version_commit_from_distribution(monkeypatch, capsys, direct_url, expected):
    class Distribution:
        def read_text(self, filename):
            assert filename == "direct_url.json"
            return direct_url

    monkeypatch.setattr(metadata, "distribution", lambda name: Distribution())
    for command in (lambda: service_command.main(["version"]),
                    lambda: corral_main(["version"])):
        assert command() == 0
        assert json.loads(capsys.readouterr().out)["commit"] == expected


def test_version_commit_missing_distribution(monkeypatch, capsys):
    def missing(_name):
        raise metadata.PackageNotFoundError("corral")

    monkeypatch.setattr(metadata, "distribution", missing)
    assert service_command.main(["version"]) == 0
    assert json.loads(capsys.readouterr().out)["commit"] is None


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
    config.write_text(json.dumps({"controller_config": controller.name}))
    before = {path.relative_to(tmp_path) for path in tmp_path.rglob("*")}
    assert service_command.main(["--config", str(config), "validate"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "valid": True, "errors": [], "warnings": [], "protocol": 1,
        "store": {"found": None, "supported": 1, "compatible": True}}
    assert {path.relative_to(tmp_path) for path in tmp_path.rglob("*")} == before


def test_validate_reports_route_profile_and_newer_store_without_writes(tmp_path, capsys):
    state = tmp_path / "state"
    state.mkdir()
    db_path = state / "controller.sqlite"
    with sqlite3.connect(db_path) as db:
        db.execute("PRAGMA user_version = 2")
    profile = {**asdict(STANDARD_NATIVE_PROFILES[0]), "id": "bad-route",
               "route": "missing-route"}
    controller = tmp_path / "controller.json"
    controller.write_text(json.dumps({
        "state": str(state), "token": "fixture", "default_host": "local",
        "hosts": {"local": {"native_routes": {"broken": {}}}},
        "profiles": [profile]}))
    config = tmp_path / "service.json"
    config.write_text(json.dumps({
        "controller_config": controller.name, "max_dispatch_per_tick": 0,
        "repositories": {"demo": {"default_host": "unknown-host",
                                  "allowed_hosts": ["unknown-host"],
                                  "review_policies": {"review": {"profile_id": "unknown-profile"}},
                                  "repair_policies": {"repair": {"profile_id": "unknown-profile"}}}}}))
    before = db_path.read_bytes()
    assert service_command.main(["--config", str(config), "validate"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["store"] == {"found": 2, "supported": 1, "compatible": False}
    assert "undeclared" in str(result["errors"])
    assert "unknown-profile" in str(result["errors"])
    assert "unknown-host" in str(result["errors"])
    assert "explicit binary" in str(result["errors"])
    assert "max_dispatch_per_tick" in str(result["errors"])
    assert db_path.read_bytes() == before


def test_validate_secret_value_is_absent_from_output(tmp_path, capsys):
    secret = tmp_path / "provider-secret.json"
    secret.write_text(json.dumps({"API_KEY": "dummy-secret-value"}))
    secret.chmod(0o600)
    controller = tmp_path / "controller.json"
    controller.write_text(json.dumps({
        "state": str(tmp_path / "state"), "token": "fixture",
        "hosts": {"local": {}}, "default_host": "local", "secret_env": str(secret)}))
    config = tmp_path / "service.json"
    config.write_text(json.dumps({"controller_config": controller.name}))
    assert service_command.main(["--config", str(config), "validate"]) == 0
    output = capsys.readouterr().out
    assert "dummy-secret-value" not in output
    assert json.loads(output)["valid"] is True


def test_validate_reads_legacy_store_without_creating_wal_sidecars(tmp_path, capsys):
    state = tmp_path / "state"
    state.mkdir()
    path = state / "controller.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA journal_mode=WAL")
    controller = tmp_path / "controller.json"
    controller.write_text(json.dumps({
        "state": str(state), "token": "fixture", "default_host": "local",
        "hosts": {"local": {}}}))
    config = tmp_path / "service.json"
    config.write_text(json.dumps({"controller_config": controller.name}))
    before = {entry.relative_to(tmp_path) for entry in tmp_path.rglob("*")}
    assert service_command.main(["--config", str(config), "validate"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["store"] == {"found": 0, "supported": 1, "compatible": True}
    assert {entry.relative_to(tmp_path) for entry in tmp_path.rglob("*")} == before


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
