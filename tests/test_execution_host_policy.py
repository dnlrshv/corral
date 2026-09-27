"""Host scheduling and launch policy at service boundaries."""
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from corral.execution.host_policy import blackout, priority_command
from corral.execution.process import Process
from corral.execution.service_dispatch import launch
from corral.execution.service import Service
from corral.execution import service_wave
from corral.execution.service_validation import validate
from .test_execution_service_cli import configs, git_repo
from .test_execution_service_dispatch import _two_repositories
from .test_execution_wave_service_scheduler import _wave_config


def _window(*, start="09:00", end="10:00", zone="UTC"):
    return {"days": ["mon"], "start": start, "end": end,
            "timezone": zone, "policy": "finish"}


def _epoch(year, month, day, hour, minute, zone="UTC", fold=0):
    return datetime(year, month, day, hour, minute, tzinfo=ZoneInfo(zone),
                    fold=fold).timestamp()


def test_blackout_tick_queues_then_dispatches(tmp_path, monkeypatch):
    workspace = git_repo(tmp_path / "repo")
    config = configs(tmp_path, workspace)
    raw = json.loads(config.read_text())
    raw["blackout_windows"] = [_window()]
    config.write_text(json.dumps(raw))
    service = Service(config)
    service.submit("queued", "demo", "do work")
    launches = []
    monkeypatch.setattr("corral.execution.service_dispatch.launch",
                        lambda *_args, **_kwargs: launches.append(True) or {"pid": 1})
    inside = service.tick(_epoch(2026, 9, 28, 9, 30))
    assert inside["blackout"]["active"] is True
    assert inside["dispatched"] == []
    assert service.status("queued")["event"]["status"] == "prepared"
    assert service.status("queued", now=_epoch(2026, 9, 28, 9, 30))["blackout"] == inside["blackout"]
    assert launches == []
    outside = service.tick(_epoch(2026, 9, 28, 10, 0))
    assert outside["blackout"]["active"] is False
    assert outside["dispatched"] == ["queued"]
    assert len(launches) == 1


def test_schedule_and_wave_wait_through_blackout(tmp_path, monkeypatch):
    config, _workspace = _wave_config(tmp_path)
    raw = json.loads(config.read_text())
    raw["blackout_windows"] = [_window()]
    raw["schedules"] = [{"id": "periodic", "interval_seconds": 3600,
                         "repository": "demo", "objective": "scheduled work"}]
    config.write_text(json.dumps(raw))
    service = Service(config)
    service.submit_wave_plan("usage", "wave-pending")
    launches = []
    monkeypatch.setattr("corral.execution.service_dispatch.launch",
                        lambda *_args, **_kwargs: launches.append(True))
    result = service.tick(_epoch(2026, 9, 28, 9, 30))
    assert len(result["created"]) == 1
    assert result["waves"] == result["dispatched"] == []
    assert result["pending"] == result["created"]
    assert service.store.records("wave_dispatch") == {}
    assert launches == []


def test_blackout_reconciles_wave_without_launching_next_step(tmp_path, monkeypatch):
    config, _workspace = _wave_config(tmp_path)
    raw = json.loads(config.read_text())
    raw["blackout_windows"] = [_window()]
    config.write_text(json.dumps(raw))
    service = Service(config)
    producer = service.submit_wave_plan("usage", "wave-reconcile")["wave"]["tasks"]["producer"]
    launches = []
    monkeypatch.setattr("corral.execution.service_dispatch.launch",
                        lambda *_args, **_kwargs: launches.append(True) or {"pid": 123})
    assert service_wave._dispatch(service, "wave-reconcile", producer, "primary") == "active"
    monkeypatch.setattr("corral.execution.runtime_identity.process_status",
                        lambda _identity: "dead")
    result = service.tick(_epoch(2026, 9, 28, 9, 30))
    assert result["waves"] == result["dispatched"] == []
    assert service.store.get("wave_dispatch", producer)["status"] == "uncertain"
    assert service.store.get("allocation", producer)["active"] is False
    assert len(launches) == 1


def test_midnight_and_dst_boundaries():
    overnight = _window(start="23:00", end="02:00")
    assert blackout([overnight], _epoch(2026, 9, 29, 1, 30))["active"]
    assert not blackout([overnight], _epoch(2026, 9, 29, 2, 0))["active"]
    spring = {**_window(start="01:30", end="02:30", zone="Europe/Zurich"),
              "days": ["sun"]}
    active = blackout([spring], _epoch(2026, 3, 29, 1, 45, "Europe/Zurich"))
    assert active["active"]
    assert active["until"] == datetime(2026, 3, 29, 1, tzinfo=ZoneInfo("UTC")).isoformat()
    autumn = {**spring, "start": "02:00", "end": "03:00"}
    assert blackout([autumn], _epoch(2026, 10, 25, 2, 30,
                                    "Europe/Zurich", fold=1))["active"]
    assert blackout([autumn], _epoch(2026, 10, 25, 2, 30,
                                    "Europe/Zurich", fold=1))["until"] == (
        datetime(2026, 10, 25, 2, tzinfo=ZoneInfo("UTC")).isoformat())


@pytest.mark.parametrize("field,value", [
    ("start", "24:00"), ("timezone", "Missing/Zone"), ("policy", "hold"),
    ("days", ["noday"]),
])
def test_validate_rejects_invalid_window(tmp_path, field, value):
    workspace = git_repo(tmp_path / "repo")
    config = configs(tmp_path, workspace)
    raw = json.loads(config.read_text())
    raw["blackout_windows"] = [{**_window(), field: value}]
    config.write_text(json.dumps(raw))
    assert not validate(config)["valid"]


def test_host_cap_counts_two_repositories_in_one_store(tmp_path):
    service = _two_repositories(tmp_path)
    service.controller.hosts["primary"]["max_concurrent_seats"] = 1
    service.submit("first", "demo", "one")
    service.submit("second", "other", "two")
    assert service._claim("first", 1, {"pid": 1}) is not None
    assert service._claim("second", 1, {"pid": 1}) is None
    with pytest.raises(PermissionError, match="seat capacity"):
        service.store.allocate("direct-task", "primary", 1, 0,
                               service.controller.capacity("primary"))
    service.store.release_allocation(service.store.get("service_event", "first")["task_id"])
    assert service._claim("second", 2, {"pid": 1}) is not None


def test_priority_wraps_launch_command(monkeypatch):
    monkeypatch.setattr("corral.execution.host_policy.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("corral.execution.host_policy.platform.system", lambda: "Darwin")
    assert priority_command(["/bin/worker"], {"nice": 5, "low_priority_io": True}) == [
        "/usr/bin/taskpolicy", "-b", "/usr/bin/nice", "-n", "5", "/bin/worker"]


def test_launcher_receives_priority_prefix(tmp_path, monkeypatch):
    seen = []

    class Child:
        pid = 123

    monkeypatch.setattr("corral.execution.host_policy.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("corral.execution.service_dispatch.subprocess.Popen",
                        lambda argv, **kwargs: seen.append((argv, kwargs)) or Child())
    monkeypatch.setattr("corral.execution.service_dispatch.launched",
                        lambda pid, _executable, host: {"pid": pid, "host": host})
    launch(tmp_path / "controller.json", tmp_path, "task", "primary",
           process_priority={"nice": 4, "low_priority_io": False})
    assert seen[0][0][:3] == ["/usr/bin/nice", "-n", "4"]
    assert seen[0][1]["start_new_session"] is True


def test_seat_command_and_environment_receive_priority(tmp_path, monkeypatch):
    seen = []

    class Child:
        pid = 123

    monkeypatch.setattr("corral.execution.host_policy.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("corral.execution.process.subprocess.Popen",
                        lambda argv, **kwargs: seen.append((argv, kwargs)) or Child())
    monkeypatch.setattr(Process, "_identity", lambda _self, _command: {"pid": 123})
    Process(["/bin/worker"], tmp_path, None, None, env={"EXAMPLE": "present"},
            process_priority={"nice": 7, "low_priority_io": False})
    assert seen[0][0] == ["/usr/bin/nice", "-n", "7", "/bin/worker"]
    assert seen[0][1]["env"]["EXAMPLE"] == "present"


def test_validate_rejects_missing_priority_tool(tmp_path, monkeypatch):
    workspace = git_repo(tmp_path / "repo")
    config = configs(tmp_path, workspace)
    controller_path = tmp_path / "controller.json"
    controller = json.loads(controller_path.read_text())
    controller["hosts"]["primary"]["process_priority"] = {
        "nice": 0, "low_priority_io": True}
    controller_path.write_text(json.dumps(controller))
    monkeypatch.setattr("corral.execution.host_policy.shutil.which", lambda _name: None)
    assert any("low_priority_io tool" in error for error in validate(config)["errors"])
