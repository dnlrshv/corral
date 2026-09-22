"""Trusted GitHub PR admission for the authoritative Corral service."""
from __future__ import annotations

import time
import json
from pathlib import Path
from typing import Any

from .github_candidate import prepare
from .store import canonical, digest


def _owner(policy: dict[str, Any]) -> tuple[str, int]:
    owner = policy.get("owner")
    epoch = policy.get("owner_epoch")
    if not isinstance(owner, str) or not owner or not isinstance(epoch, int) \
            or isinstance(epoch, bool) or epoch <= 0:
        raise PermissionError("review policy requires a trusted owner and positive owner_epoch")
    return owner, epoch


def _assert_owner(service, resource: str, owner: str, epoch: int) -> None:
    current = service.store.ownership(resource)
    if current != (owner, epoch, "active"):
        raise PermissionError(
            f"review cohort ownership fence failed: expected {(owner, epoch, 'active')}, got {current}")


def _old_binding(service, task_id: str) -> dict[str, Any]:
    request = service.store.get("request", task_id)
    state = service.store.get("state", task_id)
    result = service.store.get("result", task_id) or {}
    if not request or not state or state.get("status") != "reconciled" \
            or service.store.get("cancel", task_id) is None or result.get("accepted") is not False:
        raise PermissionError("replacement task must be historically cancelled and reconciled")
    workspace = request.get("workspace")
    if workspace:
        ownership = service.store.ownership("workspace:" + str(Path(workspace).resolve()))
        if ownership and ownership[2] != "released":
            raise PermissionError("replacement task workspace ownership is not released")
    export_id = request.get("trusted_export_id")
    candidate = service.store.get("trusted_export", export_id) if export_id else None
    if not candidate:
        validation = result.get("inspection_validation") or {}
        candidate = validation.get("candidate")
    if not candidate:
        structured = result.get("structured") or {}
        candidate = structured.get("provenance") if isinstance(structured, dict) else None
    if not isinstance(candidate, dict):
        raise PermissionError("replacement task has no preserved candidate binding")
    return candidate


def _assert_replacement(binding: dict[str, Any], receipt: dict[str, Any]) -> None:
    expected = {"repository": receipt["repository"], "pr_number": receipt["pr_number"],
                "head": receipt["head"], "base": receipt["base"]}
    aliases = {"repository": ("repository", "repo"), "pr_number": ("pr_number", "pr")}
    for field, value in expected.items():
        names = aliases.get(field, (field,))
        observed = next((binding.get(name) for name in names if binding.get(name) is not None), None)
        if observed != value:
            raise PermissionError(f"replacement candidate binding differs for {field}")


def _host(service, repo: dict[str, Any], requested: str | None) -> str:
    host = requested or repo.get("default_host") or service.controller.default_host
    if host not in service.controller.hosts:
        raise PermissionError(f"unsupported host: {host}")
    if host not in set(repo.get("allowed_hosts", ())):
        raise PermissionError(f"host is not allowed by repository profile: {host}")
    if host != service.execution_host and not service.controller.hosts[host].get("executor"):
        raise PermissionError("nonlocal host requires an authenticated remote executor route")
    return host


def submit_pr_review(service, repository: str, pr_number: int, policy_id: str, *,
                     expected_head: str | None = None, expected_base: str | None = None,
                     host: str | None = None,
                     replacement_of_task: str | None = None) -> dict[str, Any]:
    """Authenticate, export, register and admit one exact PR candidate."""
    repo = service._repository(repository)
    selected_host = _host(service, repo, host)
    github_repository = repo.get("github_repository")
    if not isinstance(github_repository, str) or "/" not in github_repository:
        raise ValueError("repository profile requires github_repository")
    policy = repo["review_policies"][policy_id]
    owner, epoch = _owner(policy)
    resource = f"pr:{github_repository}#{pr_number}"
    _assert_owner(service, resource, owner, epoch)
    old_binding = (_old_binding(service, replacement_of_task)
                   if replacement_of_task is not None else None)
    receipt, observation = prepare(
        service.store.path.parent, github_repository, pr_number, policy_id, repo,
        expected_head=expected_head, expected_base=expected_base)
    service.store.put_once("trusted_export", receipt["export_id"], receipt)
    service.store.put_once("candidate_observation", digest(observation), observation)
    if old_binding is not None:
        _assert_replacement(old_binding, receipt)
    profile_id = policy.get("profile_id", "corral-inspection-report-v1")
    objective = policy.get("objective", "Inspect the bound candidate and produce an advisory report.")
    spec = {"trusted_export_id": receipt["export_id"], "objective": objective,
            "profile_id": profile_id, "role": "review", "host": selected_host,
            "pr_owner": owner, "pr_owner_epoch": epoch}
    event_id = "github-review:" + receipt["export_id"]
    identity = {"event_id": event_id, "repository": github_repository,
                "repository_profile": repository, "host": selected_host,
                "mode": "interactive", "role": "review", "profile_id": profile_id,
                "route": service._profile_route(spec, selected_host),
                "pr_owner": owner, "pr_owner_epoch": epoch,
                "trusted_export_id": receipt["export_id"],
                "candidate_binding": {key: receipt[key] for key in (
                    "pr_number", "head", "base", "policy_id", "policy_digest")},
                "resolved_spec": spec}
    event = {**identity, "submitted": time.time(), "status": "admitted", "task_id": None}
    with service.store.transaction() as db:
        ownership = db.execute(
            "SELECT owner,epoch,status FROM owners WHERE resource=?", (resource,)).fetchone()
        if ownership != (owner, epoch, "active"):
            raise PermissionError("review cohort ownership changed during admission")
        row = db.execute(
            "SELECT value FROM records WHERE kind='service_event' AND key=?", (event_id,)).fetchone()
        if row:
            prior = json.loads(row[0])
            if any(prior.get(key) != value for key, value in identity.items()):
                raise ValueError("service event identity already has different content")
        else:
            db.execute("INSERT INTO records VALUES('service_event',?,?)",
                       (event_id, canonical(event)))
    result = service._ensure_task(event_id, spec=spec)
    if replacement_of_task is not None:
        new_task = result["event"].get("task_id")
        replacement = {"old_task": replacement_of_task, "new_task": new_task,
                       "event_id": event_id, "resource": resource, "owner": owner,
                       "epoch": epoch, "candidate_binding": identity["candidate_binding"]}
        service.store.put_once("review_replacement", replacement_of_task, replacement)
    return result
