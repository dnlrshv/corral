"""Regression coverage for evidence-bound cancellation reconciliation."""
import json
import subprocess

import pytest

from corral.execution import reconciliation_api
from corral.execution.client import Client
from corral.execution.controller import Controller
from corral.execution.demo import fixture_profiles
from corral.execution.github_advisory import GitHubAdvisoryTransport as NativeGitHubTransport
from corral.execution.recovery import reconcile
from corral.execution.reconciliation_api import controller_only_observation, reconcile_local
from corral.execution.store import digest
from tests.test_execution_regressions import bind_interrupted_attempt, spec


@pytest.fixture
def setup(tmp_path):
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=f@example.invalid",
                    "commit", "--allow-empty", "-qm", "fixture"], cwd=repo, check=True)
    controller = Controller(tmp_path / "state", "owner", {"fixture": {"routes": ["fixture"],
        "harnesses": ["synthetic"], "cpu": 4, "memory_mb": 1024,
        "reconciliation_delivery_reader": "controller-records-only"}}, default_host="fixture",
        profiles=fixture_profiles())
    return controller, repo


def _cancelled(controller, repo, name="cancelled"):
    task = controller.submit("owner", name, spec(repo))
    resource = "workspace:" + str(repo.resolve())
    epoch = controller.store.acquire(resource, task)
    controller.store.transition_owner(resource, task, epoch, "uncertain")
    state = {"status": "uncertain", "attempt": f"{name}-attempt", "epoch": epoch,
             "generation": 1, "pid": 99999991, "pgid": 99999991}
    bind_interrupted_attempt(controller, task, state)
    controller.store.replace("state", task, state)
    controller.cancel("owner", task)
    return task, state, resource


def _terminal(state):
    return {"process": {"identity": {"pid": state["pid"], "pgid": state["pgid"],
                                     "attempt": state["attempt"]}, "status": "absent"},
            "artifact": {"status": "preserved"},
            "delivery": {"searched": True, "status": "absent"}}


def test_cancelled_settlement_cas_refuses_state_changed_after_collection(setup):
    controller, repo = setup
    task, state, resource = _cancelled(controller, repo, "state-cas")

    def changed(_task, observed):
        controller.store.replace("state", task, {**observed, "error": "newer controller fact"})
        return _terminal(observed)

    with pytest.raises(PermissionError, match="state changed"):
        reconcile(controller, "owner", task, changed)
    assert controller.store.get("result", task) is None
    assert controller.store.ownership(resource)[2] == "uncertain"


def test_cancelled_settlement_rolls_back_after_audit_conflict(setup):
    controller, repo = setup
    task, state, resource = _cancelled(controller, repo, "audit-rollback")
    controller.store.put_once("reconciliation", task + ":g1", {"event": "different"})
    with pytest.raises(ValueError, match="conflicting cancellation reconciliation"):
        reconcile(controller, "owner", task, lambda *_: _terminal(state))
    assert controller.store.get("result", task) is None
    assert controller.store.get("state", task) == state
    assert controller.store.ownership(resource)[2] == "uncertain"
    assert not (controller.artifacts / task / "cancellation-reconciliation.json").exists()


def test_cancelled_local_collector_refuses_mismatched_artifact(setup):
    controller, repo = setup
    task, state, resource = _cancelled(controller, repo, "artifact-binding")
    directory = controller.artifacts / task
    (directory / "adapter-result.json").write_text(json.dumps({"task": task, "attempt": "wrong",
                                                                 "status": "completed"}))
    with pytest.raises(PermissionError, match="artifacts do not bind"):
        reconcile_local(controller, "owner", task)
    assert controller.store.ownership(resource)[2] == "uncertain"


def test_cancelled_replay_repairs_missing_receipt_file(setup):
    controller, repo = setup
    task, _state, _resource = _cancelled(controller, repo, "receipt-repair")
    result = reconcile_local(controller, "owner", task)
    receipt = controller.artifacts / task / "cancellation-reconciliation.json"
    receipt.unlink()
    assert reconcile_local(controller, "owner", task) == result
    assert json.loads(receipt.read_text()) == result


def test_local_collector_refuses_live_recorded_process_group(setup, monkeypatch):
    controller, repo = setup
    task, state, _resource = _cancelled(controller, repo, "live-group")
    monkeypatch.setattr(reconciliation_api.os, "kill", lambda *_: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(reconciliation_api.os, "killpg", lambda *_: None)
    with pytest.raises(PermissionError, match="process group remains live"):
        controller_only_observation(controller, task, state)


def test_local_collector_rechecks_group_when_pid_exits_during_probe(setup, monkeypatch):
    controller, repo = setup
    task, state, _resource = _cancelled(controller, repo, "pid-race")
    monkeypatch.setattr(reconciliation_api.os, "kill", lambda *_: None)
    monkeypatch.setattr(reconciliation_api.os, "getpgid",
                        lambda *_: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(reconciliation_api.os, "killpg", lambda *_: None)
    with pytest.raises(PermissionError, match="process group remains live"):
        controller_only_observation(controller, task, state)


def _github_reader(task, state, *, head="head", base="base", actor="fixture-bridge"):
    return {"kind": "github-pr-readback-v1", "task": task, "attempt": state["attempt"],
            "generation": str(state["generation"]), "epoch": str(state["epoch"]),
            "repo": "fixture/repo", "pr": "1", "head": head, "base": base,
            "actor": actor, "token_env": "FIXTURE_GITHUB_TOKEN"}


def _install_fake_github(monkeypatch, *, actor="fixture-bridge", head="head", base="base", reviews=None):
    calls = []

    def fake_request(method, path, *, headers, json):
        calls.append((method, path, headers))
        if path == "/user":
            return {"login": actor}
        if path == "/repos/fixture/repo/pulls/1":
            return {"state": "open", "head": {"sha": head}, "base": {"sha": base}}
        if path.endswith("reviews?per_page=100&page=1"):
            return (reviews or [])[:100]
        if path.endswith("reviews?per_page=100&page=2"):
            return (reviews or [])[100:]
        raise AssertionError(path)

    monkeypatch.setattr(reconciliation_api, "GitHubAdvisoryTransport",
                        lambda **kwargs: NativeGitHubTransport(http_client=fake_request, **kwargs))
    return calls


def test_github_reader_authenticates_and_reads_all_review_pages(setup, monkeypatch):
    controller, repo = setup
    task, state, _resource = _cancelled(controller, repo, "github-pages")
    controller.hosts["fixture"]["reconciliation_delivery_reader"] = _github_reader(task, state)
    monkeypatch.setenv("FIXTURE_GITHUB_TOKEN", "fixture-token")
    reviews = [{"id": index, "commit_id": "other", "user": {"login": "other"}}
               for index in range(1, 101)]
    calls = _install_fake_github(monkeypatch, reviews=reviews)
    observation = controller_only_observation(controller, task, state)
    assert observation["delivery"]["authenticated"] is True
    assert observation["delivery"]["review_count"] == 100
    assert any(path.endswith("page=2") for _method, path, _headers in calls)
    assert all(headers["Authorization"] == "Bearer fixture-token" for _method, _path, headers in calls)


def test_github_reader_refuses_actor_drift_head_drift_and_matching_review(setup, monkeypatch):
    controller, repo = setup
    task, state, _resource = _cancelled(controller, repo, "github-refusal")
    controller.hosts["fixture"]["reconciliation_delivery_reader"] = _github_reader(task, state)
    monkeypatch.setenv("FIXTURE_GITHUB_TOKEN", "fixture-token")
    _install_fake_github(monkeypatch, actor="different")
    with pytest.raises(PermissionError, match="not in authorized bridge actors"):
        controller_only_observation(controller, task, state)
    _install_fake_github(monkeypatch, head="different")
    with pytest.raises(PermissionError, match="candidate changed"):
        controller_only_observation(controller, task, state)
    matching = [{"id": 101, "commit_id": "head", "user": {"login": "fixture-bridge"}}]
    _install_fake_github(monkeypatch, reviews=matching)
    with pytest.raises(PermissionError, match="matching remote review"):
        controller_only_observation(controller, task, state)


def test_client_reconcile_has_no_observation_parameter(monkeypatch):
    client = Client({"python": "python", "source": ".", "controller_config": "fixture"})
    seen = {}
    monkeypatch.setattr(client, "call", lambda action, **payload: seen.update(action=action, **payload) or {"ok": True})
    assert client.reconcile("task") == {"ok": True}
    assert seen == {"action": "reconcile", "task_id": "task"}


def test_new_local_worker_persists_launch_identity(setup):
    controller, repo = setup
    task = controller.submit("owner", "launch-identity", spec(repo))
    result = controller.run("owner", task, execution_host="fixture")
    identity = result["state"]["worker_identity"]
    assert identity["pid"] == result["state"]["pid"]
    assert identity["pgid"] == result["state"]["pgid"]
    assert identity["observed_at_ns"] > 0
    assert identity["host"]
    assert identity["os_started"]
    assert identity["boot_identity"]
    assert identity["executable"]
    assert len(identity["executable_sha256"]) == 64


def test_workspace_preflight_requires_git_or_controller_snapshot_provenance(setup):
    controller, repo = setup
    import shutil
    shutil.rmtree(repo / ".git")
    with pytest.raises(PermissionError, match="requires Git metadata"):
        controller.run("owner", controller.submit("owner", "no-git", spec(repo)), execution_host="fixture")
    snapshot = {**spec(repo), "workspace_kind": "immutable_snapshot",
                "snapshot_provenance": {"repo": "fixture/repo", "head": "a" * 40,
                                        "base": "b" * 40, "export_id": "export-1",
                                        "export_digest": digest({"result.json": {"digest": None, "mode": None}})}}
    invalid = {**snapshot, "snapshot_provenance": {**snapshot["snapshot_provenance"],
                                                      "head": "a" * 41}}
    with pytest.raises(PermissionError, match="full Git SHAs"):
        controller.run("owner", controller.submit("owner", "bad-snapshot-sha", invalid), execution_host="fixture")
    invalid = {**snapshot, "snapshot_provenance": {**snapshot["snapshot_provenance"],
                                                      "export_digest": "0" * 64}}
    with pytest.raises(PermissionError, match="does not bind"):
        controller.run("owner", controller.submit("owner", "bad-snapshot-digest", invalid), execution_host="fixture")
    task = controller.submit("owner", "snapshot", snapshot)
    result = controller.run("owner", task, execution_host="fixture")
    provenance = result["state"]["workspace_provenance"]
    assert result["result"]["accepted"]
    assert provenance["kind"] == "immutable_snapshot"
    assert provenance["head"] == "a" * 40
    assert provenance["digest"]
    context = json.loads((controller.artifacts / task / "context.json").read_text())
    assert context["workspace_provenance"] == provenance
