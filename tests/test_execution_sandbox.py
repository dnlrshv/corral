"""Ephemeral worker containment: profile rendering, real probe, fail-closed refusals.

Nothing here monkeypatches the sandbox. The denial tests launch real children under
``sandbox-exec`` and assert the kernel errno they observed, and the "boundary unavailable"
tests run the real module in a child process whose ``PATH`` cannot find ``sandbox-exec``.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from corral.execution import containment
from corral.execution.containment import Boundary, build_profile, refuse_overlaps
from corral.execution.process import Process, boundary_for

REPO_ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(containment.sandbox_exec() is None,
                                reason="real worker containment requires macOS sandbox-exec")


def _boundary(tmp_path: Path, *, deny=(), allow=(), deny_write=(), sentinels=()) -> Boundary:
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "state" / "scratch" / "task-1"
    for item in (workspace, scratch):
        item.mkdir(parents=True, exist_ok=True)
    return Boundary(workspace=str(workspace), scratch=str(scratch), tmpdir=str(scratch),
                    deny=tuple(deny), allow=tuple(allow), deny_write=tuple(deny_write),
                    sentinels=tuple(sentinels))


def _real_boundary(tmp_path: Path) -> tuple[Boundary, str]:
    """A boundary that denies controller state, artifacts, source and an account store."""
    state = tmp_path / "state"
    (state / "sentinels").mkdir(parents=True, exist_ok=True)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    sentinel = containment.write_sentinel(state / "sentinels" / "task-1.controller-state",
                                          "controller state store")
    boundary = _boundary(tmp_path, deny=[str(state), str(artifacts), str(REPO_ROOT)],
                         sentinels=[sentinel])
    return boundary, sentinel


# --------------------------------------------------------------------------- profile text

def test_profile_is_default_deny_write_and_denies_curated_reads(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    boundary = _boundary(tmp_path, deny=[str(state)], deny_write=[str(tmp_path / "workspace" / "verify.sh")])
    profile = build_profile(boundary)
    assert "(version 1)" in profile and "(deny default)" in profile
    assert "(deny file-write*)" in profile
    denied = str(Path(state).resolve())
    assert f'(deny file-read* file-write* (subpath "{denied}"))' in profile
    assert f'(deny file-read* file-write* (literal "{denied}"))' in profile
    # Worker roots are re-opened for read and write only after the denials above.
    for root in boundary.writable_roots():
        assert f'(allow file-read* file-write* (subpath "{root}"))' in profile
    # An in-workspace verifier denial is emitted last so it wins over the workspace allow.
    verifier = str((tmp_path / "workspace" / "verify.sh").resolve())
    assert profile.rindex(f'(deny file-write* (literal "{verifier}"))') > profile.rindex(
        f'(allow file-read* file-write* (subpath "{boundary.workspace}"))')


def test_profile_normalizes_symlink_aliases_before_rendering(tmp_path):
    real = tmp_path / "real-state"
    real.mkdir()
    alias = tmp_path / "alias-state"
    alias.symlink_to(real)
    profile = build_profile(_boundary(tmp_path, deny=[str(alias)]))
    # Seatbelt matches resolved paths, so the unresolved alias would silently void the rule.
    assert str(Path(alias).resolve()) == str(real.resolve())
    assert f'(deny file-read* file-write* (subpath "{real.resolve()}"))' in profile
    assert f'"{alias}"' not in profile


def test_profile_denies_secret_file_names_but_keeps_templates_readable(tmp_path):
    profile = build_profile(_boundary(tmp_path))
    assert '(deny file-read* file-write* (regex #"^/.*/\\.env$"))' in profile
    assert '(deny file-read* file-write* (regex #"^/.*/[^/]+\\.(pem|key|p12|pfx)$"))' in profile
    assert '(allow file-read* (regex #"^/.*/\\.env\\.(example|sample|template)$"))' in profile


def test_sandboxed_child_cannot_read_a_dot_prefixed_env_file(tmp_path):
    """Named ``*_env`` secret files are denied by pattern, not by a list of project names."""
    boundary = _boundary(tmp_path)
    secret = Path(boundary.workspace) / ".service_env"
    secret.write_text("SERVICE_TOKEN=fixture\n")
    readable = Path(boundary.workspace) / "service.py"
    readable.write_text("VALUE = 1\n")
    child_script = (
        "import errno,json\n"
        "out={}\n"
        "for label,target in (('secret',%r),('source',%r)):\n"
        "    try:\n"
        "        open(target).read(); out[label]='allowed'\n"
        "    except OSError as e: out[label]=errno.errorcode.get(e.errno,str(e.errno))\n"
        "print(json.dumps(out))\n"
    ) % (str(secret.resolve()), str(readable.resolve()))
    stdout = tmp_path / "out"
    stderr = tmp_path / "err"
    with stdout.open("wb") as out, stderr.open("wb") as err:
        child = Process([sys.executable, "-c", child_script], boundary.workspace, out, err,
                        env={"HOME": str(tmp_path)},
                        seatbelt_profile=build_profile(boundary),
                        containment_label="test-boundary")
        assert child.child.wait() == 0, stderr.read_text()
    assert json.loads(stdout.read_text()) == {"secret": "EPERM", "source": "allowed"}


def test_profile_grant_is_read_only_reallow_inside_a_denied_parent(tmp_path):
    denied_home = tmp_path / "harness-home"
    denied_home.mkdir()
    boundary = _boundary(tmp_path, deny=[str(denied_home)], allow=[str(denied_home)])
    profile = build_profile(boundary)
    granted = str(denied_home.resolve())
    assert f'(allow file-read* (subpath "{granted}"))' in profile
    assert f'(allow file-read* (literal "{granted}"))' in profile
    # The grant never re-opens write: the only write allows are the worker roots and /dev.
    assert f'(allow file-read* file-write* (subpath "{granted}"))' not in profile
    assert "(deny file-write*)" in profile


def test_sensitive_account_stores_cover_the_denied_credential_set(tmp_path):
    home = (tmp_path / "home").resolve()
    stores = containment.sensitive_account_stores(home)
    for entry in (".ssh", ".aws", ".gnupg", ".kube", ".docker", ".azure", ".aliyun", ".gemini",
                  ".antigravity", ".qwen", ".npmrc", ".netrc", ".git-credentials",
                  ".codex/auth.json", "Library/Keychains"):
        assert f"{home}/{entry}" in stores, entry
    assert all(item.startswith(f"{home}/") for item in stores)


# --------------------------------------------------------------------------- overlap refusal

def test_refuse_overlaps_rejects_writable_root_inside_denied_state(tmp_path):
    state = tmp_path / "state"
    (state / "scratch").mkdir(parents=True)
    boundary = Boundary(workspace=str(state / "scratch"), scratch=str(state / "scratch"),
                        tmpdir=str(state / "scratch"), deny=(str(state),))
    problems = refuse_overlaps(boundary)
    assert any("reopens a denied subtree" in item for item in problems)


def test_refuse_overlaps_rejects_home_or_root_as_a_writable_root(tmp_path):
    boundary = Boundary(workspace=str(Path.home()), scratch=str(tmp_path), tmpdir=str(tmp_path),
                        deny=(str(tmp_path / "state"),))
    assert any("whole-filesystem path" in item for item in refuse_overlaps(boundary))


def test_refuse_overlaps_rejects_grant_that_is_not_an_exact_denied_root(tmp_path):
    home = tmp_path / "harness-home"
    (home / "auth").mkdir(parents=True)
    boundary = _boundary(tmp_path, deny=[str(home)], allow=[str(home / "auth")])
    problems = refuse_overlaps(boundary)
    assert any("not an exact denied root" in item for item in problems)
    # An exact match is the only accepted shape.
    assert refuse_overlaps(_boundary(tmp_path, deny=[str(home)], allow=[str(home)])) == []


def test_refuse_overlaps_rejects_grant_reopening_a_second_denied_subtree(tmp_path):
    home = tmp_path / "harness-home"
    home.mkdir()
    boundary = _boundary(tmp_path, deny=[str(home), str(home / ".ssh")], allow=[str(home)])
    assert any("grant reopens a denied subtree" in item for item in refuse_overlaps(boundary))


def test_refuse_overlaps_is_symlink_alias_safe(tmp_path):
    real = tmp_path / "real-home"
    real.mkdir()
    alias = tmp_path / "alias-home"
    alias.symlink_to(real)
    boundary = _boundary(tmp_path, deny=[str(real)], allow=[str(alias)])
    # The alias resolves to the denied root, so this is the accepted exact-match shape.
    assert refuse_overlaps(boundary) == []
    wider = tmp_path / "wider"
    wider.symlink_to(tmp_path)
    aliased = _boundary(tmp_path, deny=[str(real)], allow=[str(wider)])
    assert any("not an exact denied root" in item for item in refuse_overlaps(aliased))


# --------------------------------------------------------------------------- real probe

def test_demonstrate_proves_denial_with_kernel_errnos_and_honest_scope(tmp_path):
    boundary, sentinel = _real_boundary(tmp_path)
    receipt = containment.demonstrate(boundary)
    assert receipt["passed"] is True and receipt["blocker"] is None
    assert receipt["available"] is True and Path(receipt["sandbox_exec"]).name == "sandbox-exec"
    denials = [check for check in receipt["checks"] if check["expected"] == "denied"]
    assert denials, "the probe must actually attempt denied operations"
    assert all(check["observed"] == "denied" for check in denials)
    # ENOENT/ENOTDIR are inconclusive: only a permission errno counts as containment.
    assert {check["errno"] for check in denials} <= {"EPERM", "EACCES"}
    allowed = [check for check in receipt["checks"] if check["expected"] == "allowed"]
    assert allowed and all(check["observed"] == "allowed" for check in allowed)
    assert receipt["sentinels"] == [str(Path(sentinel).resolve())]
    # Scope limits are stated rather than implied: a private directory is not OS isolation.
    assert receipt["contained_operations"] == ["file-read*", "file-write*"]
    assert "not full OS isolation" in receipt["isolation_claim"]
    assert any("network egress" in item for item in receipt["not_contained"])
    assert any("same-UID" in item for item in receipt["not_contained"])
    # The sentinel is unchanged, so the probe did not mutate trusted state.
    assert "corral containment sentinel" in Path(sentinel).read_text()
    assert receipt["profile_digest"] == containment.profile_digest(build_profile(boundary))


def test_demonstrate_fails_closed_when_a_sentinel_does_not_pre_exist(tmp_path):
    boundary = _boundary(tmp_path, sentinels=[str(tmp_path / "state" / "absent-sentinel")])
    receipt = containment.demonstrate(boundary)
    assert receipt["passed"] is False and receipt["checks"] == []
    assert "must pre-exist" in receipt["blocker"]
    with pytest.raises(PermissionError, match="could not be demonstrated"):
        containment.require(boundary)


def test_demonstrate_fails_closed_when_a_declared_denial_is_not_enforced(tmp_path):
    # The sentinel sits inside the worker-writable root and nothing denies it, so the probe
    # observes real permissions. A configuration that does not contain must not pass.
    boundary = _boundary(tmp_path)
    loose = containment.write_sentinel(Path(boundary.workspace) / "loose-sentinel", "not denied")
    uncontained = Boundary(workspace=boundary.workspace, scratch=boundary.scratch,
                           tmpdir=boundary.tmpdir, deny=boundary.deny, sentinels=(loose,))
    receipt = containment.demonstrate(uncontained)
    assert receipt["passed"] is False
    assert "unexpected permissions" in receipt["blocker"]
    observed = {check["check"]: check["observed"] for check in receipt["checks"]}
    assert observed[f"read-file:{loose}"] == "allowed"
    assert observed[f"write-file:{loose}"] == "allowed"
    with pytest.raises(PermissionError, match="could not be demonstrated"):
        containment.require(uncontained)


def test_demonstrate_fails_closed_when_sandbox_exec_is_absent(tmp_path):
    """A missing boundary tool is a refusal, never a downgrade to directory privacy."""
    script = (
        "import json,sys;"
        "sys.path.insert(0,%r);"
        "from corral.execution import containment;"
        "b=containment.Boundary(workspace=%r,scratch=%r,tmpdir=%r,deny=());"
        "r=containment.demonstrate(b);"
        "print(json.dumps({'passed':r['passed'],'available':r['available'],'blocker':r['blocker']}));"
        "exec('try:\\n containment.require(b)\\nexcept PermissionError as e:\\n print(e)')"
        % (str(REPO_ROOT), str(tmp_path), str(tmp_path), str(tmp_path))
    )
    completed = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                               env={"PATH": "/nonexistent-corral-path", "HOME": str(tmp_path)})
    assert completed.returncode == 0, completed.stderr
    first = json.loads(completed.stdout.splitlines()[0])
    assert first == {"passed": False, "available": False,
                     "blocker": "sandbox-exec not installed on this host"}
    assert "worker containment could not be demonstrated" in completed.stdout


def test_wrapped_refuses_to_run_an_uncontained_command(tmp_path):
    script = (
        "import sys;sys.path.insert(0,%r);"
        "from corral.execution import containment;"
        "exec('try:\\n containment.wrapped(\\'(version 1)\\',[\\'true\\'])\\n"
        "except EnvironmentError as e:\\n print(\\'REFUSED:\\'+str(e))')" % str(REPO_ROOT)
    )
    completed = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                               env={"PATH": "/nonexistent-corral-path", "HOME": str(tmp_path)})
    assert completed.returncode == 0, completed.stderr
    assert "sandbox-exec is required for worker containment" in completed.stdout


def test_wrapped_prefixes_the_real_sandbox_exec_binary():
    command = containment.wrapped("(version 1)", ["/bin/echo", "ok"])
    assert command[0] == containment.sandbox_exec()
    assert command[1] == "-p" and command[2] == "(version 1)" and command[3:] == ["/bin/echo", "ok"]


# --------------------------------------------------------------------------- boundary_for

def test_boundary_for_denies_task_dir_state_and_account_stores(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    task_dir = tmp_path / "artifacts" / "task-1"
    task_dir.mkdir(parents=True)
    sentinel = containment.write_sentinel(tmp_path / "s" / "sentinel", "label")
    boundary = boundary_for(workspace, protected_paths=[str(tmp_path / "secrets")],
                            verifier_paths=["verify.sh"], task_dir=task_dir,
                            scratch=tmp_path / "scratch", sentinels=[sentinel])
    denied = set(boundary.deny)
    assert str(task_dir.resolve()) in denied
    assert str((tmp_path / "secrets").resolve()) in denied
    assert str(Path.home() / ".ssh") in denied
    assert str(Path.home() / ".qwen") in denied
    assert boundary.deny_write == (str((workspace / "verify.sh").resolve()),)
    assert boundary.sentinels == (str(Path(sentinel).resolve()),)
    assert boundary.scratch == str((tmp_path / "scratch").resolve())


# --------------------------------------------------------------------------- real Process

def test_sandboxed_process_child_observes_denial_and_keeps_workspace_writable(tmp_path):
    """A real child under a real profile: denied state is EPERM, workspace stays writable."""
    boundary, sentinel = _real_boundary(tmp_path)
    (tmp_path / "artifacts" / "receipt.json").write_text('{"trusted": true}')
    child_script = (
        "import errno,json,os\n"
        "out={}\n"
        "for label,target in (('sentinel',%r),('receipt',%r)):\n"
        "    row={}\n"
        "    for kind,mode in (('read','rb'),('write',None)):\n"
        "        try:\n"
        "            if mode: fd=os.open(target,os.O_WRONLY|os.O_APPEND);os.close(fd)\n"
        "            else:\n"
        "                with open(target,'rb') as h: h.read(1)\n"
        "            row[kind]='allowed'\n"
        "        except OSError as e: row[kind]=errno.errorcode.get(e.errno,str(e.errno))\n"
        "    out[label]=row\n"
        "open(os.path.join(%r,'note.txt'),'w').write('worker wrote this')\n"
        "print(json.dumps(out))\n"
    ) % (sentinel, str(tmp_path / "artifacts" / "receipt.json"), boundary.workspace)
    stdout = tmp_path / "out"
    stderr = tmp_path / "err"
    with stdout.open("wb") as out, stderr.open("wb") as err:
        child = Process([sys.executable, "-c", child_script], boundary.workspace, out, err,
                        env={"HOME": str(tmp_path)},
                        seatbelt_profile=build_profile(boundary),
                        containment_label="test-boundary")
        assert child.child.wait() == 0, stderr.read_text()
    observed = json.loads(stdout.read_text())
    assert observed["sentinel"] == {"read": "EPERM", "write": "EPERM"}
    assert observed["receipt"] == {"read": "EPERM", "write": "EPERM"}
    assert (Path(boundary.workspace) / "note.txt").read_text() == "worker wrote this"
    assert (tmp_path / "artifacts" / "receipt.json").read_text() == '{"trusted": true}'
    assert child.containment == "test-boundary"


def test_process_without_a_profile_reports_clean_env_not_containment(tmp_path):
    """Honest labelling: an unsandboxed child must never claim a worker boundary."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    stdout, stderr = tmp_path / "out", tmp_path / "err"
    with stdout.open("wb") as out, stderr.open("wb") as err:
        child = Process([sys.executable, "-c", "print('ok')"], workspace, out, err)
        assert child.child.wait() == 0
    assert child.containment == "standard-clean-env"
    assert stdout.read_text().strip() == "ok"
    assert os.environ.get("CORRAL_NOT_FORWARDED") is None
