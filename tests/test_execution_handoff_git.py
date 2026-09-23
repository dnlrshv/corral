"""An artifact handoff never runs the consumer checkout's hooks or configured programs."""
from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from corral.execution import workspace_contract
from corral.execution.controller import Controller
from corral.execution.demo import fixture_profiles
from corral.execution.handoff import BINDING_IDENTITY, _bind_commit, execute_handoff
from corral.execution.workspace import manifest

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


def _handoff_pair(tmp_path: Path, consumer_repo: Path):
    """An accepted producer of ``lib.py`` and a consumer task bound to ``consumer_repo``."""
    producer_repo = make_repo(tmp_path / "producer")
    controller = Controller(
        tmp_path / "state", "owner",
        {"fixture": {"routes": ["fixture"], "harnesses": ["synthetic"], "cpu": 4, "memory_mb": 1024}},
        default_host="fixture", profiles=fixture_profiles())
    producer = controller.submit("owner", "producer", {
        "repo": "producer", "workspace": str(producer_repo), "candidate_paths": ["lib.py"],
        "command": [sys.executable, "-c", "open('lib.py','w').write('VALUE = 1\\n')"],
        "verify": [sys.executable, "-c", "import lib; assert lib.VALUE == 1"]})
    assert controller.run("owner", producer, execution_host="fixture")["result"]["accepted"]
    consumer = controller.submit("owner", "consumer", {
        "repo": "consumer", "workspace": str(consumer_repo), "candidate_paths": ["result.json"],
        "command": [sys.executable, "-c", "open('result.json','w').write('{}')"],
        "verify": [sys.executable, "-c", "pass"]})
    return controller, {"producer": producer, "producer_path": "lib.py",
                        "consumer": consumer, "consumer_path": "lib.py"}


def _repository_state(repo: Path) -> dict:
    """Refs, index and object files of ``repo``: everything a redirected binding would move."""
    git_dir = Path(_git(repo, "rev-parse", "--absolute-git-dir").strip())
    return {"refs": _git(repo, "for-each-ref", "--format=%(refname) %(objectname)"),
            "head": _git(repo, "rev-parse", "HEAD"),
            "index": (git_dir / "index").read_bytes() if (git_dir / "index").exists() else None,
            "objects": sorted(str(path.relative_to(git_dir)) for path in (git_dir / "objects").rglob("*")
                              if path.is_file())}


def _redirect(consumer: Path, other: Path, kind: str, parked: Path) -> None:
    """Plant what a worker with write access to the consumer checkout could plant."""
    if kind == "gitfile":
        (consumer / ".git").rename(parked)
        (consumer / ".git").write_text(f"gitdir: {other / '.git'}\n")
    elif kind == "relative-gitfile":
        (consumer / ".git").rename(parked)
        (consumer / ".git").write_text("gitdir: " + os.path.relpath(other / ".git", consumer) + "\n")
    elif kind == "symlink":
        (consumer / ".git").rename(parked)
        (consumer / ".git").symlink_to(other / ".git")
    elif kind == "commondir":
        (consumer / ".git" / "commondir").write_text(str(other / ".git") + "\n")
    elif kind == "refs-symlink":
        heads = consumer / ".git" / "refs" / "heads"
        heads.rename(parked)
        heads.symlink_to(other / ".git" / "refs" / "heads")
    else:
        raise AssertionError(kind)


REDIRECTS = ("gitfile", "relative-gitfile", "symlink", "commondir", "refs-symlink")


@pytest.mark.parametrize("kind", REDIRECTS)
def test_handoff_refuses_a_consumer_git_redirect_to_another_repository(tmp_path, kind):
    consumer_repo = make_repo(tmp_path / "consumer")
    other = make_repo(tmp_path / "other")
    controller, handoff = _handoff_pair(tmp_path, consumer_repo)
    before = _repository_state(other)

    _redirect(consumer_repo, other, kind, tmp_path / "parked")
    with pytest.raises(PermissionError, match="redirect|symlink"):
        execute_handoff(controller, "owner", handoff)

    assert _repository_state(other) == before
    assert not (consumer_repo / "lib.py").exists()
    lock = controller.store.ownership("workspace:" + str(consumer_repo.resolve()))
    assert lock is None or lock[2] == "released"
    # The dispatch preflight refuses the same checkout before any worker could start.
    with pytest.raises(PermissionError, match="no readable Git HEAD"):
        workspace_contract.preflight({"candidate_paths": []}, consumer_repo)


@pytest.mark.parametrize("kind", REDIRECTS)
def test_binding_git_refuses_a_redirect_planted_after_the_checkout_was_read(tmp_path, kind):
    """Each binding Git call resolves the repository again, not only the first read.

    ``parent`` is the other repository's head, which is what a redirected ``HEAD`` read
    reports, so without the check every plumbing step would land in that repository.
    """
    consumer_repo = make_repo(tmp_path / "consumer")
    other = make_repo(tmp_path / "other")
    parent = _git(other, "rev-parse", "HEAD").strip()
    before = _repository_state(other)

    _redirect(consumer_repo, other, kind, tmp_path / "parked")
    with pytest.raises(PermissionError, match="redirect|symlink"):
        _bind_commit(consumer_repo.resolve(), "lib.py", b"VALUE = 1\n", 0o644, parent, "bind")
    with pytest.raises(PermissionError, match="redirect|symlink"):
        manifest(consumer_repo, ["lib.py"])
    assert _repository_state(other) == before


def test_handoff_binds_into_a_registered_linked_worktree(tmp_path):
    main = make_repo(tmp_path / "main")
    worktree = tmp_path / "consumer-worktree"
    subprocess.run(["git", "worktree", "add", "-q", "-b", "consumer-branch", str(worktree)],
                   cwd=main, check=True)
    main_head = _git(main, "rev-parse", "HEAD").strip()
    controller, handoff = _handoff_pair(tmp_path, worktree)

    record = execute_handoff(controller, "owner", handoff)

    assert record["status"] == "bound"
    assert _git(main, "rev-parse", "refs/heads/consumer-branch").strip() == record["consumer_base"]
    assert _git(main, "rev-parse", "consumer-branch^").strip() == main_head
    assert _git(main, "show", "consumer-branch:lib.py") == "VALUE = 1\n"
    # The main checkout's own branch and index are untouched.
    assert _git(main, "rev-parse", "HEAD").strip() == main_head
    assert _git(main, "diff-index", "--cached", "--name-only", "HEAD") == ""


def test_handoff_refuses_a_worktree_gitfile_registered_for_another_checkout(tmp_path):
    consumer_repo = make_repo(tmp_path / "consumer")
    other = make_repo(tmp_path / "other")
    subprocess.run(["git", "worktree", "add", "-q", "-b", "other-branch",
                    str(tmp_path / "other-worktree")], cwd=other, check=True)
    controller, handoff = _handoff_pair(tmp_path, consumer_repo)
    before = _repository_state(other)
    (consumer_repo / ".git").rename(tmp_path / "parked")
    (consumer_repo / ".git").write_text(
        f"gitdir: {other / '.git' / 'worktrees' / 'other-worktree'}\n")

    with pytest.raises(PermissionError, match="did not register this checkout"):
        execute_handoff(controller, "owner", handoff)
    assert _repository_state(other) == before
