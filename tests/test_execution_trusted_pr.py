import json
import subprocess
import sys
from pathlib import Path

import pytest

from corral.execution.github_candidate import GitObjects, prepare
from corral.execution.service import Service
from corral.execution.store import digest


def git_repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    (path / "input.txt").write_text("base\n")
    (path / "unrelated.txt").write_text("not selected\n")
    subprocess.run(["git", "add", "input.txt", "unrelated.txt"], cwd=path, check=True)
    subprocess.run(["git", "-c", "user.name=Fixture", "-c",
                    "user.email=f@example.invalid", "commit", "-qm", "base"],
                   cwd=path, check=True)
    return path


def pr_fixture(tmp_path: Path, *, fetched_head_matches=True, advanced_base=False):
    source = git_repo(tmp_path / "pr-source")
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=source, check=True,
                          text=True, capture_output=True).stdout.strip()
    (source / "runme.sh").write_text("#!/bin/sh\ntouch should-not-exist\n")
    (source / "input.txt").write_text("candidate\n")
    subprocess.run(["git", "add", "."], cwd=source, check=True)
    subprocess.run(["git", "-c", "user.name=Fixture", "-c",
                    "user.email=f@example.invalid", "commit", "-qm", "candidate"],
                   cwd=source, check=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=source, check=True,
                          text=True, capture_output=True).stdout.strip()
    base_tip = base
    if advanced_base:
        subprocess.run(["git", "checkout", "-qb", "advanced", base], cwd=source, check=True)
        (source / "main-only.txt").write_text("advanced\n")
        subprocess.run(["git", "add", "main-only.txt"], cwd=source, check=True)
        subprocess.run(["git", "-c", "user.name=Fixture", "-c",
                        "user.email=f@example.invalid", "commit", "-qm", "advance main"],
                       cwd=source, check=True)
        base_tip = subprocess.run(["git", "rev-parse", "HEAD"], cwd=source, check=True,
                                  text=True, capture_output=True).stdout.strip()
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", remote], check=True)
    subprocess.run(["git", "push", "-q", remote, f"{base_tip}:refs/heads/main"],
                   cwd=source, check=True)
    pr_oid = head if fetched_head_matches else base
    subprocess.run(["git", "push", "-q", remote, f"{pr_oid}:refs/pull/7/head"],
                   cwd=source, check=True)
    fixture = tmp_path / "pr.json"
    fixture.write_text(json.dumps({"state": "open", "draft": False,
                                   "head": {"sha": head},
                                   "base": {"sha": base, "ref": "main"}}))
    fake_gh = tmp_path / "gh"
    fake_gh.write_text("#!/usr/bin/env python3\nimport os\nfrom pathlib import Path\n"
                       "print(Path(os.environ['CORRAL_PR_FIXTURE']).read_text())\n")
    fake_gh.chmod(0o755)
    return source, remote, fixture, base, head, base_tip, fake_gh


def repository_config(tmp_path, remote, fake_gh):
    return {"github_repository": "fixture/repo",
            "github": {"executable": str(fake_gh), "auth_mode": "fixture-auth"},
            "remote_url": str(remote), "git_object_cache": str(tmp_path / "objects.git"),
            "development_file_remote": True, "allowed_roles": ["implementation", "review"],
            "review_policies": {"advisory": {"inputs": {
                "required_sources": ["input.txt"], "base_ref": "main"}}}}


def service_config(tmp_path: Path, repo: dict) -> Path:
    workspace = git_repo(tmp_path / "registered")
    controller = {"state": str(tmp_path / "state"), "token": "fixture-owner",
                  "default_host": "mini2", "hosts": {"mini2": {
                      "routes": ["deterministic"], "harnesses": [],
                      "cpu": 2, "memory_mb": 1024}}}
    controller_path = tmp_path / "controller.json"
    controller_path.write_text(json.dumps(controller))
    repo.update({"enabled": True, "default_host": "mini2", "allowed_hosts": ["mini2"],
                 "workspaces": {"mini2": str(workspace)}, "task_defaults": {
                     "command": [sys.executable, "-c", "pass"]}})
    path = tmp_path / "service.json"
    path.write_text(json.dumps({"controller_config": str(controller_path),
                                "development_mode": True,
                                "repositories": {"demo": repo}}))
    return path


def test_controller_owned_pr_export_is_immutable_registered_shape(tmp_path, monkeypatch):
    source, remote, fixture, base, head, base_tip, fake_gh = pr_fixture(
        tmp_path, advanced_base=True)
    monkeypatch.setenv("CORRAL_PR_FIXTURE", str(fixture))
    config = repository_config(tmp_path, remote, fake_gh)
    receipt, observation = prepare(
        tmp_path / "state", "fixture/repo", 7, "advisory", config,
        expected_head=head, expected_base=base)
    assert receipt["export_id"] == digest({k: v for k, v in receipt.items()
                                           if k != "export_id"})
    assert receipt["export_digest"] == receipt["selected_files_digest"]
    assert observation["base_ref_tip"] == base_tip and base_tip != base
    assert receipt["diff_sha256"] == receipt["selected_files"][
        ".corral-review/candidate.diff"]["digest"]
    assert set(receipt["selected_files"]) == {
        "input.txt", "runme.sh", ".corral-review/candidate.diff"}
    assert all(item["mode"] == 0o444 for item in receipt["selected_files"].values())
    assert not (source / "should-not-exist").exists()
    workspace = Path(receipt["workspace"])
    assert workspace.is_absolute() and (workspace / "input.txt").read_text() == "candidate\n"
    reused, second_observation = prepare(
        tmp_path / "state", "fixture/repo", 7, "advisory", config)
    assert reused == receipt and second_observation["base_ref_tip"] == base_tip


def test_git_fetch_uses_only_explicit_trusted_credential_helper(tmp_path, monkeypatch):
    cache = tmp_path / "objects.git"
    cache.mkdir()
    helper = tmp_path / "gh"
    helper.write_text("#!/bin/sh\nexit 0\n")
    helper.chmod(0o700)
    observed = {}

    def run(command, **kwargs):
        observed.update(command=command, kwargs=kwargs)
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    objects = GitObjects(cache, "https://example.invalid/private.git",
                         credential_helper=[str(helper), "auth", "git-credential"])
    monkeypatch.setattr(subprocess, "run", run)
    objects.run("status")
    rendered = " ".join(observed["command"])
    assert "credential.helper=!" in rendered and "auth git-credential" in rendered
    assert observed["kwargs"]["env"]["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert "TOKEN" not in rendered


def test_export_reconciles_crash_after_rename_before_receipt(tmp_path, monkeypatch):
    _, remote, fixture, _, _, _, fake_gh = pr_fixture(tmp_path)
    monkeypatch.setenv("CORRAL_PR_FIXTURE", str(fixture))
    config = repository_config(tmp_path, remote, fake_gh)
    import corral.execution.github_candidate as candidate
    original = candidate._write_receipt
    monkeypatch.setattr(candidate, "_write_receipt",
                        lambda *_args: (_ for _ in ()).throw(KeyboardInterrupt("crash")))
    with pytest.raises(KeyboardInterrupt, match="crash"):
        prepare(tmp_path / "state", "fixture/repo", 7, "advisory", config)
    monkeypatch.setattr(candidate, "_write_receipt", original)
    receipt, _observation = prepare(
        tmp_path / "state", "fixture/repo", 7, "advisory", config)
    assert Path(receipt["workspace"]).is_dir()
    assert len(list((tmp_path / "state" / "trusted-export-receipts").glob("*.json"))) == 1


def test_pr_admission_rejects_wrong_head_and_fetched_source_before_task(tmp_path, monkeypatch):
    _, remote, fixture, _, _, _, fake_gh = pr_fixture(tmp_path)
    monkeypatch.setenv("CORRAL_PR_FIXTURE", str(fixture))
    path = service_config(tmp_path, repository_config(tmp_path, remote, fake_gh))
    service = Service(path)
    with pytest.raises(PermissionError, match="requested head"):
        service.submit_pr_review("demo", 7, "advisory", expected_head="f" * 40)
    assert service.store.records("request") == {}
    mismatch = tmp_path / "mismatch"
    mismatch.mkdir()
    _, bad_remote, bad_fixture, _, _, _, bad_gh = pr_fixture(
        mismatch, fetched_head_matches=False)
    monkeypatch.setenv("CORRAL_PR_FIXTURE", str(bad_fixture))
    changed = json.loads(path.read_text())
    changed["repositories"]["demo"].update(
        remote_url=str(bad_remote), github={"executable": str(bad_gh),
                                           "auth_mode": "fixture-auth"},
        git_object_cache=str(tmp_path / "bad.git"))
    path.write_text(json.dumps(changed))
    with pytest.raises(PermissionError, match="differs from fetched"):
        Service(path).submit_pr_review("demo", 7, "advisory")
    assert service.store.records("request") == {}
