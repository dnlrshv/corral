"""Trusted verification: binding, isolated import, sabotage refusal and digest retention.

A declared ``external_verifier`` flag is a claim, not trust. These tests drive the real
controller dispatch path with real verifier subprocesses; nothing is monkeypatched, and a
worker that tries to hijack or rewrite the verifier is refused rather than believed.
"""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

from corral.execution import verifier
from corral.execution.controller import Controller

GOOD = '{"ok": true}\n'
BAD = '{"ok": false}\n'

CHECK_CANDIDATE = '''"""Controller-owned external verifier; never imports from the worker cwd."""
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text())
sys.exit(0 if payload.get("ok") is True else 1)
'''


@pytest.fixture
def env(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=f@example.invalid",
                    "commit", "--allow-empty", "-qm", "fixture"], cwd=repo, check=True)
    (repo / "result.json").write_text(BAD)
    roots = tmp_path / "verifier-roots"
    roots.mkdir()
    (roots / "check_candidate.py").write_text(CHECK_CANDIDATE)
    host = {"routes": ["fixture"], "harnesses": ["synthetic"], "cpu": 4, "memory_mb": 1024,
            "verifier_roots": [str(roots)]}
    controller = Controller(tmp_path / "state", "owner", {"fixture": host},
                            default_host="fixture", profiles=[])
    return {"controller": controller, "repo": repo, "roots": roots, "host": host,
            "state": tmp_path / "state", "tmp": tmp_path}


def _spec(env, *, worker=None, verify=None, external=True, **overrides):
    spec = {"repo": "verifier", "workspace": str(env["repo"]), "candidate_paths": ["result.json"],
            "verifier_paths": [],
            "verify": verify or [sys.executable, str(env["roots"] / "check_candidate.py"),
                                 "result.json"],
            "external_verifier": external}
    if worker is not None:
        spec["command"] = [sys.executable, "-c", worker]
    spec.update(overrides)
    return spec


def _run(env, spec, request_id="verifier-run"):
    controller = env["controller"]
    task = controller.submit("owner", request_id, spec)
    return task, controller.run("owner", task, execution_host="fixture")


# --------------------------------------------------------------------------- binding + trust

def test_external_verifier_bound_to_a_controller_root_judges_the_candidate(env):
    worker = "from pathlib import Path; Path('result.json').write_text(%r)" % GOOD
    task, run = _run(env, _spec(env, worker=worker))
    receipt = run["result"]["receipt"]
    assert run["result"]["accepted"] is True
    assert receipt["policy"]["kind"] == "external"
    assert receipt["policy_ok"] is True and receipt["exit_code"] == 0
    assert receipt["policy"]["verifier_root"] == str(env["roots"])
    # The bound bundle is digested, so a later swap is detectable.
    assert receipt["verifier_bundle"]["check_candidate.py"]
    assert receipt["policy"]["deps_binding"].startswith("declared-paths+deception-scan")
    # Isolated import is asserted for controller-owned verifiers.
    assert receipt["import_isolation"] == "PYTHONSAFEPATH; controller verifier root only; no cwd import"
    # Candidate digests are retained on both sides of verification.
    assert receipt["candidate_pre"] == receipt["candidate_post"] and receipt["unchanged"] is True
    assert receipt["verifier_intact"] is True
    assert env["controller"].store.ownership("workspace:" + str(env["repo"].resolve()))[2] == "released"
    assert task


def test_a_failing_external_verifier_is_not_accepted(env):
    worker = "from pathlib import Path; Path('result.json').write_text(%r)" % BAD
    _task, run = _run(env, _spec(env, worker=worker))
    receipt = run["result"]["receipt"]
    assert run["result"]["accepted"] is False
    assert receipt["exit_code"] == 1 and receipt["policy_ok"] is True
    assert receipt["unchanged"] is True


def test_bare_external_verifier_flag_without_a_controller_root_is_refused(env):
    """The flag alone is not trust: no host-declared verifier root means no dispatch."""
    host = dict(env["host"])
    host.pop("verifier_roots")
    env["controller"].hosts["fixture"] = host
    with pytest.raises(PermissionError, match="the flag alone is not trust"):
        _run(env, _spec(env, worker="pass"), request_id="bare-flag")
    # Nothing was launched, so ownership is released rather than stranded as active.
    owner, _epoch, status = env["controller"].store.ownership(
        "workspace:" + str(env["repo"].resolve()))
    assert status == "released" and owner
    state = env["controller"].store.records("state")
    assert list(state.values())[0]["status"] == "refused-before-launch"
    assert list(state.values())[0]["error"] == "PermissionError"


def test_absolute_verifier_script_outside_every_declared_root_is_refused(env):
    stray = env["tmp"] / "stray-verifier.py"
    stray.write_text(CHECK_CANDIDATE)
    with pytest.raises(PermissionError, match="not under a controller-declared verifier root"):
        _run(env, _spec(env, verify=[sys.executable, str(stray), "result.json"]),
             request_id="stray-root")


def test_verifier_executable_inside_worker_reach_is_refused(env):
    inside = env["repo"] / "runner.py"
    inside.write_text(CHECK_CANDIDATE)
    with pytest.raises(PermissionError, match="must not live in a worker-writable path"):
        _run(env, _spec(env, verify=[str(inside), str(env["roots"] / "check_candidate.py"),
                                     "result.json"]), request_id="worker-reach")


def test_relative_verifier_executable_is_refused(env):
    with pytest.raises(PermissionError, match="must be an absolute path outside worker reach"):
        _run(env, _spec(env, verify=["python3", str(env["roots"] / "check_candidate.py"),
                                     "result.json"]), request_id="relative-exe")


def test_missing_verification_command_is_refused(env):
    spec = _spec(env, worker="pass")
    del spec["verify"]
    with pytest.raises(PermissionError, match="declares no verification command"):
        _run(env, spec, request_id="no-verify")


def test_non_string_verification_command_is_refused(env):
    with pytest.raises(PermissionError, match="must be a list of strings"):
        verifier.policy(_spec(env, verify=[sys.executable, 7]), env["repo"], host=env["host"],
                        worker_writable=(str(env["repo"]),))


# --------------------------------------------------------------------------- sabotage

def test_worker_planted_hijack_file_refuses_verification_even_with_exit_zero(env):
    """A workspace that plants sitecustomize.py cannot have its verifier trusted."""
    worker = ("from pathlib import Path\n"
              "Path('sitecustomize.py').write_text('import sys\\n')\n"
              "Path('result.json').write_text(%r)\n" % GOOD)
    _task, run = _run(env, _spec(env, worker=worker), request_id="planted-hijack")
    receipt = run["result"]["receipt"]
    assert run["result"]["accepted"] is False
    assert receipt["policy_ok"] is False
    assert "verifier-hijack" in receipt["refused"]
    assert receipt["deception_scan"]["offenders"] == ["sitecustomize.py"]
    # The verifier was never executed, so no exit code is claimed.
    assert receipt["exit_code"] is None


def test_declared_hijack_shaped_file_is_scanned_and_still_write_denied(env):
    """Declaring the file makes it a bound-workspace verifier, not a silent hijack."""
    (env["repo"] / "conftest.py").write_text("COLLECT_ONLY = True\n")
    policy = verifier.policy(_spec(env, verify=[sys.executable, "conftest.py", "result.json"],
                                   external=False, verifier_paths=["conftest.py"]),
                             env["repo"], host=env["host"],
                             worker_writable=(str(env["repo"]),))
    assert policy.kind == "bound-workspace"
    assert policy.verifier_paths == ("conftest.py",)


def test_undeclared_workspace_verifier_is_refused(env):
    (env["repo"] / "check.py").write_text("import sys; sys.exit(0)\n")
    with pytest.raises(PermissionError, match="not declared in verifier_paths"):
        verifier.policy(_spec(env, verify=[sys.executable, "check.py", "result.json"],
                              external=False), env["repo"], host=env["host"],
                        worker_writable=(str(env["repo"]),))


def test_external_verifier_does_not_import_from_the_worker_cwd(env):
    """A poison module in the workspace must not shadow the verifier's own imports."""
    (env["repo"] / "json.py").write_text("raise RuntimeError('worker shadowed the stdlib')\n")
    probe = env["roots"] / "probe_imports.py"
    probe.write_text("import json\nimport sys\nfrom pathlib import Path\n"
                     "Path('import-probe.json').write_text(json.dumps({'file': json.__file__,"
                     " 'path': sys.path}))\n")
    worker = "from pathlib import Path; Path('result.json').write_text(%r)" % GOOD
    spec = _spec(env, worker=worker,
                 verify=[sys.executable, str(probe), "result.json"])
    _task, run = _run(env, spec, request_id="import-isolation")
    observed = json.loads((env["repo"] / "import-probe.json").read_text())
    assert run["result"]["receipt"]["exit_code"] == 0
    assert str(env["repo"]) not in observed["file"]
    assert "python3" in observed["file"] and observed["file"].endswith("json/__init__.py")
    assert not [entry for entry in observed["path"] if entry == str(env["repo"].resolve())]
    (env["repo"] / "json.py").unlink()


def test_verifier_bundle_change_during_execution_is_detected(env):
    """The pre/post manifest guard that replaced a literal ``pass``."""
    policy = verifier.policy(_spec(env), env["repo"], host=env["host"],
                             worker_writable=(str(env["repo"]),))
    assert policy.kind == "external"
    script = env["roots"] / "check_candidate.py"
    original = script.read_text()
    # This is the exact call the controller makes before dispatch, so the external root is
    # really bound: a pre-change to a manifest of workspace-relative paths would be vacuous.
    pre_verifier_manifest = verifier.bound_bundle(policy, env["repo"])
    assert pre_verifier_manifest == verifier.bundle_digests("external", env["repo"], (),
                                                            str(env["roots"]))
    assert pre_verifier_manifest["check_candidate.py"]
    script.write_text(original + "\n# swapped after the pre-verification manifest\n")
    record = verifier.execute(policy, env["repo"], candidate_paths=["result.json"], task="t",
                              attempt="a", pre_verifier_manifest=pre_verifier_manifest)
    assert record.payload["verifier_intact"] is False
    assert record.payload["policy_ok"] is False
    assert "changed during execution" in record.payload["refused"]
    assert record.payload["exit_code"] is None
    script.write_text(original)
    # Restored, the same policy verifies again and reports the bundle intact.
    restored = verifier.execute(policy, env["repo"], candidate_paths=["result.json"], task="t",
                                attempt="a", pre_verifier_manifest=pre_verifier_manifest)
    assert restored.payload["verifier_intact"] is True
    assert restored.payload["exit_code"] == 1  # the fixture candidate is still the failing one


def test_verifier_that_rewrites_the_candidate_is_not_accepted(env):
    """Candidate digests are retained on both sides, so a mutating verifier is caught."""
    mutating = env["roots"] / "mutating.py"
    mutating.write_text("from pathlib import Path\n"
                        "Path('result.json').write_text('{\"ok\": true}\\n')\n")
    _task, run = _run(env, _spec(env, worker="pass",
                                 verify=[sys.executable, str(mutating), "result.json"]),
                      request_id="mutating-verifier")
    receipt = run["result"]["receipt"]
    assert run["result"]["accepted"] is False
    assert receipt["exit_code"] == 0
    assert receipt["unchanged"] is False
    assert receipt["candidate_pre"] != receipt["candidate_post"]


def test_bundle_digests_enumerate_the_whole_external_root(env):
    (env["roots"] / "helpers").mkdir()
    (env["roots"] / "helpers" / "util.py").write_text("VALUE = 1\n")
    bundle = verifier.bundle_digests("external", env["repo"], (), str(env["roots"]))
    assert set(bundle) == {"check_candidate.py", "helpers/util.py"}
    assert all(value and len(value) == 64 for value in bundle.values())
    assert verifier.bundle_digests("inline", env["repo"], (), None) == {}
