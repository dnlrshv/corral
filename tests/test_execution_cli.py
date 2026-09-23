import io
import json
import sys
import time
from pathlib import Path

import pytest

from corral.execution import cli
from tests.test_execution_regressions import bind_interrupted_attempt, setup, spec  # noqa: F401


def test_dispatch_scope_effective_pause_and_submission_time(setup, tmp_path, monkeypatch, capsys):  # noqa: F811
    controller, repo = setup
    first = controller.submit("owner", "one", spec(repo))
    timestamp = controller.store.get("initial", first)["submitted"]
    assert controller.submit("owner", "one", spec(repo)) == first
    assert controller.store.get("initial", first)["submitted"] == timestamp
    time.sleep(.002)
    second = controller.submit("owner", "two", spec(repo))
    assert controller.store.get("initial", second)["submitted"] > timestamp
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"state": str(controller.store.path.parent), "token": "owner",
        "hosts": controller.hosts, "default_host": "fixture", "execution_host": "fixture"}))
    monkeypatch.setattr(sys, "argv", ["execution", "--config", str(config)])
    calls = []
    monkeypatch.setattr(cli.subprocess, "Popen", lambda args, **kwargs: calls.append(args))
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"action": "dispatch", "task_id": first})))
    cli.main()
    assert json.loads(capsys.readouterr().out)["admitted"] == [first]
    assert len(calls) == 1 and calls[0][-1] == first
    controller.steer("owner", first, "pause", {"pause_dispatch": True, "mode": "wave"})
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"action": "dispatch-wave", "task_ids": [first, second]})))
    cli.main()
    assert json.loads(capsys.readouterr().out)["admitted"] == [second]
    assert controller.context(first)["mode"] == "wave"


def test_safe_config_diagnostic_and_cli_show_config(tmp_path, capsys):
    from corral.redaction import safe_config_diagnostic

    raw_config = {
        "state": str(tmp_path / "state"),
        "token": "secret_controller_bearer_12345",
        "api_key": "sk-ant-api03-secretkey12345",
        "hosts": {"local": {"cpu": 4, "memory_mb": 8192}},
        "default_host": "local",
        "execution_host": "local",
        "total_tokens": 5000,
        "input_tokens": "2500",
        "nested": {"token": "nested_secret", "normal": "value"},
    }

    # 1. safe_config_diagnostic suppresses credentials and preserves numeric counters
    diagnostic = safe_config_diagnostic(raw_config)
    assert diagnostic["token"] == "[REDACTED]"
    assert diagnostic["api_key"] == "[REDACTED]"
    assert diagnostic["nested"]["token"] == "[REDACTED]"
    assert diagnostic["nested"]["normal"] == "value"
    assert diagnostic["total_tokens"] == 5000
    assert diagnostic["input_tokens"] == "2500"
    assert diagnostic["state"] == str(tmp_path / "state")
    assert diagnostic["default_host"] == "local"

    # Serializes as valid JSON
    serialized = json.dumps(diagnostic, indent=2)
    assert "secret_controller_bearer_12345" not in serialized
    assert "sk-ant-api03" not in serialized
    assert json.loads(serialized) == diagnostic

    # 2. CLI --show-config outputs safely redacted JSON diagnostic
    config_file = tmp_path / "corral_config.json"
    config_file.write_text(json.dumps(raw_config))

    cli.main(["--config", str(config_file), "--show-config"])
    out = capsys.readouterr().out
    assert "secret_controller_bearer_12345" not in out
    assert "[REDACTED]" in out
    loaded_cli_out = json.loads(out)
    assert loaded_cli_out["token"] == "[REDACTED]"
    assert loaded_cli_out["default_host"] == "local"


def test_cli_error_path_redacts_credentials(tmp_path, monkeypatch, capsys):
    from corral.redaction import redact_text

    secret_in_trace = "sk-ant-api03-SECRETINTRACE12345"
    err = f"Traceback (most recent call last):\n  File 'cli.py', line 10\nValueError: failed with {secret_in_trace}\n"
    redacted_err = redact_text(err, marker="[REDACTED]")
    assert secret_in_trace not in redacted_err
    assert "[REDACTED]" in redacted_err


def test_controller_authoritative_records_unaltered_by_generic_redactor(setup, tmp_path):  # noqa: F811
    controller, repo = setup
    task_spec = spec(repo)
    task_spec["objective"] = "fix bug"
    task_id = controller.submit("owner", "canonical_test", task_spec)
    context = controller.context(task_id)
    assert context["objective"] == "fix bug"
    assert controller.store.get("request", task_id)["objective"] == "fix bug"
    assert context["candidate_paths"] == ["result.json"]


def test_cli_reconcile_uses_installed_collector_not_request_observation(setup, tmp_path, monkeypatch, capsys):  # noqa: F811
    controller, repo = setup
    task = controller.submit("owner", "cli-cancelled", spec(repo))
    resource = "workspace:" + str(repo.resolve())
    epoch = controller.store.acquire(resource, task)
    controller.store.transition_owner(resource, task, epoch, "uncertain")
    state = {"status": "uncertain", "attempt": "cli-attempt", "epoch": epoch, "generation": 1,
             "pid": 99999997, "pgid": 99999997}
    bind_interrupted_attempt(controller, task, state)
    controller.store.replace("state", task, state)
    controller.cancel("owner", task)
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"state": str(controller.store.path.parent), "token": "owner",
                                  "hosts": controller.hosts, "default_host": "fixture",
                                  "execution_host": "fixture"}))
    monkeypatch.setattr(sys, "argv", ["execution", "--config", str(config)])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"action": "reconcile", "task_id": task})))
    cli.main()
    assert json.loads(capsys.readouterr().out)["terminal_status"] == "cancelled"
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"action": "reconcile", "task_id": task,
                                                                 "observation": {"forged": True}})))
    with pytest.raises(PermissionError, match="does not accept caller-provided"):
        cli.main()


def test_cli_reconcile_refuses_reader_bound_to_another_attempt(setup, tmp_path, monkeypatch):  # noqa: F811
    controller, repo = setup
    task = controller.submit("owner", "reader-binding", spec(repo))
    resource = "workspace:" + str(repo.resolve())
    epoch = controller.store.acquire(resource, task)
    controller.store.transition_owner(resource, task, epoch, "uncertain")
    state = {"status": "uncertain", "attempt": "actual", "epoch": epoch, "generation": 1,
             "pid": 99999996, "pgid": 99999996}
    bind_interrupted_attempt(controller, task, state)
    controller.store.replace("state", task, state)
    controller.cancel("owner", task)
    controller.hosts["fixture"]["reconciliation_delivery_reader"] = {
        "kind": "github-pr-readback-v1", "task": "other", "attempt": "other", "generation": "1",
        "epoch": str(epoch), "repo": "fixture/repo", "pr": "1", "head": "a", "base": "b", "actor": "fixture"}
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"state": str(controller.store.path.parent), "token": "owner",
                                  "hosts": controller.hosts, "default_host": "fixture", "execution_host": "fixture"}))
    monkeypatch.setattr(sys, "argv", ["execution", "--config", str(config)])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"action": "reconcile", "task_id": task})))
    with pytest.raises(PermissionError, match="not bound to the current task attempt"):
        cli.main()


def test_cli_loads_private_route_secret_and_forwards_only_allowlisted_value(tmp_path):
    """A detached CLI worker gets the configured route secret, never broad process env."""
    import os
    import subprocess
    from corral.execution import containment
    from corral.execution.store import Store

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=f@example.invalid",
                    "commit", "--allow-empty", "-qm", "base"], cwd=workspace, check=True)
    (workspace / "result.py").write_text("value = 'before'\n")
    home = tmp_path / "native-home"
    (home / "auth").mkdir(parents=True)
    (home / "auth" / "fixture.json").write_text("fixture runtime state\n")
    harness = tmp_path / "credential-harness.py"
    harness.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\nfrom pathlib import Path\n"
        "args = {sys.argv[i]: sys.argv[i + 1] for i in range(1, len(sys.argv), 2)}\n"
        "Path(args['--workspace']).joinpath('result.py').write_text(\"value = 'after'\\n\")\n"
        "Path(args['--result']).write_text(json.dumps({'answer': 'ok', 'changed': ['result.py']}))\n"
        "print(json.dumps({'schema': 'corral-synthetic-v1', 'synthetic': True, 'status': 'completed',\n"
        " 'narrative': 'updated fixture', 'identity': {'model': args['--model'], 'effort': args['--effort'],\n"
        " 'harness': 'credential-harness.py', 'provider': 'fixture', 'account_ref': 'fixture-account'},\n"
        " 'detail': {'route_secret_is_private': os.environ.get('ROUTE_SECRET') == 'private-fixture',\n"
        "            'unrelated_present': 'UNRELATED_SECRET' in os.environ},\n"
        " 'usage': {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2}}))\n")
    harness.chmod(0o755)
    secrets = tmp_path / "provider-secrets.json"
    secrets.write_text(json.dumps({"ROUTE_SECRET": "private-fixture",
                                   "UNRELATED_SECRET": "never-forward"}))
    secrets.chmod(0o600)
    profile = {"id": "fixture-coding", "model": "fixture-model", "effort": "medium",
               "harness": "credential-harness.py", "version": "1", "route": "fixture-route",
               "roles": ["implementation"], "tools": ["read", "edit", "shell", "test"],
               "context": 1000, "provider": "fixture", "account_ref": "fixture-account"}
    config = {"state": str(tmp_path / "state"), "token": "owner", "secret_env": str(secrets),
              "default_host": "fixture", "execution_host": "fixture", "profiles": [profile],
              "hosts": {"fixture": {"routes": ["fixture-route"],
                         "harnesses": ["credential-harness.py"], "cpu": 2, "memory_mb": 256,
                         "protected_paths": [str(home)],
                         "native_routes": {"fixture-route": {
                             "harness": "credential-harness.py", "binary": str(harness),
                             "argv": ["--workspace", "{workspace}", "--result", "{result_file}",
                                      "--model", "{model}", "--effort", "{effort}"],
                             "envelope": "corral-synthetic-v1", "provider": "fixture",
                             "account_ref": "fixture-account", "endpoint": "local-fixture",
                             "supported_models": ["fixture-model"],
                             "supported_efforts": ["medium"], "credential_env": ["ROUTE_SECRET"],
                             "runtime_read": [str(home)], "runtime_home": str(home),
                             "synthetic": True, "version": "1"}}}}}
    config_path = tmp_path / "controller.json"
    config_path.write_text(json.dumps(config))
    env = {**os.environ, "ROUTE_SECRET": "ambient-wrong", "UNRELATED_SECRET": "ambient-unrelated"}
    submit = subprocess.run([sys.executable, "-m", "corral.execution.cli", "--config", str(config_path)],
                            input=json.dumps({"action": "submit", "request_id": "private-route-secret",
                                              "spec": {"repo": "fixture/repo", "workspace": str(workspace),
                                                       "host": "fixture", "role": "implementation",
                                                       "profile_id": "fixture-coding", "objective": "Update result.",
                                                       "candidate_paths": ["result.py"], "verifier_paths": [],
                                                       "verify": ["/usr/bin/true"],
                                                       "tools": ["read", "edit", "shell", "test"]}}),
                            text=True, capture_output=True, check=True, env=env)
    task = json.loads(submit.stdout)["task"]
    executed = subprocess.run(
        [sys.executable, "-m", "corral.execution.cli", "--config", str(config_path),
         "--execute", task], text=True, capture_output=True, check=False, env=env)
    store = Store(tmp_path / "state" / "controller.sqlite")
    if containment.sandbox_exec() is None:
        # The native boundary is intentionally unsupported on this platform.
        # Check the real CLI's refusal instead of expecting an uncontained run.
        assert executed.returncode != 0
        assert "worker containment could not be demonstrated" in executed.stderr
        state = store.get("state", task)
        assert state["status"] == "refused-before-launch"
        assert state.get("pid") is None
        assert store.get("result", task) is None
        assert (workspace / "result.py").read_text() == "value = 'before'\n"
        return
    assert executed.returncode == 0, executed.stderr
    result = store.get("result", task)
    assert result["accepted"] is True
    assert result["native"]["credential_env_present"] == ["ROUTE_SECRET"]
    assert result["structured"] is not None
    adapter = json.loads((Path(result["artifact_directory"]) / "adapter-result.json").read_text())
    assert adapter["detail"]["harness_detail"] == {
        "route_secret_is_private": True, "unrelated_present": False}
