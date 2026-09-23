"""The controller's provider secret file stays out of reach of workers and verifiers."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from corral.execution import containment
from corral.execution.controller import Controller
from corral.execution.store import Store

from . import native_support as ns

pytestmark = pytest.mark.skipif(containment.sandbox_exec() is None,
                                reason="native containment needs macOS sandbox-exec")


def test_worker_and_verifier_cannot_read_the_configured_secret_env_file(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=f@example.invalid",
                    "commit", "--allow-empty", "-qm", "base"], cwd=workspace, check=True)
    (workspace / "result.py").write_text("value = 'before'\n")
    home = tmp_path / "native-home"
    (home / "auth").mkdir(parents=True)
    (home / "auth" / "fixture.json").write_text("fixture runtime state\n")
    # The provider secret file sits in a config directory outside the controller state,
    # under a name that no secret-file pattern matches.
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    secrets = config_dir / "provider-secrets.json"
    secrets.write_text(json.dumps({"ROUTE_SECRET": "private-fixture",
                                   "OTHER_PROVIDER_KEY": "never-readable"}))
    secrets.chmod(0o600)
    harness = tmp_path / "credential-harness.py"
    harness.write_text(
        "#!/usr/bin/env python3\n"
        "import errno, json, sys\nfrom pathlib import Path\n"
        "args = {sys.argv[i]: sys.argv[i + 1] for i in range(1, len(sys.argv), 2)}\n"
        "try:\n"
        f"    open({str(secrets)!r}).read(); seen = 'allowed'\n"
        "except OSError as e:\n"
        "    seen = errno.errorcode.get(e.errno, str(e.errno))\n"
        "Path(args['--workspace']).joinpath('result.py').write_text(\"value = 'after'\\n\")\n"
        "Path(args['--result']).write_text(json.dumps({'answer': 'ok', 'changed': ['result.py']}))\n"
        "print(json.dumps({'schema': 'corral-synthetic-v1', 'synthetic': True, 'status': 'completed',\n"
        " 'narrative': 'updated fixture', 'identity': {'model': args['--model'], 'effort': args['--effort'],\n"
        " 'harness': 'credential-harness.py', 'provider': 'fixture', 'account_ref': 'fixture-account'},\n"
        " 'detail': {'secret_file': seen},\n"
        " 'usage': {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2}}))\n")
    harness.chmod(0o755)
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
    # The verifier exits non-zero when it can read the secret file, so an exposed file
    # also shows up as an unaccepted result.
    verify = [sys.executable, "-I", "-c",
              "import sys\n"
              "try:\n"
              f"    open({str(secrets)!r}).read()\n"
              "except PermissionError:\n"
              "    sys.exit(0)\n"
              "sys.exit(1)\n"]
    env = {**os.environ}
    submit = subprocess.run([sys.executable, "-m", "corral.execution.cli", "--config", str(config_path)],
                            input=json.dumps({"action": "submit", "request_id": "secret-env-boundary",
                                              "spec": {"repo": "fixture/repo", "workspace": str(workspace),
                                                       "host": "fixture", "role": "implementation",
                                                       "profile_id": "fixture-coding", "objective": "Update result.",
                                                       "candidate_paths": ["result.py"], "verifier_paths": [],
                                                       "verify": verify,
                                                       "tools": ["read", "edit", "shell", "test"]}}),
                            text=True, capture_output=True, check=True, env=env)
    task = json.loads(submit.stdout)["task"]
    executed = subprocess.run(
        [sys.executable, "-m", "corral.execution.cli", "--config", str(config_path),
         "--execute", task], text=True, capture_output=True, check=False, env=env)
    assert executed.returncode == 0, executed.stderr
    result = Store(tmp_path / "state" / "controller.sqlite").get("result", task)
    adapter = json.loads((Path(result["artifact_directory"]) / "adapter-result.json").read_text())
    assert adapter["detail"]["harness_detail"] == {"secret_file": "EPERM"}
    assert result["accepted"] is True, result


def test_a_sandboxed_deterministic_command_cannot_read_the_configured_secret_env_file(
        tmp_path, monkeypatch):
    # The deterministic boundary re-opens the controller's TMPDIR for the command, and pytest's
    # tmp_path lives inside it, so the command gets its own TMPDIR away from the secret file.
    worker_tmp = tmp_path / "worker-tmp"
    worker_tmp.mkdir()
    monkeypatch.setenv("TMPDIR", str(worker_tmp))
    env = ns.native_env(tmp_path)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    secrets = config_dir / "provider-secrets.json"
    secrets.write_text(json.dumps({"ROUTE_SECRET": "private-fixture"}))
    secrets.chmod(0o600)
    controller = Controller(tmp_path / "state-secret-env", "owner", {ns.FAKE_HOST: env["host"]},
                            default_host=ns.FAKE_HOST, profiles=[env["profile"]],
                            secret_env=str(secrets))
    child = ("import errno, json\n"
             "try:\n"
             f"    open({str(secrets)!r}).read(); seen = 'allowed'\n"
             "except OSError as e:\n"
             "    seen = errno.errorcode.get(e.errno, str(e.errno))\n"
             "open('probe.json', 'w').write(json.dumps({'secret_file': seen}))\n")
    spec = {"repo": "secret-env-deterministic", "workspace": str(env["workspace"]),
            "candidate_paths": ["math_ops.py"], "verifier_paths": [],
            "verify": [sys.executable, "-c", "raise SystemExit(0)"],
            "command": [sys.executable, "-c", child], "use_sandbox": True}
    task = controller.submit("owner", "secret-env-deterministic", spec)
    controller.run("owner", task, execution_host=ns.FAKE_HOST)
    assert controller.store.get("state", task)["containment"] == "seatbelt-worker-boundary"
    assert json.loads((env["workspace"] / "probe.json").read_text()) == {"secret_file": "EPERM"}
    # The configured host declaration is not rewritten for later runs.
    assert str(secrets.resolve()) not in env["host"]["protected_paths"]
