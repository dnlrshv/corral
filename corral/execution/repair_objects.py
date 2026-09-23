"""Corral-owned Git objects for repair admission and branch publication.

A repair checkout is writable by the worker, so its ``.git`` directory is untrusted: its config,
includes, attributes, replace refs, ``core.worktree`` and fsmonitor settings can all run code or
redirect what Git reads. Nothing here runs Git with that repository's configuration. Commits are
built in a bare repository that Corral owns, from bytes the controller accepted, on top of a head
fetched from the remote, and the checkout is compared with those trusted objects through Corral's
own ``GIT_DIR``. Credentials are attached to fetch and push only.
"""
from __future__ import annotations

import hashlib
import os
import re
import shlex
import subprocess
import tempfile
from pathlib import Path
from typing import Any

SHA = re.compile(r"^[0-9a-f]{40}$")
_ZERO = "0" * 40

#: Wall-clock bound for one fetch of the repair branch.
FETCH_TIMEOUT_SECONDS = 600
#: Wall-clock bound for local object and ref queries.
LOCAL_TIMEOUT_SECONDS = 300

#: Inherited variables that configure the trusted service's own SSH transport. Every other
#: ``GIT_*`` variable could redirect the repository, index, object store or configuration.
_INHERITED_GIT_ENV = frozenset({"GIT_SSH", "GIT_SSH_COMMAND", "GIT_SSH_VARIANT"})

#: Settings that keep hooks, fsmonitor, external diff, user-level attributes and ignores, and
#: automatic maintenance out of every invocation; ``credential.helper=`` clears inherited helpers.
_HARDENING = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
              "-c", "core.untrackedCache=false", "-c", "core.attributesFile=/dev/null",
              "-c", "core.excludesFile=/dev/null", "-c", "diff.external=",
              "-c", "gc.auto=0", "-c", "maintenance.auto=false",
              "-c", "credential.helper=")


def git_environment(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Return the service environment without repository-redirecting Git variables."""
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("GIT_") or key in _INHERITED_GIT_ENV}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_ATTR_NOSYSTEM="1",
               GIT_NO_REPLACE_OBJECTS="1", GIT_NO_LAZY_FETCH="1", GIT_TERMINAL_PROMPT="0",
               GIT_OPTIONAL_LOCKS="0")
    env.update(extra or {})
    return env


def validate_branch(branch: Any) -> str:
    """Accept only a plain branch name, checked without any repository."""
    if not isinstance(branch, str) or not branch or branch.startswith("-"):
        raise PermissionError("repair branch name is invalid")
    result = subprocess.run(["git", "check-ref-format", "--branch", branch],
                            capture_output=True, env=git_environment(), cwd=os.sep,
                            check=False, timeout=LOCAL_TIMEOUT_SECONDS)
    if result.returncode or result.stdout.decode(errors="replace").strip() != branch:
        raise PermissionError("repair branch name is invalid")
    return branch


def checkout_ref(workspace: Path, *args: str) -> str | None:
    """Answer one ref query about a worker-writable checkout, or ``None`` when Git refuses.

    Only ref plumbing (``rev-parse``, ``symbolic-ref``) runs there. It reads configuration as
    data and never loads the index, filters or fsmonitor; executable settings are overridden
    and system, global and inherited configuration are off regardless.
    """
    result = subprocess.run(["git", *_HARDENING, "-C", str(workspace), *args],
                            capture_output=True, env=git_environment(), check=False,
                            timeout=LOCAL_TIMEOUT_SECONDS)
    if result.returncode:
        return None
    return result.stdout.decode(errors="replace").strip()


class RepairObjects:
    """A bare repository owned by Corral, fed only by fetches from the configured remote."""

    def __init__(self, path: Path, remote_url: Any, *, allow_file_remote: bool = False,
                 credential_helper: list[str] | None = None):
        if not isinstance(remote_url, str) or not remote_url or remote_url.startswith("-"):
            raise PermissionError("repair remote URL is unavailable")
        if ((remote_url.startswith("/") or remote_url.startswith("file://"))
                and allow_file_remote is not True):
            raise PermissionError("file remotes are restricted to explicit development fixtures")
        helper = list(credential_helper or [])
        if helper:
            binary = Path(str(helper[0]))
            if not binary.is_absolute() or not binary.is_file() or not os.access(binary, os.X_OK):
                raise PermissionError("repair Git credential helper is not a trusted executable")
        self.path, self.remote_url = Path(path).resolve(), remote_url
        self.allow_file_remote, self.credential_helper = allow_file_remote is True, helper
        if not (self.path / "objects").is_dir():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "init", "--bare", "-q", str(self.path)], capture_output=True,
                           env=git_environment(), cwd=self.path.parent, check=True,
                           timeout=LOCAL_TIMEOUT_SECONDS)

    @classmethod
    def for_repository(cls, store, repo: dict[str, Any]) -> RepairObjects:
        """Open the object store for one repository profile under the controller state."""
        remote_url = repo.get("remote_url")
        name = hashlib.sha256(str(remote_url).encode()).hexdigest()[:32]
        return cls(Path(store.path).parent / "repair-objects" / (name + ".git"), remote_url,
                   allow_file_remote=repo.get("development_file_remote") is True,
                   credential_helper=(repo.get("github") or {}).get("git_credential_helper"))

    def run(self, *args: str, input: bytes | None = None, env: dict[str, str] | None = None,
            credentials: bool = False, check: bool = True, timeout: float | None = None,
            work_tree: Path | None = None) -> subprocess.CompletedProcess:
        command = ["git", *_HARDENING, "-c",
                   "protocol.file.allow=" + ("always" if self.allow_file_remote else "never")]
        if credentials and self.credential_helper:
            command += ["-c", "credential.helper=!" + shlex.join(self.credential_helper),
                        "-c", "credential.useHttpPath=true"]
        command.append("--git-dir=" + str(self.path))
        if work_tree is not None:
            command.append("--work-tree=" + str(work_tree))
        command += args
        result = subprocess.run(command, input=input, capture_output=True, cwd=self.path,
                                env=git_environment(env), check=False,
                                timeout=timeout or LOCAL_TIMEOUT_SECONDS)
        if check and result.returncode:
            raise RuntimeError(f"trusted repair Git operation failed: {args[0]}")
        return result

    def _text(self, *args: str, **kwargs: Any) -> str:
        return self.run(*args, **kwargs).stdout.decode().strip()

    def fetch_branch(self, branch: str, pr_number: int) -> str:
        """Fetch the remote repair branch and return its tip commit."""
        validate_branch(branch)
        ref = f"refs/corral/repair/pr-{int(pr_number)}"
        self.run("fetch", "--no-tags", "--force", "--no-write-fetch-head", "--",
                 self.remote_url, f"+refs/heads/{branch}:{ref}", credentials=True,
                 timeout=FETCH_TIMEOUT_SECONDS)
        tip = self._text("rev-parse", "--verify", "--end-of-options", ref + "^{commit}")
        if not SHA.fullmatch(tip):
            raise RuntimeError("fetched repair branch did not resolve to a commit")
        return tip

    def has_commit(self, oid: str) -> bool:
        return bool(SHA.fullmatch(str(oid))) and self.run(
            "cat-file", "-e", oid + "^{commit}", check=False).returncode == 0

    def _index(self) -> Path:
        fd, name = tempfile.mkstemp(prefix="index-", dir=self.path)
        os.close(fd)
        index = Path(name)
        # read-tree refuses an empty file; it creates the index itself.
        index.unlink()
        return index

    def build_commit(self, parent: str, files: dict[str, tuple[bytes | None, int | None]], *,
                     identity: dict[str, str], when: int, message: str) -> dict[str, Any]:
        """Write ``files`` over ``parent``'s tree and commit it; report every changed path.

        A file whose data is ``None`` is removed. Blobs are hashed from the given bytes with no
        filters, and the changed-path report comes from a tree diff in this repository, so a
        caller can refuse any effect outside the paths it authorized.
        """
        if not self.has_commit(parent):
            raise PermissionError("repair parent commit is not in the trusted object store")
        index = self._index()
        try:
            env = {"GIT_INDEX_FILE": str(index)}
            self.run("read-tree", parent, env=env)
            records = []
            for name in sorted(files):
                data, mode = files[name]
                if data is None:
                    records.append(f"0 {_ZERO}\t{name}\0")
                    continue
                blob = self._text("hash-object", "-w", "--no-filters", "--stdin", input=data)
                records.append(f"{'100755' if mode & 0o100 else '100644'} {blob}\t{name}\0")
            self.run("update-index", "-z", "--index-info",
                     input="".join(records).encode(), env=env)
            tree = self._text("write-tree", env=env)
        finally:
            index.unlink(missing_ok=True)
        parent_tree = self._text("rev-parse", "--verify", parent + "^{tree}")
        raw = self.run("diff-tree", "-r", "-z", "--name-only", "--no-renames",
                       parent_tree, tree).stdout
        try:
            changed = sorted({name for name in raw.decode().split("\0") if name})
        except UnicodeDecodeError as error:
            raise PermissionError("repair path names must be UTF-8") from error
        date = f"@{int(when)} +0000"
        commit = self._text("commit-tree", tree, "-p", parent, input=message.encode(), env={
            "GIT_AUTHOR_NAME": identity["name"], "GIT_AUTHOR_EMAIL": identity["email"],
            "GIT_COMMITTER_NAME": identity["name"], "GIT_COMMITTER_EMAIL": identity["email"],
            "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date})
        if not SHA.fullmatch(commit):
            raise RuntimeError("repair commit object was not created")
        return {"tree": tree, "commit": commit, "changed": changed}

    def push(self, commit: str, branch: str, *, timeout: float) -> subprocess.CompletedProcess:
        """Non-force push of one commit to the remote branch; the caller reads the effect back."""
        validate_branch(branch)
        return self.run("push", "--porcelain", "--no-verify", "--", self.remote_url,
                        f"{commit}:refs/heads/{branch}", credentials=True, check=False,
                        timeout=timeout)

    def worktree_differences(self, checkout: Path, head: str) -> list[str]:
        """List checkout paths that differ from the trusted ``head`` tree.

        The comparison runs with Corral's ``GIT_DIR`` and a temporary index read from trusted
        objects, so none of the checkout's own configuration, index flags, excludes or replace
        refs apply. In-tree ``.gitignore`` files, which are part of the compared tree, still
        decide which untracked paths are ignored. Submodule contents are not inspected.
        """
        if not self.has_commit(head):
            raise PermissionError("repair head is not in the trusted object store")
        index = self._index()
        try:
            env = {"GIT_INDEX_FILE": str(index)}
            self.run("read-tree", head, env=env)
            # Exit status 1 only means entries need updating; diff-files reports them.
            self.run("update-index", "-q", "--ignore-submodules", "--refresh",
                     env=env, work_tree=checkout, check=False)
            tracked = self.run("diff-files", "--name-only", "-z", "--ignore-submodules=all",
                               env=env, work_tree=checkout).stdout
            untracked = self.run("ls-files", "-z", "--others", "--exclude-standard",
                                 "--directory", "--no-empty-directory",
                                 env=env, work_tree=checkout).stdout
        finally:
            index.unlink(missing_ok=True)
        return sorted({name.decode(errors="replace")
                       for name in (tracked + untracked).split(b"\0") if name})
