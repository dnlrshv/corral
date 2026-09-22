import json
import os
import plistlib
import subprocess
import sys
import time
from pathlib import Path

import pytest

from corral.execution.service import Service
from corral.execution.service_client import ServiceClient
from corral.execution.store import Store
from corral.execution.github_candidate import prepare


def git_repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    (path / "input.txt").write_text("base\n")
    subprocess.run(["git", "add", "input.txt"], cwd=path, check=True)
    subprocess.run(["git", "-c", "user.name=Fixture", "-c",
                    "user.email=f@example.invalid", "commit", "-qm", "base"],
                   cwd=path, check=True)
    return path


def pr_fixture(tmp_path: Path, *, fetched_head_matches=True):
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
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", remote], check=True)
    subprocess.run(["git", "push", "-q", remote, f"{base}:refs/heads/main"],
                   cwd=source, check=True)
    pr_oid = head if fetched_head_matches else base
    subprocess.run(["git", "push", "-q", remote, f"{pr_oid}:refs/pull/7/head"],
                   cwd=source, check=True)
    fixture = tmp_path / "pr.json"
    fixture.write_text(json.dumps({"state": "open", "draft": False,
                                   "head": {"sha": head},
                                   "base": {"sha": base, "ref": "main"},
                                   "html_url": "https://example.invalid/pr/7"}))
    fake_gh = tmp_path / "gh"
    fake_gh.write_text("#!/usr/bin/env python3\nimport os\nfrom pathlib import Path\nprint(Path(os.environ['CORRAL_PR_FIXTURE']).read_text())\n")
    fake_gh.chmod(0o755)
    return source, remote, fixture, base, head, fake_gh


def configs(tmp_path: Path, workspace: Path, *, schedules=None, worker=None):
    worker = worker or (
        "import json; from pathlib import Path; "
        "value=Path('input.txt').read_text(); Path('output.txt').write_text(value); "
        "Path('result.json').write_text(json.dumps({'copied': value}))"
    )
    controller = {
        "state": str(tmp_path / "state"), "token": "fixture-owner",
        "default_host": "mini2",
        "hosts": {"mini2": {"routes": ["deterministic"], "harnesses": [],
                              "cpu": 2, "memory_mb": 1024}},
    }
    controller_path = tmp_path / "controller.json"
    controller_path.write_text(json.dumps(controller))
    service = {
        "controller_config": str(controller_path), "max_dispatch_per_tick": 1,
        "development_mode": True,
        "repositories": {"demo": {
            "enabled": True, "default_host": "mini2", "allowed_hosts": ["mini2"],
            "workspaces": {"mini2": str(workspace)},
            "allowed_roles": ["implementation"],
            "task_defaults": {
                "role": "implementation", "candidate_paths": ["output.txt"],
                "command": [sys.executable, "-c", worker],
                "verify": [sys.executable, "-c",
                           "from pathlib import Path; assert Path('output.txt').is_file()"],
            },
        }},
        "schedules": schedules or [],
    }
    service_path = tmp_path / "service.json"
    service_path.write_text(json.dumps(service))
    return service_path


def cli(module: str, *args: object, check=True):
    result = subprocess.run([sys.executable, "-m", module, *map(str, args)],
                            text=True, capture_output=True, check=check)
    return json.loads(result.stdout) if check and result.stdout else result


def wait_event(service: Service, event_id: str, *, target="completed", seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        service.tick()
        status = service.status(event_id)
        if status["event"]["status"] == target:
            return status
        if status["event"]["status"] in {"failed", "blocked", "uncertain", "refused-before-launch"}:
            raise AssertionError(status)
        time.sleep(0.05)
    raise AssertionError(service.status(event_id))


def test_agent_cli_transfers_dirty_input_runs_once_and_returns_without_overwrite(tmp_path):
    source = git_repo(tmp_path / "source")
    target = tmp_path / "target"
    subprocess.run(["git", "clone", "-q", str(source), str(target)], check=True)
    (source / "input.txt").write_text("dirty developer value\n")
    config = configs(tmp_path, target)
    args = ("--config", config, "submit", "--repository", "demo", "--objective",
            "copy the selected input", "--event-id", "event-1", "--source-root", source,
            "--input", "input.txt", "--run")
    args += ("--poll-interval", "0.05")
    first = cli("corral.execution.agent_cli", *args)
    second = cli("corral.execution.agent_cli", *args)
    assert first["event"]["status"] == second["event"]["status"] == "completed"
    assert (target / "input.txt").read_text() == "dirty developer value\n"
    store = Store(tmp_path / "state" / "controller.sqlite")
    assert len(store.records("invocation")) == 1
    runtime = next(iter(store.records("service_runtime").values()))
    assert Path(runtime["executable"]).is_absolute()
    assert runtime["host"] and runtime["pid"] > 0 and len(runtime["loaded_pin"]) == 64
    assert runtime["loaded"]["service"]["path"].endswith("corral/execution/service.py")
    assert first["event"]["worker_identity"]["attempt"]
    returned = tmp_path / "returned" / "output.txt"
    cli("corral.execution.agent_cli", "--config", config, "return", "--event-id",
        "event-1", "--path", "output.txt", "--destination", returned)
    assert returned.read_text() == "dirty developer value\n"
    returned.write_text("newer dev edit\n")
    refused = cli("corral.execution.agent_cli", "--config", config, "return",
                  "--event-id", "event-1", "--path", "output.txt",
                  "--destination", returned, check=False)
    assert refused.returncode != 0
    assert returned.read_text() == "newer dev edit\n"


def test_agent_cli_rejects_secrets_symlinks_and_unsupported_host_before_task(tmp_path):
    source = git_repo(tmp_path / "source")
    target = tmp_path / "target"
    subprocess.run(["git", "clone", "-q", str(source), str(target)], check=True)
    config = configs(tmp_path, target)
    (source / ".env").write_text("TOKEN=secret")
    os.symlink(source / "input.txt", source / "linked.txt")
    base = ("--config", config, "submit", "--repository", "demo", "--objective", "x",
            "--source-root", source)
    secret = cli("corral.execution.agent_cli", *base, "--event-id", "secret", "--input",
                 ".env", check=False)
    linked = cli("corral.execution.agent_cli", *base, "--event-id", "link", "--input",
                 "linked.txt", check=False)
    host = cli("corral.execution.agent_cli", "--config", config, "submit",
               "--repository", "demo", "--objective", "x", "--event-id", "host",
               "--host", "unregistered", check=False)
    assert secret.returncode and linked.returncode and host.returncode
    assert Store(tmp_path / "state" / "controller.sqlite").records("request") == {}


def test_restart_lost_ack_and_schedule_occurrence_do_not_duplicate_workers(tmp_path):
    workspace = git_repo(tmp_path / "repo")
    schedules = [{"id": "maintenance", "repository": "demo", "objective": "scheduled",
                  "interval_seconds": 86400, "start": 0}]
    config = configs(tmp_path, workspace, schedules=schedules)
    service = Service(config)
    service.submit("manual", "demo", "manual")
    now = time.time()
    service.tick(now=now)
    wait_event(service, "manual")
    event = service.store.get("service_event", "manual")
    service.store.replace("service_event", "manual", {**event, "status": "dispatching"})
    restarted = Service(config)
    result = restarted.tick(now=now)
    assert "manual" in result["reconciled"]
    schedule_id = next(key for key in restarted.store.records("service_event")
                       if key.startswith("schedule:maintenance:"))
    wait_event(restarted, schedule_id)
    assert len(restarted.store.records("invocation")) == 2  # manual + one schedule
    assert len([key for key in restarted.store.records("service_event")
                if key.startswith("schedule:maintenance:")]) == 1


def test_admission_recovers_after_event_recorded_before_task(tmp_path, monkeypatch):
    workspace = git_repo(tmp_path / "repo")
    config = configs(tmp_path, workspace)
    service = Service(config)
    original = service._ensure_task
    monkeypatch.setattr(service, "_ensure_task",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("crash")))
    with pytest.raises(RuntimeError, match="crash"):
        service.submit("recover", "demo", "durable admission")
    assert service.store.get("service_event", "recover")["task_id"] is None
    monkeypatch.setattr(service, "_ensure_task", original)
    status = service.submit("recover", "demo", "durable admission")
    assert status["event"]["task_id"]
    assert len(service.store.records("request")) == 1


def test_schedule_cannot_bypass_repository_role_policy(tmp_path):
    workspace = git_repo(tmp_path / "repo")
    schedules = [{"id": "review", "repository": "demo", "objective": "review",
                  "role": "review", "interval_seconds": 60, "start": 0}]
    service = Service(configs(tmp_path, workspace, schedules=schedules))
    with pytest.raises(PermissionError, match="role is not allowed"):
        service.tick(now=time.time())
    assert service.store.records("service_event") == {}


def test_interrupted_source_preparation_resumes_and_config_drift_is_refused(tmp_path, monkeypatch):
    source = git_repo(tmp_path / "source")
    target = tmp_path / "target"
    subprocess.run(["git", "clone", "-q", str(source), str(target)], check=True)
    (source / "input.txt").write_text("changed\n")
    config = configs(tmp_path, target)
    service = Service(config)
    from corral.execution.workspace import manifest
    snapshot = manifest(source, ["input.txt"])
    original = service.controller.transfer

    def interrupted(*args, **kwargs):
        original(*args, **kwargs)
        raise KeyboardInterrupt("simulated process loss after durable receipt")

    monkeypatch.setattr(service.controller, "transfer", interrupted)
    with pytest.raises(KeyboardInterrupt):
        service.submit("prepare", "demo", "apply source", source_snapshot=snapshot)
    assert service.store.get("service_event", "prepare")["status"] == "preparing"
    resumed = Service(config).submit("prepare", "demo", "apply source", source_snapshot=snapshot)
    assert resumed["event"]["status"] == "prepared"
    assert (target / "input.txt").read_text() == "changed\n"

    raw = json.loads(config.read_text())
    raw["repositories"]["demo"]["task_defaults"]["command"] = [sys.executable, "-c", "raise SystemExit(9)"]
    config.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="different content"):
        Service(config).submit("prepare", "demo", "apply source", source_snapshot=snapshot)


def test_long_worker_does_not_block_service_or_other_workspace(tmp_path):
    first = git_repo(tmp_path / "first")
    second = git_repo(tmp_path / "second")
    slow = ("import json,time; from pathlib import Path; time.sleep(1); "
            "Path('output.txt').write_text('slow'); Path('result.json').write_text(json.dumps({'ok':1}))")
    config = configs(tmp_path, first, worker=slow)
    raw = json.loads(config.read_text())
    fast = json.loads(json.dumps(raw["repositories"]["demo"]))
    fast["workspaces"]["mini2"] = str(second)
    fast["task_defaults"]["command"] = [sys.executable, "-c",
        "import json; from pathlib import Path; Path('output.txt').write_text('fast'); Path('result.json').write_text(json.dumps({'ok':1}))"]
    raw["repositories"]["fast"] = fast
    config.write_text(json.dumps(raw))
    service = Service(config)
    service.submit("slow", "demo", "slow task")
    started = time.monotonic()
    service.tick()
    assert time.monotonic() - started < 0.5
    service.submit("fast", "fast", "fast task")
    service.tick()
    fast_status = wait_event(service, "fast")
    assert fast_status["event"]["status"] == "completed"
    assert service.status("slow")["event"]["status"] == "dispatching"
    wait_event(service, "slow")


def test_agent_cli_ssh_endpoint_keeps_manifest_on_dev_side(tmp_path, monkeypatch):
    source = git_repo(tmp_path / "source")
    target = tmp_path / "target"
    subprocess.run(["git", "clone", "-q", str(source), str(target)], check=True)
    (source / "input.txt").write_text("ssh-selected\n")
    service_config = configs(tmp_path, target)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_ssh = fake_bin / "ssh"
    fake_ssh.write_text("#!/bin/sh\nfor last do :; done\nexec /bin/sh -c \"$last\"\n")
    fake_ssh.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake_bin) + os.pathsep + os.environ["PATH"])
    client_config = tmp_path / "agent.json"
    client_config.write_text(json.dumps({"service_endpoint": {
        "transport": "ssh", "ssh_host": "fixture-host", "ssh_options": [],
        "development_mode": True,
        "python": sys.executable, "source": str(Path(__file__).parents[1]),
        "service_config": str(service_config)}}))
    result = cli("corral.execution.agent_cli", "--config", client_config, "submit",
                 "--repository", "demo", "--objective", "remote submit",
                 "--event-id", "ssh-event", "--source-root", source,
                 "--input", "input.txt")
    assert result["event"]["status"] == "prepared"
    assert (target / "input.txt").read_text() == "ssh-selected\n"


def test_active_amendment_changes_behavior_and_continues_same_task(tmp_path):
    workspace = git_repo(tmp_path / "repo")
    worker = """
import json, os, time
from pathlib import Path
for _ in range(100):
    objective = json.loads(Path(os.environ['CORRAL_CONTEXT_PATH']).read_text())['objective']
    if objective == 'write amended behavior':
        break
    time.sleep(0.02)
Path('output.txt').write_text(objective)
Path('result.json').write_text(json.dumps({'objective': objective}))
"""
    config = configs(tmp_path, workspace, worker=worker)
    service = Service(config)
    initial = service.submit("amended", "demo", "write original behavior")
    task_id = initial["event"]["task_id"]
    service.tick()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        state = service.controller.status(service.token, task_id)["state"]
        if state.get("status") == "running":
            break
        time.sleep(0.01)
    service.amend("amended", "change-objective", "write amended behavior")
    status = wait_event(service, "amended")
    assert status["event"]["status"] == "completed"
    assert status["task"]["task"] == task_id
    assert sorted(status["task"]["results"]) == ["1", "2"]
    assert (workspace / "output.txt").read_text() == "write amended behavior"


def test_usage_cli_separates_providers_and_preserves_unknowns(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    path = tmp_path / "usage.jsonl"
    events = [
        {"id": "a1", "invocation": "one", "scope": "session", "epoch": 0,
         "sequence": 1, "mode": "cumulative", "provider": "openai", "model": "astra",
         "session_id": "s1", "role": "implementation", "origin": "native-measured",
         "counters": {"input": 10, "cache": 4, "reasoning": 2}},
        {"id": "a2", "invocation": "one", "scope": "session", "epoch": 0,
         "sequence": 2, "mode": "cumulative", "provider": "openai", "model": "astra",
         "session_id": "s1", "role": "implementation", "origin": "native-measured",
         "counters": {"input": 18, "cache": 7, "reasoning": 3}},
        {"id": "g1", "invocation": "two", "scope": "request", "epoch": 0,
         "sequence": 1, "mode": "delta", "provider": "google", "model": None,
         "session_id": None, "role": "review", "counters": {}},
    ]
    path.write_text("".join(json.dumps(event) + "\n" for event in events))
    first = cli("corral.execution.usage_api", "--state", state, "ingest", "--jsonl", path)
    second = cli("corral.execution.usage_api", "--state", state, "ingest", "--jsonl", path)
    report = cli("corral.execution.usage_api", "--state", state, "report")
    assert first == {"accepted": 3, "conflicts": 0, "duplicates": 0}
    assert second == {"accepted": 0, "conflicts": 0, "duplicates": 3}
    assert {row["provider"] for row in report["groups"]} == {"openai", "google"}
    openai = next(row for row in report["groups"] if row["provider"] == "openai")
    assert openai["observed_fields"] == {"cache": 7, "input": 18, "reasoning": 3}
    google = next(row for row in report["groups"] if row["provider"] == "google")
    assert google["model"] is None and google["session_id"] is None
    assert google["coverage"] == "partial"
    assert report["root_usage"] is None and report["account_total"] is None


def test_launchd_renderer_is_generic_and_plist_valid(tmp_path):
    template = Path(__file__).parents[1] / "templates" / "io.corral.service.plist.in"
    output = tmp_path / "service.plist"
    cli("corral.execution.service_install", "--template", template, "--label",
        "io.corral.fixture", "--executable", Path("/opt/corral/bin/corral-service"),
        "--config", Path("/etc/corral/service.json"), "--interval-seconds", 15,
        "--log-dir", Path("/var/log/corral"), "--output", output)
    loaded = plistlib.loads(output.read_bytes())
    assert loaded["ProgramArguments"][-1] == "tick"
    assert loaded["StartInterval"] == 15 and loaded["RunAtLoad"] is True


def test_installed_service_client_uses_isolated_python_and_safe_cwd(monkeypatch):
    observed = {}

    def run(command, **kwargs):
        observed.update(command=command, kwargs=kwargs)
        return subprocess.CompletedProcess(command, 0, stdout="{}", stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    ServiceClient({"python": "/opt/corral/bin/python", "transport": "local",
                   "service_config": "/etc/corral/service.json"}).call("tick")
    assert observed["command"][:3] == ["/opt/corral/bin/python", "-I", "-m"]
    assert observed["kwargs"]["cwd"] == "/"


def test_controller_owned_pr_export_is_immutable_registered_shape(tmp_path, monkeypatch):
    source, remote, fixture, base, head, fake_gh = pr_fixture(tmp_path)
    monkeypatch.setenv("CORRAL_PR_FIXTURE", str(fixture))
    config = {"github": {"executable": str(fake_gh), "auth_mode": "fixture-auth"},
              "remote_url": str(remote), "git_object_cache": str(tmp_path / "objects.git"),
              "development_file_remote": True,
              "review_policies": {"advisory": {"digest": "1" * 64}}}
    receipt = prepare(tmp_path / "state", "fixture/repo", 7, "advisory", config,
                      expected_head=head, expected_base=base)
    record = {key: value for key, value in receipt.items() if key != "export_id"}
    from corral.execution.store import digest
    assert receipt["export_id"] == digest(record)
    assert receipt["export_digest"] == receipt["selected_files_digest"]
    assert receipt["diff_sha256"] == receipt["selected_files"]["candidate.diff"]["digest"]
    assert receipt["selected_files"]["runme.sh"]["mode"] == 0o100644
    assert not (source / "should-not-exist").exists()
    workspace = Path(receipt["workspace"])
    assert workspace.is_absolute() and (workspace / "input.txt").read_text() == "candidate\n"
    assert workspace.stat().st_mode & 0o222 == 0
    assert prepare(tmp_path / "state", "fixture/repo", 7, "advisory", config) == receipt


def test_pr_admission_rejects_wrong_head_and_fetched_source_before_task(tmp_path, monkeypatch):
    _, remote, fixture, base, head, fake_gh = pr_fixture(tmp_path)
    monkeypatch.setenv("CORRAL_PR_FIXTURE", str(fixture))
    workspace = git_repo(tmp_path / "registered")
    service_path = configs(tmp_path, workspace)
    raw = json.loads(service_path.read_text())
    raw["repositories"]["demo"].update({
        "github_repository": "fixture/repo",
        "github": {"executable": str(fake_gh), "auth_mode": "fixture-auth"},
        "remote_url": str(remote), "git_object_cache": str(tmp_path / "objects.git"),
        "development_file_remote": True,
        "allowed_roles": ["implementation", "review"],
        "review_policies": {"advisory": {"digest": "2" * 64}},
    })
    service_path.write_text(json.dumps(raw))
    service = Service(service_path)
    with pytest.raises(PermissionError, match="requested head"):
        service.submit_pr_review("demo", 7, "advisory", expected_head="f" * 40)
    assert service.store.records("request") == {}
    assert service.store.records("trusted_export") == {}

    mismatch = tmp_path / "mismatch"
    mismatch.mkdir()
    _, bad_remote, bad_fixture, _, _, bad_gh = pr_fixture(
        mismatch, fetched_head_matches=False)
    monkeypatch.setenv("CORRAL_PR_FIXTURE", str(bad_fixture))
    changed = json.loads(service_path.read_text())
    changed["repositories"]["demo"]["remote_url"] = str(bad_remote)
    changed["repositories"]["demo"]["github"]["executable"] = str(bad_gh)
    changed["repositories"]["demo"]["git_object_cache"] = str(tmp_path / "bad.git")
    service_path.write_text(json.dumps(changed))
    with pytest.raises(PermissionError, match="differs from fetched"):
        Service(service_path).submit_pr_review("demo", 7, "advisory")
    assert service.store.records("request") == {}
