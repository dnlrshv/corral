"""Typed PR repair admission and exact-head branch publication."""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from corral.execution import continuation
from corral.execution import github_branch
from corral.execution import service_command
from corral.execution.github_branch import BranchPublisher
from corral.execution.inspection_report import validate
from corral.execution.internal_review import record as record_review
from corral.execution.service import Service
from corral.execution.store import digest
from corral.execution.workspace import manifest


def git(*args, cwd: Path | None = None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True,
                          check=True).stdout.strip()


def commit_dates(git_dir: Path, commit: str) -> tuple[int, int]:
    author, committer = git("--git-dir", str(git_dir), "show", "-s", "--format=%at %ct",
                            commit).split()
    return int(author), int(committer)


def git_fixture(tmp_path: Path) -> tuple[Path, Path, str, str]:
    remote, workspace = tmp_path / "remote.git", tmp_path / "repair"
    git("init", "--bare", "-q", str(remote))
    git("init", "-q", str(workspace))
    git("config", "user.name", "Fixture", cwd=workspace)
    git("config", "user.email", "fixture@example.invalid", cwd=workspace)
    (workspace / "change.py").write_text("VALUE = 0\n")
    git("add", "change.py", cwd=workspace)
    git("commit", "-qm", "base", cwd=workspace)
    base = git("rev-parse", "HEAD", cwd=workspace)
    git("branch", "-M", "main", cwd=workspace)
    git("checkout", "-qb", "repair-7", cwd=workspace)
    (workspace / "change.py").write_text("VALUE = 1\n")
    git("commit", "-qam", "candidate", cwd=workspace)
    head = git("rev-parse", "HEAD", cwd=workspace)
    git("remote", "add", "origin", str(remote), cwd=workspace)
    git("push", "-q", "origin", "main", "repair-7", cwd=workspace)
    return workspace, remote, base, head


def fake_gh(tmp_path: Path) -> Path:
    executable = tmp_path / "gh-fixture"
    executable.write_text("""#!/usr/bin/env python3
import json, os, subprocess, sys
fixture = json.loads(open(os.environ['CORRAL_REPAIR_GH_FIXTURE']).read())
path = sys.argv[-1]
if path == 'user':
    print(json.dumps({'login': fixture['actor'], 'id': fixture['actor_id']}))
else:
    head = subprocess.check_output(['git', '--git-dir', fixture['remote'], 'rev-parse',
                                    'refs/heads/' + fixture['branch']], text=True).strip()
    print(json.dumps({'state': 'open', 'head': {'sha': head, 'ref': fixture['branch'],
          'repo': {'full_name': 'fixture/repo'}},
          'base': {'sha': fixture['base'], 'ref': 'main'}}))
""")
    executable.chmod(0o700)
    return executable


def service_fixture(tmp_path: Path, workspace: Path, remote: Path, gh: Path) -> Service:
    verifier = tmp_path / "verify.py"
    verifier.write_text("raise SystemExit(0)\n")
    controller = tmp_path / "controller.json"
    controller.write_text(json.dumps({
        "state": str(tmp_path / "state"), "token": "owner", "default_host": "fixture",
        "hosts": {"fixture": {"routes": ["native-codex-alibaba"], "harnesses": [],
                               "cpu": 2, "memory_mb": 1024}},
    }))
    repair_policy = {
        "owner": "corral", "owner_epoch": 1,
        "profile_id": "qwen3.8-max-high-codex-alibaba",
        "objective_template": "Repair {repository} PR {pr} from this review:\n{review_report}",
        "candidate_paths": ["change.py"], "verifier_paths": [],
        "verify": [sys.executable, str(verifier), "change.py"], "branch": "repair-7",
        "allowed_prs": [7], "allowed_hosts": ["fixture"],
        "commit_identity": {"name": "Corral Repair", "email": "corral@example.invalid"},
        "publisher_actor": "fixture-publisher", "publisher_actor_id": 101,
        "publisher_account_ref": "fixture-account",
    }
    config = tmp_path / "service.json"
    config.write_text(json.dumps({
        "controller_config": str(controller), "repositories": {"demo": {
            "enabled": True, "github_repository": "fixture/repo", "remote_url": str(remote),
            "development_file_remote": True,
            "github": {"executable": str(gh), "auth_mode": "fixture-user"},
            "default_host": "fixture", "allowed_hosts": ["fixture"],
            "workspaces": {"fixture": str(workspace)}, "repair_policies": {
                "advisory": repair_policy}}}}))
    return Service(config)


def trusted_review(service: Service, tmp_path: Path, head: str, base: str,
                   verdict: str = "CHANGES_REQUIRED") -> dict:
    store, task, attempt = service.store, "review-task-" + verdict, "review-attempt-" + verdict
    selected = {"change.py": {"digest": "d" * 64, "mode": 0o444}}
    export = {"repository": "fixture/repo", "pr_number": 7, "head": head, "base": base,
              "policy_id": "advisory", "policy_digest": "c" * 64,
              "workspace": str(tmp_path / "review-snapshot"), "selected_files": selected,
              "selected_files_digest": digest(selected), "diff_path": "change.py",
              "diff_sha256": "d" * 64, "export_digest": digest(selected),
              "auth_mode": "fixture"}
    Path(export["workspace"]).mkdir(exist_ok=True)
    export = {"export_id": digest(export), **export}
    store.put_once("trusted_export", export["export_id"], export)
    profile = {"id": "inspection", "provider": "fixture", "account_ref": "fixture",
               "route": "packet", "model": "review-model", "effort": "medium",
               "harness": "corral-inspection-packet"}
    store.put_once("request", task, {"role": "review", "trusted_export_id": export["export_id"],
                                      "selection": {"profile": profile}})
    store.put_once("state", task, {"status": "completed", "attempt": attempt, "generation": 1})
    store.put_once("invocation", attempt, {"task": task, "generation": 1})
    provenance = {"repo": "fixture/repo", "pr": 7, "head": head, "base": base,
                  "export_id": export["export_id"], "trusted_export_id": export["export_id"],
                  "policy_id": "advisory", "policy_digest": "c" * 64}
    provenance["digest"] = digest(provenance)
    documents = [{"path": "change.py", "kind": "source", "sha256": "d" * 64, "bytes": 1}]
    provenance["documents_digest"] = digest(documents)
    capability = {"mode": "stateless-inspection-only", "tools_supplied": [],
                  "session_reuse": False, "denied": ["candidate-code-execution", "shell",
                  "tests", "imports", "build-hooks", "package-commands"]}
    structured = {"verdict": verdict, "report": "Change VALUE safely.",
                  "packet_digest": "f" * 64, "provenance": provenance,
                  "capability": capability}
    packet = {"task": task, "attempt": attempt, "generation": 1,
              "digest": "f" * 64, "provenance": provenance,
              "capability": capability, "documents": documents}
    adapter = {"schema": "corral-adapter-result-v1", "status": "completed", "task": task,
               "attempt": attempt, "structured": structured}
    validation = validate(adapter, packet, task_id=task, attempt=attempt, generation=1,
                          trusted_export_id=export["export_id"])
    store.put_once("inspection_validation", f"{task}:{attempt}:g1", validation)
    continuation.record_result(store, task, 1, {
        "accepted": True, "generation": 1, "selection": {"profile": profile},
        "observed": {"model": "review-model", "harness": "inspection-packet-http"},
        "structured": structured, "inspection_validation": validation})
    return record_review(store, task)


def stub_submission(service: Service, monkeypatch) -> None:
    def submit(_token, request_id, spec):
        task = digest({"request": request_id})
        service.store.put_once("request", task, spec)
        service.store.put_once("initial", task, {"status": "submitted"})
        return task
    monkeypatch.setattr(service.controller, "submit", submit)


def test_repair_admission_is_policy_typed_and_review_bound(tmp_path, monkeypatch):
    workspace, remote, base, head = git_fixture(tmp_path)
    gh = fake_gh(tmp_path)
    service = service_fixture(tmp_path, workspace, remote, gh)
    epoch = service.store.acquire("pr:fixture/repo#7", "corral")
    assert epoch == 1
    receipt = trusted_review(service, tmp_path, head, base)
    stub_submission(service, monkeypatch)

    admitted = service.submit_pr_repair(
        "demo", 7, receipt["receipt_id"], expected_head=head, expected_base=base)
    event, task = admitted["event"], admitted["event"]["task_id"]
    request = service.store.get("request", task)
    assert event["role"] == "repair" and event["pr_number"] == 7
    assert request["service_event_id"] == event["event_id"]
    assert request["objective"] == "Repair fixture/repo PR 7 from this review:\nChange VALUE safely."
    assert request["candidate_paths"] == ["change.py"]
    assert request["verify"][-1] == "change.py"
    assert (request["pr_owner"], request["pr_owner_epoch"]) == ("corral", 1)

    passed = trusted_review(service, tmp_path, head, base, verdict="PASS")
    with pytest.raises(PermissionError, match="does not bind"):
        service.submit_pr_repair(
            "demo", 7, passed["receipt_id"], expected_head=head, expected_base=base)

    monkeypatch.setattr(service_command, "Service", lambda _config: service)
    assert service_command.main([
        "--config", "controller-owned.json", "repair-pr", "--repository", "demo",
        "--pr", "7", "--review-receipt", receipt["receipt_id"],
        "--expected-head", head, "--expected-base", base]) == 0


def accepted_repair(service: Service, event: dict, workspace: Path) -> dict:
    task, attempt = event["task_id"], "repair-attempt"
    request = service.store.get("request", task)
    (workspace / "change.py").write_text("VALUE = 2\n")
    candidate = manifest(workspace, request["candidate_paths"])
    artifact = service.controller.artifacts / task
    artifact.mkdir(parents=True, exist_ok=True)
    (artifact / "candidate_manifest.json").write_text(json.dumps(candidate))
    service.store.replace("state", task, {"status": "completed", "attempt": attempt,
                                           "generation": 1})
    service.store.put_once("invocation", attempt, {"task": task, "generation": 1})
    continuation.record_result(service.store, task, 1, {
        "accepted": True, "generation": 1, "artifact_directory": str(artifact),
        "amendment_pending": False, "receipt": {"candidate_post": candidate["digest"]}})
    return candidate


def test_branch_publication_pushes_exact_accepted_commit_once(tmp_path, monkeypatch):
    workspace, remote, base, head = git_fixture(tmp_path)
    gh = fake_gh(tmp_path)
    fixture = tmp_path / "gh.json"
    fixture.write_text(json.dumps({"remote": str(remote), "branch": "repair-7", "base": base,
                                   "actor": "fixture-publisher", "actor_id": 101}))
    monkeypatch.setenv("CORRAL_REPAIR_GH_FIXTURE", str(fixture))
    service = service_fixture(tmp_path, workspace, remote, gh)
    service.store.acquire("pr:fixture/repo#7", "corral")
    receipt = trusted_review(service, tmp_path, head, base)
    stub_submission(service, monkeypatch)
    event = service.submit_pr_repair(
        "demo", 7, receipt["receipt_id"], expected_head=head, expected_base=base)["event"]
    candidate = accepted_repair(service, event, workspace)

    before = int(time.time())
    published = service.publish_pr_repair("demo", event["event_id"])
    after = int(time.time())
    assert published["published"] is True and published["old_head"] == head
    assert published["publisher"] == {"login": "fixture-publisher", "id": 101,
                                       "account_ref": "fixture-account",
                                       "auth_mode": "fixture-user"}
    remote_head = git("--git-dir", str(remote), "rev-parse", "refs/heads/repair-7")
    assert remote_head == published["new_head"]
    assert git("show", f"{remote_head}:change.py", cwd=workspace) == "VALUE = 2"
    assert service.publish_pr_repair("demo", event["event_id"]) == published
    assert len(service.store.records("repair_publish_intent")) == 1
    commit = service.store.get("repair_commit", event["task_id"] + ":g1")
    assert commit["candidate_manifest"] == candidate["digest"]
    # The commit carries the real admission time, persisted once for this operation.
    seed = service.store.get("repair_commit_seed", event["task_id"] + ":g1")
    assert before <= seed["admitted_at"] <= after
    assert commit["admitted_at"] == seed["admitted_at"]
    assert commit_dates(remote, remote_head) == (seed["admitted_at"], seed["admitted_at"])


def test_branch_publication_refuses_unaccepted_or_out_of_policy_bytes(tmp_path, monkeypatch):
    workspace, remote, base, head = git_fixture(tmp_path)
    gh = fake_gh(tmp_path)
    fixture = tmp_path / "gh.json"
    fixture.write_text(json.dumps({"remote": str(remote), "branch": "repair-7", "base": base,
                                   "actor": "fixture-publisher", "actor_id": 101}))
    monkeypatch.setenv("CORRAL_REPAIR_GH_FIXTURE", str(fixture))
    service = service_fixture(tmp_path, workspace, remote, gh)
    service.store.acquire("pr:fixture/repo#7", "corral")
    receipt = trusted_review(service, tmp_path, head, base)
    stub_submission(service, monkeypatch)
    event = service.submit_pr_repair(
        "demo", 7, receipt["receipt_id"], expected_head=head, expected_base=base)["event"]
    with pytest.raises(PermissionError, match="accepted terminal"):
        service.publish_pr_repair("demo", event["event_id"])
    accepted_repair(service, event, workspace)
    (workspace / "outside.py").write_text("UNAUTHORIZED = True\n")
    with pytest.raises(PermissionError, match="outside the registered candidate"):
        service.publish_pr_repair("demo", event["event_id"])
    assert service.store.records("repair_publish_intent") == {}


def test_lost_push_ack_reads_remote_before_same_intent_retry(tmp_path, monkeypatch):
    workspace, remote, base, head = git_fixture(tmp_path)
    gh = fake_gh(tmp_path)
    fixture = tmp_path / "gh.json"
    fixture.write_text(json.dumps({"remote": str(remote), "branch": "repair-7", "base": base,
                                   "actor": "fixture-publisher", "actor_id": 101}))
    monkeypatch.setenv("CORRAL_REPAIR_GH_FIXTURE", str(fixture))
    service = service_fixture(tmp_path, workspace, remote, gh)
    service.store.acquire("pr:fixture/repo#7", "corral")
    receipt = trusted_review(service, tmp_path, head, base)
    stub_submission(service, monkeypatch)
    event = service.submit_pr_repair(
        "demo", 7, receipt["receipt_id"], expected_head=head, expected_base=base)["event"]
    accepted_repair(service, event, workspace)
    original = BranchPublisher._git

    def lose_before_push(self, root, *args, **kwargs):
        if args and args[0] == "push":
            return subprocess.CompletedProcess(args, 1, stdout=b"", stderr=b"lost")
        return original(self, root, *args, **kwargs)

    monkeypatch.setattr(BranchPublisher, "_git", lose_before_push)
    with pytest.raises(PermissionError, match="not confirmed"):
        service.publish_pr_repair("demo", event["event_id"])
    intent = next(iter(service.store.records("repair_publish_intent")))
    assert service.reconcile_pr_repair("demo", intent)["status"] == "not-delivered"
    assert git("--git-dir", str(remote), "rev-parse", "refs/heads/repair-7") == head

    monkeypatch.setattr(BranchPublisher, "_git", original)
    published = service.publish_pr_repair("demo", event["event_id"])
    assert published["published"] is True
    assert published["intent"] == intent


def test_retry_after_lost_commit_record_reuses_persisted_admission_time(tmp_path, monkeypatch):
    workspace, remote, base, head = git_fixture(tmp_path)
    gh = fake_gh(tmp_path)
    fixture = tmp_path / "gh.json"
    fixture.write_text(json.dumps({"remote": str(remote), "branch": "repair-7", "base": base,
                                   "actor": "fixture-publisher", "actor_id": 101}))
    monkeypatch.setenv("CORRAL_REPAIR_GH_FIXTURE", str(fixture))
    service = service_fixture(tmp_path, workspace, remote, gh)
    service.store.acquire("pr:fixture/repo#7", "corral")
    receipt = trusted_review(service, tmp_path, head, base)
    stub_submission(service, monkeypatch)
    event = service.submit_pr_repair(
        "demo", 7, receipt["receipt_id"], expected_head=head, expected_base=base)["event"]
    accepted_repair(service, event, workspace)
    key = event["task_id"] + ":g1"
    first, retry = 1_790_000_000, 1_790_003_600
    created = []
    original_git, original_put = BranchPublisher._git, service.store.put_once

    def observe_commit(self, root, *args, **kwargs):
        value = original_git(self, root, *args, **kwargs)
        if args and args[0] == "commit-tree":
            created.append(value.stdout.decode().strip())
        return value

    def crash_before_commit_record(kind, record_key, value):
        if kind == "repair_commit":
            raise RuntimeError("crash after the commit object, before its record")
        return original_put(kind, record_key, value)

    monkeypatch.setattr(BranchPublisher, "_git", observe_commit)
    monkeypatch.setattr(github_branch, "time", SimpleNamespace(time=lambda: first))
    monkeypatch.setattr(service.store, "put_once", crash_before_commit_record)
    with pytest.raises(RuntimeError, match="crash after the commit object"):
        service.publish_pr_repair("demo", event["event_id"])
    assert service.store.get("repair_commit_seed", key)["admitted_at"] == first
    assert service.store.get("repair_commit", key) is None
    assert service.store.records("repair_publish_intent") == {}

    # The retry runs an hour later; it must reuse the persisted admission time, not its own.
    monkeypatch.setattr(service.store, "put_once", original_put)
    monkeypatch.setattr(github_branch, "time", SimpleNamespace(time=lambda: retry))
    published = service.publish_pr_repair("demo", event["event_id"])
    assert len(created) == 2 and created[0] == created[1] == published["new_head"]
    assert service.store.get("repair_commit_seed", key)["admitted_at"] == first
    assert service.store.get("repair_commit", key)["admitted_at"] == first
    remote_head = git("--git-dir", str(remote), "rev-parse", "refs/heads/repair-7")
    assert remote_head == published["new_head"]
    assert commit_dates(remote, remote_head) == (first, first)


def test_owned_once_persists_first_value_under_the_owner_fence(tmp_path):
    from corral.execution.store import Store

    store = Store(tmp_path / "state" / "controller.sqlite")
    epoch = store.acquire("pr:fixture/repo#7", "corral")
    identity = {"task": "t", "generation": 1}
    stored = store.owned_once("pr:fixture/repo#7", "corral", epoch, "seed", "t:g1",
                              identity, {"admitted_at": 10})
    assert stored == {"task": "t", "generation": 1, "admitted_at": 10}
    assert store.owned_once("pr:fixture/repo#7", "corral", epoch, "seed", "t:g1",
                            identity, {"admitted_at": 20}) == stored
    with pytest.raises(ValueError, match="conflicting operation identity"):
        store.owned_once("pr:fixture/repo#7", "corral", epoch, "seed", "t:g1",
                         {"task": "t", "generation": 2}, {"admitted_at": 30})
    with pytest.raises(PermissionError, match="stale operation fence"):
        store.owned_once("pr:fixture/repo#7", "corral", epoch + 1, "seed", "t:g2",
                         identity, {"admitted_at": 40})
    assert store.get("seed", "t:g2") is None


def test_timed_out_push_is_resolved_by_remote_readback(tmp_path, monkeypatch):
    workspace, remote, base, head = git_fixture(tmp_path)
    gh = fake_gh(tmp_path)
    fixture = tmp_path / "gh.json"
    fixture.write_text(json.dumps({"remote": str(remote), "branch": "repair-7", "base": base,
                                   "actor": "fixture-publisher", "actor_id": 101}))
    monkeypatch.setenv("CORRAL_REPAIR_GH_FIXTURE", str(fixture))
    service = service_fixture(tmp_path, workspace, remote, gh)
    service.store.acquire("pr:fixture/repo#7", "corral")
    receipt = trusted_review(service, tmp_path, head, base)
    stub_submission(service, monkeypatch)
    event = service.submit_pr_repair(
        "demo", 7, receipt["receipt_id"], expected_head=head, expected_base=base)["event"]
    accepted_repair(service, event, workspace)
    original = BranchPublisher._git
    bounds = []

    def push_lands_then_times_out(self, root, *args, **kwargs):
        if args and args[0] == "push":
            bounds.append(kwargs.get("timeout"))
            original(self, root, *args, **kwargs)
            raise subprocess.TimeoutExpired(["git", "push"], kwargs.get("timeout"))
        return original(self, root, *args, **kwargs)

    monkeypatch.setattr(BranchPublisher, "_git", push_lands_then_times_out)
    published = service.publish_pr_repair("demo", event["event_id"])
    assert bounds == [github_branch.PUSH_TIMEOUT_SECONDS]
    assert published["published"] is True
    assert git("--git-dir", str(remote), "rev-parse", "refs/heads/repair-7") == \
        published["new_head"]
