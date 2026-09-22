import io
import json
import sys
import time

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
