"""Typed PR repair admission and exact-head branch publication."""
from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
import time
import traceback
from pathlib import Path
from types import SimpleNamespace

import pytest

from corral.execution import continuation
from corral.execution import github_branch
from corral.execution import repair_objects
from corral.execution import service_command
from corral.execution.github_branch import BranchPublisher
from corral.execution.inspection_report import validate
from corral.execution.internal_review import record as record_review
from corral.execution.repair_objects import RepairObjects
from corral.execution.service import Service
from corral.execution.store import digest
from corral.execution.workspace import manifest


def git(*args, cwd: Path | None = None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True,
                          check=True, stdin=subprocess.DEVNULL).stdout.strip()


def commit_dates(git_dir: Path, commit: str) -> tuple[int, int]:
    author, committer = git("--git-dir", str(git_dir), "show", "-s", "--format=%at %ct",
                            commit).split()
    return int(author), int(committer)


def remote_files(remote: Path, commit: str) -> dict[str, bytes]:
    names = git("--git-dir", str(remote), "ls-tree", "-r", "--name-only", commit).splitlines()
    return {name: subprocess.run(["git", "--git-dir", str(remote), "cat-file", "blob",
                                  f"{commit}:{name}"], capture_output=True, check=True).stdout
            for name in names}


def remote_head(remote: Path) -> str:
    return git("--git-dir", str(remote), "rev-parse", "refs/heads/repair-7")


def git_fixture(tmp_path: Path, extra: dict[str, str] | None = None
                ) -> tuple[Path, Path, str, str]:
    remote, workspace = tmp_path / "remote.git", tmp_path / "repair"
    git("init", "--bare", "-q", str(remote))
    git("init", "-q", str(workspace))
    git("config", "user.name", "Fixture", cwd=workspace)
    git("config", "user.email", "fixture@example.invalid", cwd=workspace)
    (workspace / "change.py").write_text("VALUE = 0\n")
    for name, text in (extra or {}).items():
        (workspace / name).parent.mkdir(parents=True, exist_ok=True)
        (workspace / name).write_text(text)
    git("add", "-A", cwd=workspace)
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


def fake_credential_helper(tmp_path: Path) -> Path:
    """A publisher credential helper that answers every request with a fixture credential."""
    helper = tmp_path / "credential-fixture"
    helper.write_text("#!/bin/sh\nprintf 'username=fixture\\npassword=fixture\\n'\n")
    helper.chmod(0o700)
    return helper


def service_fixture(tmp_path: Path, workspace: Path, remote: Path, gh: Path, *,
                    candidate_paths: list[str] | None = None,
                    policy: dict | None = None, repository: dict | None = None) -> Service:
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
        "candidate_paths": candidate_paths or ["change.py"], "verifier_paths": [],
        "verify": [sys.executable, str(verifier), "change.py"], "branch": "repair-7",
        "allowed_prs": [7], "allowed_hosts": ["fixture"],
        "commit_identity": {"name": "Corral Repair", "email": "corral@example.invalid"},
        "publisher_actor": "fixture-publisher", "publisher_actor_id": 101,
        "publisher_account_ref": "fixture-account", **(policy or {}),
    }
    config = tmp_path / "service.json"
    config.write_text(json.dumps({
        "controller_config": str(controller), "repositories": {"demo": {
            "enabled": True, "github_repository": "fixture/repo", "remote_url": str(remote),
            "development_file_remote": True,
            "github": {"executable": str(gh), "auth_mode": "fixture-user"},
            "default_host": "fixture", "allowed_hosts": ["fixture"],
            "workspaces": {"fixture": str(workspace)}, "repair_policies": {
                "advisory": repair_policy}, **(repository or {})}}}))
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


def admitted(tmp_path: Path, monkeypatch, *, extra: dict[str, str] | None = None,
             candidate_paths: list[str] | None = None,
             repository: dict | None = None) -> SimpleNamespace:
    """A repair admitted for fixture PR 7, with the fake GitHub reader wired to the remote."""
    workspace, remote, base, head = git_fixture(tmp_path, extra)
    gh = fake_gh(tmp_path)
    fixture = tmp_path / "gh.json"
    fixture.write_text(json.dumps({"remote": str(remote), "branch": "repair-7", "base": base,
                                   "actor": "fixture-publisher", "actor_id": 101}))
    monkeypatch.setenv("CORRAL_REPAIR_GH_FIXTURE", str(fixture))
    service = service_fixture(tmp_path, workspace, remote, gh, candidate_paths=candidate_paths,
                              repository=repository)
    service.store.acquire("pr:fixture/repo#7", "corral")
    receipt = trusted_review(service, tmp_path, head, base)
    stub_submission(service, monkeypatch)
    event = service.submit_pr_repair(
        "demo", 7, receipt["receipt_id"], expected_head=head, expected_base=base)["event"]
    return SimpleNamespace(workspace=workspace, remote=remote, base=base, head=head,
                           service=service, event=event)


def accepted_repair(service: Service, event: dict, workspace: Path) -> dict:
    """Record the controller's acceptance of the checkout's current candidate bytes."""
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


def trap(tmp_path: Path, name: str, *, passthrough: bool) -> tuple[Path, Path]:
    """An executable that records each run (and the Git config it inherited) in a marker."""
    marker, script = tmp_path / (name + ".ran"), tmp_path / name
    body = f'printf "%s\\n" "ran ${{GIT_CONFIG_PARAMETERS:-}}" >> "{marker}"\n'
    script.write_text("#!/bin/sh\n" + body + ("exec cat\n" if passthrough else "exit 0\n"))
    script.chmod(0o700)
    return script, marker


def install_filter_trap(tmp_path: Path, workspace: Path) -> Path:
    """A clean filter reached only through ``include.path`` and ``.git/info/attributes``."""
    script, marker = trap(tmp_path, "filter-trap", passthrough=True)
    included = tmp_path / "included.gitconfig"
    included.write_text(f'[filter "evil"]\n\tclean = {script}\n\tsmudge = {script}\n')
    git("config", "include.path", str(included), cwd=workspace)
    info = workspace / ".git" / "info"
    info.mkdir(exist_ok=True)
    (info / "attributes").write_text("* filter=evil\n")
    return marker


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
    # What will be published, and by whom, is frozen at admission.
    assert request["repair_publication"] == {
        "branch": "repair-7", "allowed_prs": [7],
        "commit_identity": {"name": "Corral Repair", "email": "corral@example.invalid"},
        "publisher_actor": "fixture-publisher", "publisher_actor_id": 101,
        "publisher_account_ref": "fixture-account"}

    passed = trusted_review(service, tmp_path, head, base, verdict="PASS")
    with pytest.raises(PermissionError, match="does not bind"):
        service.submit_pr_repair(
            "demo", 7, passed["receipt_id"], expected_head=head, expected_base=base)

    monkeypatch.setattr(service_command, "Service", lambda _config: service)
    assert service_command.main([
        "--config", "controller-owned.json", "repair-pr", "--repository", "demo",
        "--pr", "7", "--review-receipt", receipt["receipt_id"],
        "--expected-head", head, "--expected-base", base]) == 0


@pytest.mark.parametrize(("repository", "policy", "message"), [
    ({"allowed_roles": ["implementation", "review"]}, {}, "role is not allowed"),
    ({}, {"verifier_paths": "verify.py"}, "verifier_paths must be a list"),
    ({}, {"allowed_hosts": "fixture"}, "allowed_hosts must be a list"),
])
def test_repair_admission_refuses_disallowed_role_and_untyped_policy_lists(
        tmp_path, monkeypatch, repository, policy, message):
    workspace, remote, base, head = git_fixture(tmp_path)
    service = service_fixture(tmp_path, workspace, remote, fake_gh(tmp_path),
                              policy=policy, repository=repository)
    service.store.acquire("pr:fixture/repo#7", "corral")
    receipt = trusted_review(service, tmp_path, head, base)
    stub_submission(service, monkeypatch)
    with pytest.raises(PermissionError, match=message):
        service.submit_pr_repair(
            "demo", 7, receipt["receipt_id"], expected_head=head, expected_base=base)
    assert service.store.records("service_event") == {}


def test_admission_never_runs_the_checkout_git_configuration(tmp_path, monkeypatch):
    workspace, remote, base, head = git_fixture(tmp_path)
    service = service_fixture(tmp_path, workspace, remote, fake_gh(tmp_path))
    service.store.acquire("pr:fixture/repo#7", "corral")
    receipt = trusted_review(service, tmp_path, head, base)
    stub_submission(service, monkeypatch)
    # An earlier worker left executable Git settings in the reused checkout.
    monitor, monitor_marker = trap(tmp_path, "fsmonitor-trap", passthrough=False)
    git("config", "core.fsmonitor", str(monitor), cwd=workspace)
    filter_marker = install_filter_trap(tmp_path, workspace)
    stamp = time.time() + 5
    os.utime(workspace / "change.py", (stamp, stamp))

    event = service.submit_pr_repair(
        "demo", 7, receipt["receipt_id"], expected_head=head, expected_base=base)["event"]
    assert event["role"] == "repair" and event["task_id"]
    assert not monitor_marker.exists() and not filter_marker.exists()

    # Control: an ordinary status in the checkout does run the planted programs.
    git("status", "--porcelain", cwd=workspace)
    assert monitor_marker.exists() or filter_marker.exists()


def test_admission_judges_cleanliness_from_trusted_objects(tmp_path, monkeypatch):
    workspace, remote, base, head = git_fixture(tmp_path)
    service = service_fixture(tmp_path, workspace, remote, fake_gh(tmp_path))
    service.store.acquire("pr:fixture/repo#7", "corral")
    receipt = trusted_review(service, tmp_path, head, base)
    stub_submission(service, monkeypatch)

    def admit():
        return service.submit_pr_repair(
            "demo", 7, receipt["receipt_id"], expected_head=head, expected_base=base)

    # The checkout's own index hides a modified tracked file from an ordinary status.
    git("update-index", "--assume-unchanged", "change.py", cwd=workspace)
    (workspace / "change.py").write_text("VALUE = 9\n")
    assert git("status", "--porcelain", cwd=workspace) == ""
    with pytest.raises(PermissionError, match="must be clean"):
        admit()
    (workspace / "change.py").write_text("VALUE = 1\n")
    git("update-index", "--no-assume-unchanged", "change.py", cwd=workspace)

    # The checkout's own exclude file hides an untracked file from an ordinary status.
    (workspace / ".git" / "info").mkdir(exist_ok=True)
    (workspace / ".git" / "info" / "exclude").write_text("planted.py\n")
    (workspace / "planted.py").write_text("PLANTED = True\n")
    assert git("status", "--porcelain", cwd=workspace) == ""
    with pytest.raises(PermissionError, match="must be clean"):
        admit()
    (workspace / "planted.py").unlink()

    # The remote branch moved on from the reviewed head.
    moved = git("commit-tree", git("rev-parse", "HEAD^{tree}", cwd=workspace), "-p", head,
                "-m", "moved", cwd=workspace)
    git("push", "-q", str(remote), f"{moved}:refs/heads/repair-7", cwd=workspace)
    with pytest.raises(PermissionError, match="differs from the reviewed candidate"):
        admit()
    assert service.store.records("service_event") == {}


@pytest.mark.parametrize("planted", ["include-fifo", "packed-refs", "linked-worktree",
                                     "head-fifo", "ref-fifo", "detached"])
def test_admission_reads_the_checkout_head_as_data(tmp_path, monkeypatch, planted):
    workspace, remote, base, head = git_fixture(tmp_path)
    checkout, dotgit = workspace, workspace / ".git"
    if planted == "include-fifo":
        # Any Git process that read this checkout's configuration would wait on the FIFO.
        os.mkfifo(dotgit / "stall.fifo")
        with (dotgit / "config").open("a") as config:
            config.write(f"[include]\n\tpath = {dotgit / 'stall.fifo'}\n")
    elif planted == "packed-refs":
        git("pack-refs", "--all", cwd=workspace)
        assert not (dotgit / "refs" / "heads" / "repair-7").exists()
    elif planted == "linked-worktree":
        git("checkout", "-q", "main", cwd=workspace)
        checkout = tmp_path / "linked"
        git("worktree", "add", "-q", str(checkout), "repair-7", cwd=workspace)
    elif planted == "head-fifo":
        (dotgit / "HEAD").unlink()
        os.mkfifo(dotgit / "HEAD")
    elif planted == "ref-fifo":
        (dotgit / "refs" / "heads" / "repair-7").unlink()
        os.mkfifo(dotgit / "refs" / "heads" / "repair-7")
    else:
        git("checkout", "-q", "--detach", cwd=workspace)
    service = service_fixture(tmp_path, checkout, remote, fake_gh(tmp_path))
    service.store.acquire("pr:fixture/repo#7", "corral")
    receipt = trusted_review(service, tmp_path, head, base)
    stub_submission(service, monkeypatch)
    monkeypatch.setattr(repair_objects, "LOCAL_TIMEOUT_SECONDS", 5)
    calls, original = [], subprocess.run

    def record(command, *args, **kwargs):
        calls.append((list(map(str, command)), kwargs))
        return original(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", record)
    started = time.monotonic()
    if planted in ("include-fifo", "packed-refs", "linked-worktree"):
        admitted_event = service.submit_pr_repair(
            "demo", 7, receipt["receipt_id"], expected_head=head, expected_base=base)["event"]
        assert admitted_event["role"] == "repair"
    else:
        with pytest.raises(PermissionError, match="must be clean at the exact"):
            service.submit_pr_repair(
                "demo", 7, receipt["receipt_id"], expected_head=head, expected_base=base)
    assert time.monotonic() - started < repair_objects.LOCAL_TIMEOUT_SECONDS
    # Git reaches the checkout only as the work tree of Corral's own repository, and never
    # inherits the service's standard input.
    roots = {str(checkout), str(checkout.resolve())}
    git_calls = [(command, kwargs) for command, kwargs in calls if command[0] == "git"]
    assert any("fetch" in command for command, _kwargs in git_calls)
    for command, kwargs in git_calls:
        assert "-C" not in command and str(kwargs.get("cwd")) not in roots
        for argument in command:
            if any(root in argument for root in roots):
                assert argument in {"--work-tree=" + root for root in roots}, command
        assert kwargs.get("stdin") is subprocess.DEVNULL or kwargs.get("input") is not None


def test_branch_publication_pushes_exact_accepted_commit_once(tmp_path, monkeypatch):
    case = admitted(tmp_path, monkeypatch)
    service, event = case.service, case.event
    candidate = accepted_repair(service, event, case.workspace)

    before = int(time.time())
    published = service.publish_pr_repair("demo", event["event_id"])
    after = int(time.time())
    assert published["published"] is True and published["old_head"] == case.head
    assert published["publisher"] == {"login": "fixture-publisher", "id": 101,
                                       "account_ref": "fixture-account",
                                       "auth_mode": "fixture-user"}
    pushed = remote_head(case.remote)
    assert pushed == published["new_head"]
    assert remote_files(case.remote, pushed) == {"change.py": b"VALUE = 2\n"}
    assert git("--git-dir", str(case.remote), "rev-parse", pushed + "^") == case.head
    assert service.publish_pr_repair("demo", event["event_id"]) == published
    assert len(service.store.records("repair_publish_intent")) == 1
    commit = service.store.get("repair_commit", event["task_id"] + ":g1")
    assert commit["candidate_manifest"] == candidate["digest"]
    # The commit carries the real time it was first built, persisted once for this operation.
    seed = service.store.get("repair_commit_seed", event["task_id"] + ":g1")
    assert before <= seed["admitted_at"] <= after
    assert commit["admitted_at"] == seed["admitted_at"]
    assert commit_dates(case.remote, pushed) == (seed["admitted_at"], seed["admitted_at"])


def test_branch_publication_publishes_only_the_accepted_manifest_bytes(tmp_path, monkeypatch):
    case = admitted(tmp_path, monkeypatch)
    service, event = case.service, case.event
    with pytest.raises(PermissionError, match="accepted terminal"):
        service.publish_pr_repair("demo", event["event_id"])
    accepted_repair(service, event, case.workspace)

    # A manifest whose bytes no longer match the verifier receipt is refused.
    path = service.controller.artifacts / event["task_id"] / "candidate_manifest.json"
    original = path.read_text()
    tampered = json.loads(original)
    tampered["files"]["change.py"]["data"] = "VkFMVUUgPSAzCg=="
    path.write_text(json.dumps(tampered))
    with pytest.raises(PermissionError, match="differs from verifier receipt"):
        service.publish_pr_repair("demo", event["event_id"])
    path.write_text(original)

    # After acceptance the worker rewrites the candidate and plants an unregistered file.
    # Neither reaches the branch: the commit is built from the accepted bytes alone.
    (case.workspace / "change.py").write_text("VALUE = 3\n")
    (case.workspace / "outside.py").write_text("UNAUTHORIZED = True\n")
    published = service.publish_pr_repair("demo", event["event_id"])
    assert remote_files(case.remote, published["new_head"]) == {"change.py": b"VALUE = 2\n"}


@pytest.mark.parametrize("planted", ["filter-include", "worktree", "replace-ref",
                                     "ssh-redirect"])
def test_publication_ignores_worker_controlled_git_state(tmp_path, monkeypatch, planted):
    helper = fake_credential_helper(tmp_path)
    case = admitted(tmp_path, monkeypatch, repository={"github": {
        "executable": str(tmp_path / "gh-fixture"), "auth_mode": "fixture-user",
        "git_credential_helper": [str(helper)]}})
    service, event, workspace = case.service, case.event, case.workspace
    accepted_repair(service, event, workspace)
    marker = tmp_path / "never.ran"
    if planted == "filter-include":
        # Probe 1: a filter defined through include.path, which a --local config read misses.
        marker = install_filter_trap(tmp_path, workspace)
    elif planted == "worktree":
        # Probe 2: core.worktree redirects Git to bytes the verifier never saw.
        shadow = tmp_path / "shadow"
        shadow.mkdir()
        (shadow / "change.py").write_text("VALUE = 666\n")
        git("config", "core.worktree", str(shadow), cwd=workspace)
    elif planted == "replace-ref":
        # Probe 3: a replace ref swaps the admitted head for a tree with an extra path.
        blob = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=workspace,
                              input=b"EXTRA = True\n", capture_output=True,
                              check=True).stdout.decode().strip()
        entries = git("ls-tree", case.head, cwd=workspace) + f"\n100644 blob {blob}\textra.py\n"
        tree = subprocess.run(["git", "mktree"], cwd=workspace, input=entries.encode(),
                              capture_output=True, check=True).stdout.decode().strip()
        swapped = git("commit-tree", tree, "-p", case.base, "-m", "swapped", cwd=workspace)
        git("replace", case.head, swapped, cwd=workspace)
        assert "extra.py" in git("ls-tree", "-r", "--name-only", "HEAD", cwd=workspace)
    else:
        # Probe 4: core.sshCommand, which a case-sensitive denylist never matched, plus a
        # URL rewrite that would route the push through it.
        script, marker = trap(tmp_path, "ssh-trap", passthrough=False)
        git("config", "core.sshCommand", str(script), cwd=workspace)
        git("config", "url.ssh://attacker.invalid/x.git.insteadOf", str(case.remote),
            cwd=workspace)

    published = service.publish_pr_repair("demo", event["event_id"])
    assert published["published"] is True
    assert remote_head(case.remote) == published["new_head"]
    assert remote_files(case.remote, published["new_head"]) == {"change.py": b"VALUE = 2\n"}
    assert git("--git-dir", str(case.remote), "rev-parse",
               published["new_head"] + "^") == case.head
    assert not marker.exists()
    if planted == "filter-include":
        # Control: an ordinary add in the checkout runs the planted filter.
        git("add", "-A", cwd=workspace)
        assert marker.exists()


def test_publisher_never_runs_git_in_the_worker_checkout(tmp_path, monkeypatch):
    case = admitted(tmp_path, monkeypatch)
    accepted_repair(case.service, case.event, case.workspace)
    roots = {str(case.workspace), str(case.workspace.resolve())}
    calls = []
    original = subprocess.run

    def record(command, *args, **kwargs):
        calls.append((list(map(str, command)), str(kwargs.get("cwd") or os.getcwd()),
                      {key: value for key, value in (kwargs.get("env") or {}).items()
                       if key.startswith("GIT_")}))
        return original(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", record)
    published = case.service.publish_pr_repair("demo", case.event["event_id"])
    assert published["published"] is True
    git_calls = [call for call in calls if call[0][0] == "git"]
    assert any("commit-tree" in command for command, _cwd, _env in git_calls)
    for command, cwd, env in calls:
        text = " ".join([*command, cwd, *env.values()])
        assert not any(root in text for root in roots), command
    for command, _cwd, env in git_calls:
        assert env["GIT_NO_REPLACE_OBJECTS"] == "1"
        assert env["GIT_CONFIG_NOSYSTEM"] == "1" and env["GIT_CONFIG_GLOBAL"] == os.devnull


def test_publication_refuses_a_candidate_that_drops_collateral_paths(tmp_path, monkeypatch):
    case = admitted(tmp_path, monkeypatch, extra={"pkg/mod.py": "MOD = 1\n"},
                    candidate_paths=["change.py", "pkg"])
    service, event, workspace = case.service, case.event, case.workspace
    # The candidate turns directory pkg/ into a file, which would silently drop pkg/mod.py.
    (workspace / "pkg" / "mod.py").unlink()
    (workspace / "pkg").rmdir()
    (workspace / "pkg").write_text("FLAT = True\n")
    accepted_repair(service, event, workspace)
    with pytest.raises(PermissionError, match="outside the registered candidate"):
        service.publish_pr_repair("demo", event["event_id"])
    assert service.store.records("repair_publish_intent") == {}
    assert remote_head(case.remote) == case.head


def test_publication_deletes_a_removed_candidate(tmp_path, monkeypatch):
    case = admitted(tmp_path, monkeypatch, extra={"gone.py": "GONE = 1\n"},
                    candidate_paths=["change.py", "gone.py"])
    (case.workspace / "gone.py").unlink()
    accepted_repair(case.service, case.event, case.workspace)
    published = case.service.publish_pr_repair("demo", case.event["event_id"])
    assert remote_files(case.remote, published["new_head"]) == {"change.py": b"VALUE = 2\n"}


def test_publication_refuses_a_policy_changed_since_admission(tmp_path, monkeypatch):
    case = admitted(tmp_path, monkeypatch)
    service, event = case.service, case.event
    accepted_repair(service, event, case.workspace)
    policy = service.repositories["demo"]["repair_policies"]["advisory"]
    admitted_identity = dict(policy["commit_identity"])
    policy["commit_identity"] = {"name": "Someone Else", "email": "else@example.invalid"}
    with pytest.raises(PermissionError, match="changed since admission"):
        service.publish_pr_repair("demo", event["event_id"])
    policy["commit_identity"] = admitted_identity
    policy["allowed_prs"] = [8]
    with pytest.raises(PermissionError, match="changed since admission"):
        service.publish_pr_repair("demo", event["event_id"])
    policy["allowed_prs"] = [7]
    assert service.store.records("repair_publish_intent") == {}
    published = service.publish_pr_repair("demo", event["event_id"])
    author = git("--git-dir", str(case.remote), "show", "-s", "--format=%an <%ae>",
                 published["new_head"])
    assert author == "Corral Repair <corral@example.invalid>"


def test_reconcile_refuses_an_intent_for_another_repository(tmp_path, monkeypatch):
    case = admitted(tmp_path, monkeypatch)
    case.service.store.put_once("repair_publish_intent", "foreign", {
        "intent": "foreign", "pr": "other/repo#7", "task": case.event["task_id"]})
    with pytest.raises(PermissionError, match="another repository"):
        case.service.reconcile_pr_repair("demo", "foreign")


def lose_push(monkeypatch) -> None:
    original = RepairObjects.run

    def lose_before_push(self, *args, **kwargs):
        if args and args[0] == "push":
            return subprocess.CompletedProcess(args, 1, stdout=b"", stderr=b"lost")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(RepairObjects, "run", lose_before_push)


def test_lost_push_ack_reads_remote_before_same_intent_retry(tmp_path, monkeypatch):
    case = admitted(tmp_path, monkeypatch)
    service, event = case.service, case.event
    accepted_repair(service, event, case.workspace)
    original = RepairObjects.run
    lose_push(monkeypatch)
    with pytest.raises(PermissionError, match="not confirmed"):
        service.publish_pr_repair("demo", event["event_id"])
    intent = next(iter(service.store.records("repair_publish_intent")))
    assert service.reconcile_pr_repair("demo", intent)["status"] == "not-delivered"
    assert remote_head(case.remote) == case.head

    monkeypatch.setattr(RepairObjects, "run", original)
    published = service.publish_pr_repair("demo", event["event_id"])
    assert published["published"] is True
    assert published["intent"] == intent


def test_retry_rebuilds_a_lost_commit_object_with_the_same_identity(tmp_path, monkeypatch):
    case = admitted(tmp_path, monkeypatch)
    service, event = case.service, case.event
    accepted_repair(service, event, case.workspace)
    original = RepairObjects.run
    lose_push(monkeypatch)
    with pytest.raises(PermissionError, match="not confirmed"):
        service.publish_pr_repair("demo", event["event_id"])
    recorded = service.store.get("repair_commit", event["task_id"] + ":g1")

    # The object store is lost; the retry fetches the head again and rebuilds the same commit.
    monkeypatch.setattr(RepairObjects, "run", original)
    fresh = RepairObjects(tmp_path / "fresh-objects.git", str(case.remote),
                          allow_file_remote=True)
    assert not fresh.has_commit(recorded["new_head"])
    monkeypatch.setattr(BranchPublisher, "_objects", lambda _self: fresh)
    published = service.publish_pr_repair("demo", event["event_id"])
    assert published["new_head"] == recorded["new_head"] == remote_head(case.remote)


def test_retry_after_lost_commit_record_reuses_persisted_admission_time(tmp_path, monkeypatch):
    case = admitted(tmp_path, monkeypatch)
    service, event = case.service, case.event
    accepted_repair(service, event, case.workspace)
    key = event["task_id"] + ":g1"
    first, retry = 1_790_000_000, 1_790_003_600
    created = []
    original_run, original_put = RepairObjects.run, service.store.put_once

    def observe_commit(self, *args, **kwargs):
        value = original_run(self, *args, **kwargs)
        if args and args[0] == "commit-tree":
            created.append(value.stdout.decode().strip())
        return value

    def crash_before_commit_record(kind, record_key, value):
        if kind == "repair_commit":
            raise RuntimeError("crash after the commit object, before its record")
        return original_put(kind, record_key, value)

    monkeypatch.setattr(RepairObjects, "run", observe_commit)
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
    pushed = remote_head(case.remote)
    assert pushed == published["new_head"]
    assert commit_dates(case.remote, pushed) == (first, first)


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
    case = admitted(tmp_path, monkeypatch)
    service, event = case.service, case.event
    accepted_repair(service, event, case.workspace)
    original = RepairObjects.run
    bounds = []

    def push_lands_then_times_out(self, *args, **kwargs):
        if args and args[0] == "push":
            bounds.append(kwargs.get("timeout"))
            original(self, *args, **kwargs)
            raise repair_objects.GitTimeout("push", kwargs.get("timeout"))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(RepairObjects, "run", push_lands_then_times_out)
    published = service.publish_pr_repair("demo", event["event_id"])
    assert bounds == [github_branch.PUSH_TIMEOUT_SECONDS]
    assert published["published"] is True
    assert remote_head(case.remote) == published["new_head"]


def test_git_timeouts_never_expose_the_remote_url_or_credential_helper(
        tmp_path, monkeypatch, caplog, capfd):
    # A remote that accepts the connection and never answers, behind a URL that carries a
    # credential, with a credential helper whose argv names an account.
    listener = socket.create_server(("127.0.0.1", 0))
    userinfo, helper_argument = "fixture-user:fixture-password-4c1d", "--account=fixture-9e27"
    url = f"http://{userinfo}@127.0.0.1:{listener.getsockname()[1]}/fixture/repo.git"
    for name in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)
    helper = fake_credential_helper(tmp_path)
    workspace, remote, base, head = git_fixture(tmp_path)
    service = service_fixture(tmp_path, workspace, remote, fake_gh(tmp_path), repository={
        "remote_url": url, "github": {"executable": str(tmp_path / "gh-fixture"),
                                      "git_credential_helper": [str(helper), helper_argument]}})
    service.store.acquire("pr:fixture/repo#7", "corral")
    receipt = trusted_review(service, tmp_path, head, base)
    stub_submission(service, monkeypatch)
    monkeypatch.setattr(repair_objects, "FETCH_TIMEOUT_SECONDS", 1)
    commands, original = [], subprocess.run

    def record(command, *args, **kwargs):
        commands.append(list(map(str, command)))
        return original(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", record)
    caplog.set_level(logging.DEBUG)
    try:
        with pytest.raises(repair_objects.GitTimeout, match="fetch") as fetch_timeout:
            service.submit_pr_repair(
                "demo", 7, receipt["receipt_id"], expected_head=head, expected_base=base)
        objects = RepairObjects.for_repository(service.store, service.repositories["demo"])
        with pytest.raises(repair_objects.GitTimeout, match="push") as push_timeout:
            objects.push(head, "repair-7", timeout=1)
    finally:
        listener.close()
    # Both commands carried the credential, so its absence below is not vacuous.
    for operation in ("fetch", "push"):
        assert any(operation in command and url in command
                   and any(helper_argument in argument for argument in command)
                   for command in commands)
    captured = capfd.readouterr()
    observed = [caplog.text, captured.out, captured.err]
    for error in (fetch_timeout.value, push_timeout.value):
        assert error.__cause__ is None and error.__context__ is None
        observed += ["".join(traceback.format_exception(type(error), error, error.__traceback__)),
                     repr(error), repr(vars(error))]
    for text in observed:
        for secret in (userinfo, helper_argument, str(helper)):
            assert secret not in text
    assert service.store.records("service_event") == {}


def test_repair_objects_refuse_file_remotes_outside_development_fixtures(tmp_path):
    with pytest.raises(PermissionError, match="development fixtures"):
        RepairObjects(tmp_path / "objects.git", str(tmp_path / "remote.git"))
    with pytest.raises(PermissionError, match="branch name is invalid"):
        repair_objects.validate_branch("-x")
