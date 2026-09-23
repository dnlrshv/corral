"""A route binary runs by its declared path; everything it resolves through is validated."""
from __future__ import annotations

import os
import subprocess
import sysconfig
import venv
from pathlib import Path

import pytest

import corral
from corral.execution import routes

from . import native_support as ns

PROBE = "corral_route_venv_probe"


def _route(binary: Path | str, **extra) -> routes.NativeRoute:
    return routes.declare(ns.FAKE_ROUTE, {
        "harness": ns.FAKE_HARNESS, "binary": str(binary), "envelope": "corral-synthetic-v1",
        "argv": ["--model", "{model}", "--effort", "{effort}"], "provider": "fixture",
        "account_ref": "fixture-account", "supported_models": [ns.FAKE_MODEL],
        "supported_efforts": [ns.FAKE_EFFORT], "synthetic": True, **extra})


def _plan(binary: Path | str, forbidden_roots=()) -> routes.LaunchPlan:
    return routes.plan(_route(binary), ns.fake_profile(), host_routes=(ns.FAKE_ROUTE,),
                       forbidden_roots=tuple(str(item) for item in forbidden_roots))


def _real_venv(root: Path) -> Path:
    """A real virtual environment whose interpreter is a symlink to its base interpreter.

    The environment sees Corral through a ``.pth`` entry and also holds a module that only
    it can import, so running the base interpreter instead is observable on any host, even
    one whose base interpreter has Corral installed.
    """
    venv.EnvBuilder(with_pip=False, symlinks=True).create(root)
    python = root / "bin" / "python"
    purelib = subprocess.run(
        [str(python), "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
        capture_output=True, text=True, check=True).stdout.strip()
    site = Path(purelib)
    (site / "corral-source.pth").write_text(str(Path(corral.__file__).resolve().parents[1]) + "\n")
    (site / f"{PROBE}.py").write_text("INSIDE_ENVIRONMENT = True\n")
    return python


def _run(binary: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([binary, *args], capture_output=True, text=True, cwd=os.sep,
                          env={"PATH": os.environ.get("PATH", "")}, timeout=120)


@pytest.mark.skipif(sysconfig.get_platform().startswith("win"), reason="POSIX venv layout")
def test_symlinked_venv_interpreter_runs_by_its_declared_path(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "host-runtime" / "venv"
    python = _real_venv(root)
    assert python.is_symlink(), "the regression needs a venv interpreter that is a symlink"
    real = os.path.realpath(python)
    assert Path(real).parent != python.parent

    plan = _plan(python, forbidden_roots=(workspace,))
    # The declared interpreter is executed; its target is validated and recorded for audit.
    assert plan.binary == str(python)
    assert plan.binary_realpath == real == plan.evidence["binary_realpath"]
    assert plan.as_dict()["binary"] == str(python)
    assert str(python) in plan.evidence["binary_symlinks"]

    # The live inspection route's exact invocation resolves its module inside the venv.
    started = _run(plan.binary, "-I", "-m", "corral.execution.inspection_transport", "--help")
    assert started.returncode == 0, started.stderr
    inside = _run(plan.binary, "-I", "-c",
                  f"import sys, corral.execution.inspection_transport, {PROBE}; print(sys.prefix)")
    assert inside.returncode == 0, inside.stderr
    assert Path(inside.stdout.strip()).resolve() == root.resolve()

    # What used to be executed, the resolved base interpreter, is outside the environment.
    outside = _run(plan.binary_realpath, "-I", "-c", f"import {PROBE}")
    assert outside.returncode != 0 and "ModuleNotFoundError" in outside.stderr


def test_symlink_inside_worker_reach_is_refused_even_when_its_target_is_trusted(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    trusted = tmp_path / "host-bin" / "harness"
    trusted.parent.mkdir()
    trusted.write_text("#!/bin/sh\n")
    link = workspace / "harness"
    link.symlink_to(trusted)
    # The worker could retarget this link between validation and launch.
    with pytest.raises(PermissionError, match="must not live in a worker-writable path"):
        _plan(link, forbidden_roots=(workspace,))
    # The same target declared by its own trusted path is accepted.
    assert _plan(trusted, forbidden_roots=(workspace,)).binary == str(trusted)


def test_intermediate_symlink_hop_inside_worker_reach_is_refused(tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / "bin").mkdir(parents=True)
    trusted = tmp_path / "host-bin" / "harness"
    trusted.parent.mkdir()
    trusted.write_text("#!/bin/sh\n")
    # host-runtime/current -> workspace/bin, and workspace/bin/harness -> the trusted file:
    # the final file is trusted, but a hop on the way is worker-writable.
    (workspace / "bin" / "harness").symlink_to(trusted)
    runtime = tmp_path / "host-runtime"
    runtime.mkdir()
    (runtime / "current").symlink_to(workspace / "bin")
    declared = runtime / "current" / "harness"
    assert os.path.realpath(declared) == str(trusted.resolve())
    with pytest.raises(PermissionError, match="must not live in a worker-writable path"):
        _plan(declared, forbidden_roots=(workspace,))


def test_directory_left_through_dotdot_inside_worker_reach_is_refused(tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / "real-dir").mkdir(parents=True)
    trusted = tmp_path / "host-bin" / "harness"
    trusted.parent.mkdir()
    trusted.write_text("#!/bin/sh\n")
    # Resolves to the trusted file today, but the worker could replace workspace/real-dir
    # with a symlink and change where the later ``..`` leads.
    declared = Path(str(workspace / "real-dir") + "/../../host-bin/harness")
    assert os.path.realpath(declared) == str(trusted.resolve())
    with pytest.raises(PermissionError, match="must not live in a worker-writable path"):
        _plan(declared, forbidden_roots=(workspace,))


def test_symlink_loop_is_refused(tmp_path):
    loop = tmp_path / "loop"
    loop.symlink_to(loop)
    with pytest.raises(PermissionError):
        _plan(loop)


def test_route_runtime_write_grants_are_worker_writable_for_the_binary(tmp_path):
    env = ns.native_env(tmp_path)
    writable = env["fake_home"] / "runtime"
    writable.mkdir()
    binary = writable / "harness"
    binary.write_text(ns.FAKE_NATIVE_CLI)
    binary.chmod(0o755)
    route = env["host"]["native_routes"][ns.FAKE_ROUTE]
    route.update(binary=str(binary), runtime_write=[str(writable)])
    controller = env["controller"]
    task = controller.submit("owner", "binary-in-write-grant", ns.native_spec(env, ops=[]))
    with pytest.raises(PermissionError, match="must not live in a worker-writable path"):
        controller.run("owner", task, execution_host=ns.FAKE_HOST)
    assert not (controller.artifacts / task / "harness.stdout").exists()
