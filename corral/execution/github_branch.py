"""Exact-head, non-force publication of one accepted repair candidate."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shlex
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from . import continuation
from .store import digest
from .workspace import safe_path

_SHA = re.compile(r"^[0-9a-f]{40}$")

#: Wall-clock bound for one authenticated GitHub read through the configured executable.
GITHUB_READ_TIMEOUT_SECONDS = 120
#: Wall-clock bound for the branch push. A push that times out may still have landed, so the
#: publisher always reads the remote branch back before it reports an outcome.
PUSH_TIMEOUT_SECONDS = 600


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


class BranchPublisher:
    """Publish one controller-accepted task to its existing same-repository PR branch."""

    def __init__(self, *, service, repository: str):
        self.service, self.store = service, service.store
        self.repository_name = repository
        self.repo = service._repository(repository)
        self.github_repository = self.repo.get("github_repository")
        if not isinstance(self.github_repository, str) or "/" not in self.github_repository:
            raise PermissionError("repair publisher requires github_repository")

    def _git(self, workspace: Path, *args: str, env: dict | None = None,
             data: bytes | None = None, check: bool = True,
             timeout: float | None = None) -> subprocess.CompletedProcess:
        helper = (self.repo.get("github") or {}).get("git_credential_helper") or []
        command = ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
                   "-c", "diff.external=", "-c", "credential.helper="]
        if helper:
            binary = Path(helper[0])
            if not binary.is_absolute() or not binary.is_file() or not os.access(binary, os.X_OK):
                raise PermissionError("repair Git credential helper is not a trusted executable")
            command += ["-c", "credential.helper=!" + shlex.join(helper),
                        "-c", "credential.useHttpPath=true"]
        command += ["-C", str(workspace), *args]
        clean_env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1",
                     "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0",
                     **(env or {})}
        return subprocess.run(command, input=data, capture_output=True, env=clean_env,
                              check=check, timeout=timeout)

    def _github(self, path: str) -> dict[str, Any]:
        github = self.repo.get("github") or {}
        executable = Path(str(github.get("executable") or "gh"))
        if (not executable.is_absolute() or not executable.is_file()
                or not os.access(executable, os.X_OK)):
            raise PermissionError("repair GitHub reader must be an absolute trusted executable")
        try:
            result = subprocess.run([str(executable), "api", "--method", "GET", path],
                                    text=True, capture_output=True, check=False,
                                    timeout=GITHUB_READ_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as error:
            raise RuntimeError("authenticated GitHub repair read timed out") from error
        if result.returncode:
            raise RuntimeError("authenticated GitHub repair read failed")
        try:
            value = json.loads(result.stdout)
        except ValueError as error:
            raise RuntimeError("authenticated GitHub repair read is malformed") from error
        if not isinstance(value, dict):
            raise RuntimeError("authenticated GitHub repair read is malformed")
        return value

    def _actor(self, policy: dict[str, Any]) -> dict[str, Any]:
        observed = self._github("user")
        login, actor_id = observed.get("login"), observed.get("id")
        if (login != policy["publisher_actor"] or isinstance(actor_id, bool)
                or not isinstance(actor_id, int) or actor_id <= 0):
            raise PermissionError("authenticated GitHub actor differs from repair policy")
        expected_id = policy.get("publisher_actor_id")
        if expected_id is not None and actor_id != expected_id:
            raise PermissionError("authenticated GitHub actor id differs from repair policy")
        return {"login": login, "id": actor_id,
                "account_ref": policy["publisher_account_ref"],
                "auth_mode": (self.repo.get("github") or {}).get("auth_mode", "unknown")}

    def _remote(self, pr_number: int) -> dict[str, Any]:
        pull = self._github(f"repos/{self.github_repository}/pulls/{pr_number}")
        head, base = _mapping(pull.get("head")), _mapping(pull.get("base"))
        value = {"state": pull.get("state"), "head": head.get("sha"),
                 "base": base.get("sha"), "branch": head.get("ref"),
                 "head_repository": _mapping(head.get("repo")).get("full_name")}
        if (value["state"] != "open" or not _SHA.fullmatch(str(value["head"] or ""))
                or not _SHA.fullmatch(str(value["base"] or ""))
                or not isinstance(value["branch"], str) or not value["branch"]):
            raise PermissionError("authenticated PR branch identity is incomplete")
        if value["head_repository"] != self.github_repository:
            raise PermissionError("fork PR repair publication is outside the supported mode")
        return value

    def _accepted(self, event_id: str) -> tuple[dict, dict, dict, dict, Path]:
        event = self.store.get("service_event", event_id)
        if not isinstance(event, dict) or event.get("role") != "repair":
            raise PermissionError("branch publication requires a typed repair event")
        task = event.get("task_id")
        request, state = self.store.get("request", task), self.store.get("state", task)
        generation = continuation.generation_of(state)
        result = continuation.results(self.store, task).get(generation)
        if (not isinstance(request, dict) or request.get("service_event_id") != event_id
                or request.get("github_repository") != self.github_repository
                or request.get("pr_number") != event.get("pr_number")
                or not isinstance(state, dict) or state.get("status") != "completed"
                or not isinstance(result, dict) or result.get("accepted") is not True
                or result.get("amendment_pending") is True
                or continuation.generation_of(result) != generation
                or event.get("review_receipt") != request.get("review_receipt")):
            raise PermissionError("repair event has no exact accepted terminal result")
        attempt = state.get("attempt")
        invocation = self.store.get("invocation", attempt) if isinstance(attempt, str) else None
        if (not isinstance(invocation, dict) or invocation.get("task") != task
                or continuation.generation_of(invocation) != generation):
            raise PermissionError("repair invocation does not match its accepted result")
        workspace = Path(request["workspace"]).resolve()
        return event, request, state, result, workspace

    def _manifest(self, request: dict, result: dict, workspace: Path) -> dict:
        try:
            value = json.loads((Path(result["artifact_directory"])
                                / "candidate_manifest.json").read_text())
        except (KeyError, OSError, ValueError) as error:
            raise PermissionError("accepted repair candidate manifest is unavailable") from error
        core = {"base": value.get("base"), "files": value.get("files")}
        files, paths = value.get("files"), request.get("candidate_paths") or []
        if (value.get("digest") != digest(core) or value.get("base") != request["expected_head"]
                or not isinstance(files, dict) or set(files) != set(paths)
                or (result.get("receipt") or {}).get("candidate_post") != value.get("digest")):
            raise PermissionError("accepted repair manifest differs from verifier receipt")
        for name in paths:
            item, path = files[name], safe_path(workspace, name)
            data = path.read_bytes() if path.is_file() else None
            encoded = base64.b64encode(data).decode() if data is not None else None
            mode = path.stat().st_mode & 0o777 if data is not None else None
            if (not isinstance(item, dict) or item.get("data") != encoded
                    or item.get("mode") != mode or item.get("digest") != (
                        hashlib.sha256(data).hexdigest() if data is not None else None)):
                raise PermissionError("workspace bytes differ from accepted repair artifact")
        return value

    def _changed_paths(self, workspace: Path) -> set[str]:
        tracked = self._git(workspace, "diff", "--name-only", "-z", "HEAD", "--").stdout
        untracked = self._git(
            workspace, "ls-files", "--others", "--exclude-standard", "-z").stdout
        try:
            return {name for name in (tracked + untracked).decode().split("\0") if name}
        except UnicodeDecodeError as error:
            raise PermissionError("repair path names must be UTF-8") from error

    def _commit(self, event: dict, request: dict, state: dict, result: dict,
                workspace: Path, resource: str) -> dict:
        task, generation = event["task_id"], continuation.generation_of(state)
        key = f"{task}:g{generation}"
        prior = self.store.get("repair_commit", key)
        if prior:
            return prior
        forbidden = self._git(
            workspace, "config", "--local", "--name-only", "--get-regexp",
            r"^(filter\.|url\.|credential\.|core\.sshCommand$|core\.fsmonitor$)",
            check=False).stdout
        if forbidden.strip():
            raise PermissionError("repair checkout contains executable or redirecting Git config")
        old = self._git(workspace, "rev-parse", "HEAD").stdout.decode().strip()
        if old != request["expected_head"]:
            raise PermissionError("repair checkout HEAD moved from the admitted candidate")
        changed, allowed = self._changed_paths(workspace), set(request.get("candidate_paths") or ())
        if not changed:
            raise PermissionError("repair produced no candidate change")
        if not changed <= allowed:
            raise PermissionError("repair modified a path outside the registered candidate set")
        self._manifest(request, result, workspace)
        # The commit date is the real time this repair commit was first admitted. It is persisted
        # once under the PR owner fence and every retry reuses it, so a recreated commit object
        # (and its SHA) is identical to the first one.
        seed = self.store.owned_once(
            resource, request["pr_owner"], request["pr_owner_epoch"], "repair_commit_seed", key,
            {"task": task, "generation": generation, "attempt": state["attempt"],
             "manifest": result["receipt"]["candidate_post"]},
            {"admitted_at": int(time.time())})
        admitted_at = seed.get("admitted_at")
        if isinstance(admitted_at, bool) or not isinstance(admitted_at, int) or admitted_at <= 0:
            raise PermissionError("persisted repair admission time is invalid")
        fd, index_name = tempfile.mkstemp(prefix="corral-repair-index-",
                                          dir=self.store.path.parent)
        os.close(fd)
        index = Path(index_name)
        try:
            index.unlink()
            env = {"GIT_INDEX_FILE": str(index)}
            self._git(workspace, "read-tree", old, env=env)
            self._git(workspace, "add", "-A", "--", *sorted(allowed), env=env)
            tree = self._git(workspace, "write-tree", env=env).stdout.decode().strip()
            policy = self.repo["repair_policies"][request["repair_policy_id"]]
            identity = policy["commit_identity"]
            git_date = f"@{admitted_at} +0000"
            commit_env = {**env, "GIT_AUTHOR_NAME": identity["name"],
                          "GIT_AUTHOR_EMAIL": identity["email"],
                          "GIT_COMMITTER_NAME": identity["name"],
                          "GIT_COMMITTER_EMAIL": identity["email"],
                          "GIT_AUTHOR_DATE": git_date, "GIT_COMMITTER_DATE": git_date}
            message = f"Corral repair for {self.github_repository}#{request['pr_number']}\n"
            commit = self._git(workspace, "commit-tree", tree, "-p", old,
                               env=commit_env, data=message.encode()).stdout.decode().strip()
        finally:
            index.unlink(missing_ok=True)
        if not _SHA.fullmatch(commit):
            raise RuntimeError("repair commit object was not created")
        value = {"task": task, "attempt": state["attempt"], "generation": generation,
                 "old_head": old, "new_head": commit, "tree": tree,
                 "candidate_manifest": result["receipt"]["candidate_post"],
                 "admitted_at": admitted_at}
        self.store.put_once("repair_commit", key, value)
        return value

    def publish(self, event_id: str) -> dict[str, Any]:
        event, request, state, result, workspace = self._accepted(event_id)
        policy = self.repo["repair_policies"][request["repair_policy_id"]]
        number, branch = request["pr_number"], policy["branch"]
        if number not in policy["allowed_prs"]:
            raise PermissionError("PR is outside the repair policy allowlist")
        self._git(workspace, "check-ref-format", "--branch", branch)
        pr = f"{self.github_repository}#{number}"
        resource = "pr:" + pr
        owner, epoch = request["pr_owner"], request["pr_owner_epoch"]
        if self.store.ownership(resource) != (owner, epoch, "active"):
            raise PermissionError("repair branch publication owner epoch is stale")
        actor, remote = self._actor(policy), self._remote(number)
        if remote["base"] != request["expected_base"] or remote["branch"] != branch:
            raise PermissionError("authenticated PR branch or base differs from repair policy")
        commit = self._commit(event, request, state, result, workspace, resource)
        intent = digest({"pr": pr, "owner": owner, "epoch": epoch,
                         "actor": actor, **commit})
        receipt = self.store.get("repair_publish_receipt", intent)
        if receipt:
            return receipt
        prior = self.store.get("repair_publish_intent", intent)
        if prior:
            observed = self.reconcile(intent)
            if observed.get("published") is True:
                return observed
        elif remote["head"] != commit["old_head"]:
            raise PermissionError("PR head changed before repair publication")
        else:
            self.store.owned_operation(
                resource, owner, epoch, "repair_publish_intent", intent,
                {"intent": intent, "pr": pr, "branch": branch, "actor": actor, **commit})
        remote_url = self.repo.get("remote_url")
        if not isinstance(remote_url, str) or not remote_url:
            raise PermissionError("repair publisher remote URL is unavailable")
        if ((remote_url.startswith("/") or remote_url.startswith("file://"))
                and self.repo.get("development_file_remote") is not True):
            raise PermissionError("file remotes are restricted to explicit development fixtures")
        try:
            push = self._git(workspace, "push", "--porcelain", "--", remote_url,
                             f"{commit['new_head']}:refs/heads/{branch}", check=False,
                             timeout=PUSH_TIMEOUT_SECONDS)
            outcome = f"exit={push.returncode}"
        except subprocess.TimeoutExpired:
            outcome = f"timed out after {PUSH_TIMEOUT_SECONDS} s"
        observed = self.reconcile(intent)
        if observed.get("published") is not True:
            raise PermissionError(f"repair push was not confirmed ({outcome})")
        return observed

    def reconcile(self, intent: str) -> dict[str, Any]:
        stored = self.store.get("repair_publish_intent", intent)
        if not isinstance(stored, dict):
            raise ValueError("unknown repair publication intent")
        existing = self.store.get("repair_publish_receipt", intent)
        if existing:
            return existing
        _repo, number = stored["pr"].rsplit("#", 1)
        request = self.store.get("request", stored["task"])
        policy = self.repo["repair_policies"][request["repair_policy_id"]]
        actor, remote = self._actor(policy), self._remote(int(number))
        if actor != stored["actor"]:
            raise PermissionError("repair publisher identity changed during reconciliation")
        if remote["head"] == stored["new_head"] and remote["branch"] == stored["branch"]:
            receipt = {"intent": intent, "pr": stored["pr"], "old_head": stored["old_head"],
                       "new_head": stored["new_head"], "branch": stored["branch"],
                       "publisher": actor, "published": True, "observed": True,
                       "observed_at": time.time()}
            try:
                self.store.put_once("repair_publish_receipt", intent, receipt)
            except ValueError:
                # A concurrent reconciliation recorded its own observation of the same effect.
                return self.store.get("repair_publish_receipt", intent)
            return receipt
        if remote["head"] != stored["old_head"]:
            raise PermissionError("repair publication effect is unresolved at a different PR head")
        return {"intent": intent, "pr": stored["pr"], "published": False,
                "observed": True, "status": "not-delivered", "publisher": actor}
