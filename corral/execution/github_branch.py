"""Exact-head, non-force publication of one accepted repair candidate."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from . import continuation
from .repair_objects import RepairObjects, validate_branch
from .store import digest

_SHA = re.compile(r"^[0-9a-f]{40}$")

#: Wall-clock bound for one authenticated GitHub read through the configured executable.
GITHUB_READ_TIMEOUT_SECONDS = 120
#: Wall-clock bound for the branch push. A push that times out may still have landed, so the
#: publisher always reads the remote branch back before it reports an outcome.
PUSH_TIMEOUT_SECONDS = 600
#: Repair-policy fields that decide what is published and by whom. Admission snapshots them
#: into the request; publication uses the snapshot and refuses if the live policy differs.
PUBLICATION_FIELDS = ("branch", "allowed_prs", "commit_identity", "publisher_actor",
                      "publisher_actor_id", "publisher_account_ref")


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def publication_snapshot(policy: dict[str, Any]) -> dict[str, Any]:
    """The publication-relevant part of a validated repair policy, frozen at admission."""
    return json.loads(json.dumps({key: policy.get(key) for key in PUBLICATION_FIELDS}))


class BranchPublisher:
    """Publish one controller-accepted task to its existing same-repository PR branch.

    Git never runs inside the worker-writable repair checkout. The commit is built in a
    Corral-owned object store from the bytes of the accepted candidate manifest, on top of the
    admitted head fetched from the remote, and pushed from there.
    """

    def __init__(self, *, service, repository: str):
        self.service, self.store = service, service.store
        self.repository_name = repository
        self.repo = service._repository(repository)
        self.github_repository = self.repo.get("github_repository")
        if not isinstance(self.github_repository, str) or "/" not in self.github_repository:
            raise PermissionError("repair publisher requires github_repository")

    def _objects(self) -> RepairObjects:
        return RepairObjects.for_repository(self.store, self.repo)

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

    def _actor(self, publication: dict[str, Any]) -> dict[str, Any]:
        observed = self._github("user")
        login, actor_id = observed.get("login"), observed.get("id")
        if (login != publication["publisher_actor"] or isinstance(actor_id, bool)
                or not isinstance(actor_id, int) or actor_id <= 0):
            raise PermissionError("authenticated GitHub actor differs from repair policy")
        expected_id = publication.get("publisher_actor_id")
        if expected_id is not None and actor_id != expected_id:
            raise PermissionError("authenticated GitHub actor id differs from repair policy")
        return {"login": login, "id": actor_id,
                "account_ref": publication["publisher_account_ref"],
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

    def _snapshot(self, request: Any) -> dict[str, Any]:
        snapshot = request.get("repair_publication") if isinstance(request, dict) else None
        if not isinstance(snapshot, dict) or set(snapshot) != set(PUBLICATION_FIELDS):
            raise PermissionError("repair request has no admitted publication policy")
        return snapshot

    def _publication(self, request: dict[str, Any]) -> dict[str, Any]:
        """The admitted publication policy, provided the live policy still authorizes it."""
        snapshot = self._snapshot(request)
        live = (self.repo.get("repair_policies") or {}).get(request.get("repair_policy_id"))
        if (not isinstance(live, dict)
                or publication_snapshot(live) != snapshot
                or request.get("pr_number") not in (snapshot.get("allowed_prs") or ())):
            raise PermissionError("repair policy changed since admission")
        return snapshot

    def _accepted(self, event_id: str) -> tuple[dict, dict, dict, dict]:
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
        return event, request, state, result

    def _manifest(self, request: dict, result: dict) -> dict[str, tuple[bytes | None, int | None]]:
        """Decode the accepted candidate bytes from the controller-owned manifest.

        The manifest is read once and bound to the verifier receipt by its digest; the commit is
        built from exactly these bytes, so the worker checkout is never read again.
        """
        refused = PermissionError("accepted repair manifest differs from verifier receipt")
        try:
            value = json.loads((Path(result["artifact_directory"])
                                / "candidate_manifest.json").read_text())
        except (KeyError, TypeError, OSError, ValueError) as error:
            raise PermissionError("accepted repair candidate manifest is unavailable") from error
        if not isinstance(value, dict):
            raise refused
        core = {"base": value.get("base"), "files": value.get("files")}
        files, paths = value.get("files"), request.get("candidate_paths") or []
        if (value.get("digest") != digest(core) or value.get("base") != request["expected_head"]
                or not isinstance(files, dict) or set(files) != set(paths)
                or (result.get("receipt") or {}).get("candidate_post") != value.get("digest")):
            raise refused
        decoded: dict[str, tuple[bytes | None, int | None]] = {}
        for name in paths:
            item = files[name]
            if not isinstance(item, dict):
                raise refused
            data, mode, expected = item.get("data"), item.get("mode"), item.get("digest")
            if data is None:
                if mode is not None or expected is not None:
                    raise refused
                decoded[name] = (None, None)
                continue
            if (not isinstance(data, str) or isinstance(mode, bool) or not isinstance(mode, int)
                    or not 0 <= mode <= 0o777):
                raise refused
            try:
                raw = base64.b64decode(data, validate=True)
            except ValueError as error:
                raise refused from error
            if hashlib.sha256(raw).hexdigest() != expected:
                raise refused
            decoded[name] = (raw, mode)
        return decoded

    def _commit(self, event: dict, request: dict, state: dict, result: dict, resource: str,
                objects: RepairObjects, publication: dict) -> dict:
        task, generation = event["task_id"], continuation.generation_of(state)
        key = f"{task}:g{generation}"
        prior = self.store.get("repair_commit", key)
        if prior and objects.has_commit(prior["new_head"]):
            return prior
        files = self._manifest(request, result)
        old = request["expected_head"]
        if not objects.has_commit(old):
            objects.fetch_branch(publication["branch"], request["pr_number"])
            if not objects.has_commit(old):
                raise PermissionError("admitted PR head is not reachable from the repair branch")
        # The commit date is the real time Corral first built this repair commit: the first
        # publication attempt for this accepted generation, not the repair task's admission.
        # It is persisted once under the PR owner fence and every retry reuses it, so a
        # recreated commit object (and its SHA) is identical to the first one.
        seed = self.store.owned_once(
            resource, request["pr_owner"], request["pr_owner_epoch"], "repair_commit_seed", key,
            {"task": task, "generation": generation, "attempt": state["attempt"],
             "manifest": result["receipt"]["candidate_post"]},
            {"admitted_at": int(time.time())})
        admitted_at = seed.get("admitted_at")
        if isinstance(admitted_at, bool) or not isinstance(admitted_at, int) or admitted_at <= 0:
            raise PermissionError("persisted repair admission time is invalid")
        message = f"Corral repair for {self.github_repository}#{request['pr_number']}\n"
        built = objects.build_commit(old, files, identity=publication["commit_identity"],
                                     when=admitted_at, message=message)
        changed = set(built["changed"])
        if not changed:
            raise PermissionError("repair produced no candidate change")
        if not changed <= set(request.get("candidate_paths") or ()):
            raise PermissionError("repair modified a path outside the registered candidate set")
        value = {"task": task, "attempt": state["attempt"], "generation": generation,
                 "old_head": old, "new_head": built["commit"], "tree": built["tree"],
                 "candidate_manifest": result["receipt"]["candidate_post"],
                 "admitted_at": admitted_at}
        if prior:
            if prior != value:
                raise RuntimeError("rebuilt repair commit differs from its recorded identity")
            return prior
        self.store.put_once("repair_commit", key, value)
        return value

    def publish(self, event_id: str) -> dict[str, Any]:
        event, request, state, result = self._accepted(event_id)
        publication = self._publication(request)
        number, branch = request["pr_number"], validate_branch(publication["branch"])
        pr = f"{self.github_repository}#{number}"
        resource = "pr:" + pr
        owner, epoch = request["pr_owner"], request["pr_owner_epoch"]
        if self.store.ownership(resource) != (owner, epoch, "active"):
            raise PermissionError("repair branch publication owner epoch is stale")
        objects = self._objects()
        actor, remote = self._actor(publication), self._remote(number)
        if remote["base"] != request["expected_base"] or remote["branch"] != branch:
            raise PermissionError("authenticated PR branch or base differs from repair policy")
        commit = self._commit(event, request, state, result, resource, objects, publication)
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
        try:
            push = objects.push(commit["new_head"], branch, timeout=PUSH_TIMEOUT_SECONDS)
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
        repository, number = stored["pr"].rsplit("#", 1)
        if repository != self.github_repository:
            raise PermissionError("repair publication intent belongs to another repository")
        publication = self._snapshot(self.store.get("request", stored["task"]))
        actor, remote = self._actor(publication), self._remote(int(number))
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
