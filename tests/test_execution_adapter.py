"""Trusted adapter: package resolution from a foreign cwd, launch guards, usage semantics.

The adapter is controller-side code that runs *outside* the worker boundary. These tests
exercise the real command builder and the real ``run_adapter``; no adapter, process,
command builder or sandbox call is monkeypatched. Model output is explicitly synthetic.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from corral.execution import adapter, containment, native
from corral.execution.adapter import (
    ADAPTER_RESULT,
    BASE_ENV_NAMES,
    _harness_env,
    _substitute,
    build_adapter_command,
    run_adapter,
    source_root,
    write_native_usage,
)

from . import native_support as ns

pytestmark = pytest.mark.skipif(containment.sandbox_exec() is None,
                                reason="real worker containment requires macOS sandbox-exec")

FIXED_CANDIDATE = "def add(a, b):\n    return a + b\n"
SUCCESS_OPS = [
    f"write math_ops.py {ns.b64(FIXED_CANDIDATE)}",
    f"result {ns.b64(json.dumps({'answer': 5}))}",
    "narrative Implemented add().",
    f"usage {json.dumps({'input_tokens': 10, 'output_tokens': 4, 'total_tokens': 14})}",
]


def _prepared(tmp_path: Path, *, ops=SUCCESS_OPS, **env_kwargs):
    """Build a real controller-prepared task directory through the production path."""
    env = ns.native_env(tmp_path, **env_kwargs)
    spec = ns.native_spec(env, ops=list(ops))
    task_id = "adapter-unit"
    task_dir = env["controller"].artifacts / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    context_path = task_dir / "context.json"
    context_path.write_text(json.dumps({**spec, "task": task_id, "attempt": "attempt-1",
                                        "amendment_history": []}))
    prepared = native.prepare(spec=spec, host=env["host"], profile=env["profile"],
                              task_dir=task_dir, workspace=str(env["workspace"]),
                              state_dir=tmp_path / "state", artifacts=env["controller"].artifacts,
                              source_root=source_root(), task_id=task_id,
                              verifier_roots=tuple(env["host"]["verifier_roots"]),
                              usage_path=task_dir / "native-usage.json",
                              context_path=context_path, attempt="attempt-1")
    return env, prepared, task_dir


# --------------------------------------------------------------------------- command builder

def test_build_adapter_command_is_isolated_and_embeds_a_literal_source_root(tmp_path):
    workspace, task_dir = tmp_path / "ws", tmp_path / "task"
    workspace.mkdir()
    task_dir.mkdir()
    command = build_adapter_command(task_dir=task_dir, workspace=workspace, source=source_root())
    assert command[0] == sys.executable
    # -I: no ambient PYTHONPATH, no user site, no worker-writable cwd on sys.path.
    assert command[1] == "-I"
    bootstrap = command[3]
    assert "sys.path.insert(0," in bootstrap and str(Path(source_root()).resolve()) in bootstrap
    assert "runpy.run_module('corral.execution.adapter'" in bootstrap
    assert command[4:] == ["--task-dir", str(task_dir.resolve()),
                           "--workspace", str(workspace.resolve())]


def test_build_adapter_command_refuses_a_source_root_inside_worker_reach(tmp_path):
    workspace, task_dir = tmp_path / "ws", tmp_path / "task"
    workspace.mkdir()
    task_dir.mkdir()
    with pytest.raises(PermissionError, match="must not live inside the workspace"):
        build_adapter_command(task_dir=task_dir, workspace=workspace, source=workspace)
    with pytest.raises(PermissionError, match="must not live inside the task directory"):
        build_adapter_command(task_dir=task_dir, workspace=workspace, source=task_dir)


def test_build_adapter_command_refuses_a_root_without_the_adapter_package(tmp_path):
    empty = tmp_path / "not-corral"
    empty.mkdir()
    with pytest.raises(PermissionError, match="does not contain the adapter package"):
        build_adapter_command(task_dir=tmp_path, workspace=tmp_path, source=empty)


def test_adapter_package_resolves_from_a_foreign_cwd_and_completes(tmp_path):
    """Regression: the adapter must import from any cwd, not only the source root."""
    env, prepared, task_dir = _prepared(tmp_path)
    command = prepared.command
    completed = subprocess.run(command, cwd="/", capture_output=True, text=True,
                               env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)})
    assert completed.returncode == 0, completed.stderr
    assert "ModuleNotFoundError" not in completed.stderr
    assert "No module named corral" not in completed.stderr
    result = json.loads((task_dir / ADAPTER_RESULT).read_text())
    assert result["status"] == "completed"
    assert result["structured"] == {"answer": 5}
    assert (env["workspace"] / "math_ops.py").read_text() == FIXED_CANDIDATE
    # The child really ran under the profile the controller prepared.
    assert result["containment"]["passed"] is True
    assert result["containment"]["profile_digest"] == json.loads(
        (task_dir / "boundary.json").read_text())["profile_digest"]


def test_adapter_from_source_cwd_proves_resolution_is_not_cwd_dependent(tmp_path):
    env, prepared, task_dir = _prepared(tmp_path)
    from_root = subprocess.run(prepared.command, cwd=str(source_root()), capture_output=True,
                               text=True, env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)})
    assert from_root.returncode == 0, from_root.stderr
    assert json.loads((task_dir / ADAPTER_RESULT).read_text())["status"] == "completed"
    assert env is not None


# --------------------------------------------------------------------------- launch guards

def test_adapter_refuses_when_containment_cannot_be_demonstrated(tmp_path):
    """A boundary whose sentinel is gone is a hard refusal, never an unsandboxed launch."""
    env, prepared, task_dir = _prepared(tmp_path)
    for sentinel in prepared.boundary.sentinels:
        Path(sentinel).unlink()
    assert run_adapter(task_dir, env["workspace"]) == 1
    result = json.loads((task_dir / ADAPTER_RESULT).read_text())
    assert result["status"] == "failed"
    assert result["containment"]["passed"] is False
    assert any("worker containment unavailable" in item for item in result["errors"])
    # No harness was started: the workspace candidate is untouched and no envelope exists.
    assert (env["workspace"] / "math_ops.py").read_text() != FIXED_CANDIDATE
    assert not (task_dir / "harness.stdout").exists()


def test_adapter_refuses_when_the_boundary_profile_changed_since_preparation(tmp_path):
    env, _prepared_plan, task_dir = _prepared(tmp_path)
    record = json.loads((task_dir / "boundary.json").read_text())
    record["profile_digest"] = "0" * 64
    (task_dir / "boundary.json").write_text(json.dumps(record))
    assert run_adapter(task_dir, env["workspace"]) == 1
    result = json.loads((task_dir / ADAPTER_RESULT).read_text())
    assert result["status"] == "failed"
    assert any("profile changed between controller and adapter" in item for item in result["errors"])
    assert (env["workspace"] / "math_ops.py").read_text() != FIXED_CANDIDATE


def test_unstartable_harness_yields_no_invented_result_and_keeps_raw_output(tmp_path):
    """A harness that never runs must not be reported as a completion or given fake output."""
    env, _plan, task_dir = _prepared(tmp_path)
    plan = json.loads((task_dir / "launch-plan.json").read_text())
    plan["binary"] = str(tmp_path / "absent-harness")
    plan["argv"] = [str(tmp_path / "absent-harness"), "--prompt", "{prompt_file}"]
    (task_dir / "launch-plan.json").write_text(json.dumps(plan))
    assert run_adapter(task_dir, env["workspace"]) == 1
    result = json.loads((task_dir / ADAPTER_RESULT).read_text())
    assert result["status"] == "failed" and result["structured"] is None
    assert result["identity_observed"] == {} and result["usage_events"] == []
    # The failure is evidenced by the captured child output, not asserted from a label.
    assert any("no parsable envelope" in item for item in result["errors"])
    assert result["detail"]["raw_stdout_preserved"] is True
    assert result["detail"]["raw_stderr_preserved"] is True
    assert result["detail"]["exit_code"] != 0
    assert result["detail"]["stderr_tail"].strip()
    assert (task_dir / "harness.stderr").read_text().strip()
    assert (env["workspace"] / "math_ops.py").read_text() != FIXED_CANDIDATE


def test_adapter_refuses_an_objective_it_did_not_receive(tmp_path):
    env, _plan, task_dir = _prepared(tmp_path)
    context = json.loads((task_dir / "context.json").read_text())
    context["objective"] = "   "
    (task_dir / "context.json").write_text(json.dumps(context))
    assert run_adapter(task_dir, env["workspace"]) == 1
    result = json.loads((task_dir / ADAPTER_RESULT).read_text())
    assert any("requires an explicit objective" in item for item in result["errors"])


def test_adapter_refuses_a_workspace_that_disagrees_with_the_boundary(tmp_path):
    env, _plan, task_dir = _prepared(tmp_path)
    other = tmp_path / "other-workspace"
    other.mkdir()
    with pytest.raises(PermissionError, match="does not match the declared worker boundary"):
        run_adapter(task_dir, other)
    assert env["workspace"].is_dir()


def test_adapter_launches_harness_with_devnull_stdin(tmp_path, monkeypatch):
    env, _plan, task_dir = _prepared(tmp_path)
    actual = adapter.subprocess.Popen
    seen = {}

    def checked_launch(*args, **kwargs):
        seen["stdin"] = kwargs.get("stdin")
        return actual(*args, **kwargs)

    monkeypatch.setattr(adapter.subprocess, "Popen", checked_launch)
    assert run_adapter(task_dir, env["workspace"]) == 0
    assert seen["stdin"] is subprocess.DEVNULL


def test_adapter_refuses_a_route_argv_with_an_unresolved_placeholder(tmp_path):
    with pytest.raises(PermissionError, match="retains an unresolved placeholder"):
        _substitute(["--model", "{unsupported}"], {"model": "fixture-model"})
    assert _substitute(["--model", "{model}"], {"model": "fixture-model"}) == \
        ["--model", "fixture-model"]


# --------------------------------------------------------------------------- harness env

def test_harness_env_forwards_only_declared_names_and_never_the_ambient_environment(tmp_path):
    boundary = containment.Boundary(workspace=str(tmp_path), scratch=str(tmp_path / "scratch"),
                                    tmpdir=str(tmp_path / "scratch"), deny=())
    plan = {"route": {"runtime_env": ["CORRAL_FIXTURE=1", "CORRAL_MODE=native"]},
            "credential_env": ["CORRAL_ABSENT_CREDENTIAL"]}
    env, forwarded, missing = _harness_env(plan, boundary, str(tmp_path / "home"),
                                           tmp_path / "scratch")
    assert env["HOME"] == str(tmp_path / "home")
    assert env["TMPDIR"] == str(tmp_path / "scratch")
    assert env["CORRAL_FIXTURE"] == "1" and env["CORRAL_MODE"] == "native"
    assert forwarded == [] and missing == ["CORRAL_ABSENT_CREDENTIAL"]
    allowed = set(BASE_ENV_NAMES) | {"TMPDIR", "PYTHONDONTWRITEBYTECODE", "HOME",
                                     "CORRAL_FIXTURE", "CORRAL_MODE"}
    assert set(env) <= allowed
    # Ambient interpreter configuration is never handed to the harness.
    assert "PYTHONPATH" not in env and "PYTHONSTARTUP" not in env


def test_harness_env_records_credential_names_not_values(tmp_path, monkeypatch):
    monkeypatch.setenv("CORRAL_TEST_CREDENTIAL", "synthetic-secret-value")
    boundary = containment.Boundary(workspace=str(tmp_path), scratch=str(tmp_path),
                                    tmpdir=str(tmp_path), deny=())
    env, forwarded, missing = _harness_env({"credential_env": ["CORRAL_TEST_CREDENTIAL"]},
                                           boundary, None, tmp_path)
    assert forwarded == ["CORRAL_TEST_CREDENTIAL"] and missing == []
    assert env["CORRAL_TEST_CREDENTIAL"] == "synthetic-secret-value"
    assert "HOME" not in env


# --------------------------------------------------------------------------- usage semantics

def test_missing_usage_is_a_warning_that_preserves_a_good_candidate(tmp_path):
    """Telemetry absence must never rewrite a real code change into a failure or a zero."""
    env, _plan, task_dir = _prepared(
        tmp_path, ops=[op for op in SUCCESS_OPS if not op.startswith("usage")])
    assert run_adapter(task_dir, env["workspace"]) == 0
    result = json.loads((task_dir / ADAPTER_RESULT).read_text())
    assert result["status"] == "completed" and result["structured"] == {"answer": 5}
    assert result["usage_events"] == []
    assert any("usage stays unknown, not zero" in item for item in result["warnings"])
    assert not result["errors"]
    # Absence is persisted as an explicit empty record, not as zeroed counters.
    assert json.loads((task_dir / "native-usage.json").read_text()) == []
    assert (env["workspace"] / "math_ops.py").read_text() == FIXED_CANDIDATE


def test_malformed_usage_counters_are_dropped_without_failing_the_candidate(tmp_path):
    env, _plan, task_dir = _prepared(
        tmp_path, ops=SUCCESS_OPS[:-1] + [f"usage {json.dumps({'input_tokens': 'many', 'output_tokens': -5, 'bogus_metric': 7})}"])
    assert run_adapter(task_dir, env["workspace"]) == 0
    result = json.loads((task_dir / ADAPTER_RESULT).read_text())
    assert result["status"] == "completed"
    # Invalid counters are dropped whole: no zero-fill, no coercion, no undeclared metric.
    assert result["usage_events"] == []
    assert not result["errors"]
    assert any("usage stays unknown" in item for item in result["warnings"])
    assert json.loads((task_dir / "native-usage.json").read_text()) == []
    # The good candidate survives the malformed telemetry.
    assert result["structured"] == {"answer": 5}
    assert (env["workspace"] / "math_ops.py").read_text() == FIXED_CANDIDATE


def test_reported_usage_keeps_cumulative_semantics_and_synthetic_origin(tmp_path):
    env, _plan, task_dir = _prepared(tmp_path)
    assert run_adapter(task_dir, env["workspace"]) == 0
    events = json.loads((task_dir / "native-usage.json").read_text())
    assert len(events) == 1
    assert events[0]["mode"] == "cumulative" and events[0]["sequence"] == 1
    assert events[0]["origin"] == "synthetic-fixture"
    assert events[0]["counters"] == {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14}
    assert events[0]["failure_path"] is False
    assert events[0]["invocation"] == "attempt-1"
    assert env["workspace"].is_dir()


def test_write_native_usage_appends_and_survives_a_corrupt_existing_record(tmp_path):
    target = tmp_path / "native-usage.json"
    first = [{"id": "a", "invocation": "i", "counters": {"input_tokens": 1}}]
    write_native_usage(target, first)
    write_native_usage(target, [{"id": "b", "invocation": "i", "counters": {"input_tokens": 2}}])
    assert [item["id"] for item in json.loads(target.read_text())] == ["a", "b"]
    target.write_text("{not json")
    write_native_usage(target, first)
    assert json.loads(target.read_text()) == first


def test_failed_attempt_is_retained_in_the_attempt_history(tmp_path):
    env, _plan, task_dir = _prepared(tmp_path, ops=SUCCESS_OPS + ["status failed"])
    assert run_adapter(task_dir, env["workspace"]) == 1
    attempts = [json.loads(line) for line in
                (task_dir / "attempts.jsonl").read_text().splitlines() if line.strip()]
    assert len(attempts) == 1
    assert attempts[0]["status"] == "failed" and attempts[0]["attempt"] == "attempt-1"
    assert attempts[0]["synthetic"] is True
    assert json.loads((task_dir / ADAPTER_RESULT).read_text())["status"] == "failed"
    assert env["workspace"].is_dir()
