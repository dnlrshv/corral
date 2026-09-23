"""An artifact handoff never runs the consumer checkout's hooks or configured programs."""
from __future__ import annotations

import stat
import subprocess
import sys
from pathlib import Path

from corral.execution.controller import Controller
from corral.execution.demo import fixture_profiles
from corral.execution.handoff import BINDING_IDENTITY, execute_handoff

from .test_execution_wave import make_repo

HOOKS = ("pre-commit", "prepare-commit-msg", "commit-msg", "post-commit", "pre-auto-gc",
         "reference-transaction", "post-index-change")


def _probe(path: Path, marker: Path, name: str, *, passthrough: bool = False) -> Path:
    path.write_text(f"#!/bin/sh\necho {name} >> '{marker}'\n" + ("cat\n" if passthrough else "exit 0\n"))
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _plant(repo: Path, marker: Path, probes: Path) -> None:
    """Everything a worker with write access to the checkout's .git could plant."""
    for name in HOOKS:
        _probe(repo / ".git" / "hooks" / name, marker, "hook:" + name)
    hooks_dir = probes / "hooks"
    hooks_dir.mkdir(parents=True)
    for name in HOOKS:
        _probe(hooks_dir / name, marker, "hooks-path:" + name)
    fsmonitor = _probe(probes / "fsmonitor", marker, "fsmonitor")
    clean = _probe(probes / "clean", marker, "clean-filter", passthrough=True)
    signer = _probe(probes / "gpg", marker, "gpg-program")
    included = probes / "included.gitconfig"
    included.write_text(f"[core]\n\thooksPath = {hooks_dir}\n")
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "attributes").write_text("* filter=probe\n")
    for key, value in (("core.fsmonitor", str(fsmonitor)), ("filter.probe.clean", str(clean)),
                       ("filter.probe.required", "true"), ("commit.gpgSign", "true"),
                       ("gpg.program", str(signer)), ("include.path", str(included)),
                       ("user.name", "Planted"), ("user.email", "planted@example.invalid")):
        subprocess.run(["git", "config", key, value], cwd=repo, check=True)


def _git(repo: Path, *args: str) -> str:
    """Read the result back without letting the planted configuration run either."""
    return subprocess.run(["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
                           *args], cwd=repo, check=True, capture_output=True, text=True).stdout


def test_handoff_binds_without_running_consumer_hooks_or_programs(tmp_path):
    producer_repo = make_repo(tmp_path / "producer")
    consumer_repo = make_repo(tmp_path / "consumer")
    controller = Controller(
        tmp_path / "state", "owner",
        {"fixture": {"routes": ["fixture"], "harnesses": ["synthetic"], "cpu": 4, "memory_mb": 1024}},
        default_host="fixture", profiles=fixture_profiles())
    producer = controller.submit("owner", "producer", {
        "repo": "producer", "workspace": str(producer_repo),
        "candidate_paths": ["lib.py", "result.json"],
        "command": [sys.executable, "-c", "open('lib.py','w').write('VALUE = 1\\n');"
                    "open('result.json','w').write('{\"ok\":true}')"],
        "verify": [sys.executable, "-c", "import lib; assert lib.VALUE == 1"]})
    assert controller.run("owner", producer, execution_host="fixture")["result"]["accepted"]
    consumer = controller.submit("owner", "consumer", {
        "repo": "consumer", "workspace": str(consumer_repo), "candidate_paths": ["result.json"],
        "command": [sys.executable, "-c", "open('result.json','w').write('{}')"],
        "verify": [sys.executable, "-c", "pass"]})
    parent = _git(consumer_repo, "rev-parse", "HEAD").strip()

    marker = tmp_path / "planted-programs-ran"
    _plant(consumer_repo, marker, tmp_path / "planted")
    record = execute_handoff(controller, "owner", {
        "producer": producer, "producer_path": "lib.py",
        "consumer": consumer, "consumer_path": "lib.py"})

    assert not marker.exists(), marker.read_text()
    assert record["status"] == "bound"
    head = _git(consumer_repo, "rev-parse", "HEAD").strip()
    assert record["consumer_base"] == head != parent
    assert _git(consumer_repo, "rev-parse", "HEAD^").strip() == parent
    # The commit holds exactly the verified bytes, under Corral's identity, unsigned.
    assert _git(consumer_repo, "show", "HEAD:lib.py") == "VALUE = 1\n"
    assert _git(consumer_repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD") == "lib.py\n"
    assert _git(consumer_repo, "log", "-1", "--format=%an <%ae>|%cn <%ce>|%G?").strip() == (
        "{name} <{email}>|{name} <{email}>|N".format(**BINDING_IDENTITY))
    # The index and HEAD agree afterwards, so a later handoff sees no staged changes.
    assert _git(consumer_repo, "diff-index", "--cached", "--name-only", "HEAD") == ""
    assert (consumer_repo / "lib.py").read_text() == "VALUE = 1\n"
    assert not marker.exists()
