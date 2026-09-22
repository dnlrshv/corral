"""Regression for trusted-export admission reaching the installed scheduler."""
import os
from pathlib import Path

import pytest

from corral.execution.service import Service
from corral.execution.service_specs import request_spec
from .test_execution_trusted_pr import pr_fixture, repository_config, service_config


def admitted_review(tmp_path, monkeypatch):
    _, remote, fixture, _, _, _, fake_gh = pr_fixture(tmp_path)
    monkeypatch.setenv("CORRAL_PR_FIXTURE", str(fixture))
    service = Service(service_config(tmp_path, repository_config(tmp_path, remote, fake_gh)))
    service.store.acquire("pr:fixture/repo#7", "corral")
    admitted = service.submit_pr_review("demo", 7, "advisory")
    event = admitted["event"]
    assert "workspace" not in event["resolved_spec"]
    spec = request_spec(service.store, event)
    assert spec["workspace_kind"] == "immutable_snapshot"
    assert not (Path(spec["workspace"]) / ".git").exists()
    return service, event, spec


def test_real_pr_admission_dispatches_same_task_once_after_service_restart(tmp_path, monkeypatch):
    service, event, spec = admitted_review(tmp_path, monkeypatch)
    launches = []
    monkeypatch.setattr("corral.execution.service_dispatch.launch",
                        lambda _config, _state, task, host, **_kw:
                        launches.append((task, host)) or {"pid": os.getpid()})
    monkeypatch.setattr("corral.execution.runtime_identity.alive", lambda _value: True)
    restarted = Service(service.config_path)
    assert restarted.tick(now=10)["dispatched"] == [event["event_id"]]
    assert restarted.tick(now=11)["dispatched"] == []
    reused = restarted.submit_pr_review("demo", 7, "advisory")
    assert reused["event"]["task_id"] == event["task_id"]
    assert reused["event"]["resolved_spec"] == event["resolved_spec"]
    assert launches == [(event["task_id"], "mini2")]
    assert restarted.store.records("request") == {event["task_id"]: spec}
    assert restarted.store.records("invocation") == {}


def test_snapshot_cancellation_waits_for_resolved_workspace_owner(tmp_path, monkeypatch):
    service, event, spec = admitted_review(tmp_path, monkeypatch)
    task = event["task_id"]
    resource = "workspace:" + str(Path(spec["workspace"]).resolve())
    epoch = service.store.acquire(resource, task)
    service._claim(event["event_id"], 10, {"pid": os.getpid()})
    service.controller.cancel(service.token, task)
    service.store.replace("state", task, {"status": "cancelled", "generation": 1})
    service._reconcile({})
    assert service.status(event["event_id"])["event"]["status"] == "uncertain"
    service.store.transition_owner(resource, task, epoch, "released")
    service._reconcile({})
    assert service.status(event["event_id"])["event"]["status"] == "cancelled"


def test_claim_uses_authoritative_capacity_and_refuses_missing_request(tmp_path, monkeypatch):
    service, event, spec = admitted_review(tmp_path, monkeypatch)
    spec["cpu"] = service.controller.hosts["mini2"]["cpu"] + 1
    service.store.replace("request", event["task_id"], spec)
    assert service._claim(event["event_id"], 10, {}) is None
    assert service.tick(now=10)["dispatched"] == []
    with pytest.raises(ValueError, match="no authoritative request"):
        request_spec(service.store, {**event, "task_id": "missing"})
    with pytest.raises(PermissionError, match="host differs"):
        request_spec(service.store, {**event, "host": "different"})
