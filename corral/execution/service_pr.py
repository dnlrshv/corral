"""Trusted GitHub PR admission for the authoritative Corral service."""
from __future__ import annotations

import time
from typing import Any

from .github_candidate import prepare


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
                     host: str | None = None) -> dict[str, Any]:
    """Authenticate, export, register and admit one exact PR candidate."""
    repo = service._repository(repository)
    selected_host = _host(service, repo, host)
    github_repository = repo.get("github_repository")
    if not isinstance(github_repository, str) or "/" not in github_repository:
        raise ValueError("repository profile requires github_repository")
    receipt = prepare(service.store.path.parent, github_repository, pr_number, policy_id, repo,
                      expected_head=expected_head, expected_base=expected_base)
    service.store.put_once("trusted_export", receipt["export_id"], receipt)
    policy = repo["review_policies"][policy_id]
    profile_id = policy.get("profile_id", "corral-inspection-report-v1")
    objective = policy.get("objective", "Inspect the bound candidate and produce an advisory report.")
    spec = {"trusted_export_id": receipt["export_id"], "objective": objective,
            "profile_id": profile_id, "role": "review", "host": selected_host}
    event_id = "github-review:" + receipt["export_id"]
    identity = {"event_id": event_id, "repository": github_repository,
                "repository_profile": repository, "host": selected_host,
                "mode": "interactive", "role": "review", "profile_id": profile_id,
                "route": service._profile_route(spec, selected_host),
                "trusted_export_id": receipt["export_id"],
                "candidate_binding": {key: receipt[key] for key in (
                    "pr_number", "head", "base", "policy_id", "policy_digest")},
                "resolved_spec": spec}
    prior = service.store.get("service_event", event_id)
    if prior is not None:
        if any(prior.get(key) != value for key, value in identity.items()):
            raise ValueError("service event identity already has different content")
        return service._ensure_task(event_id)
    event = {**identity, "submitted": time.time(), "status": "admitted", "task_id": None}
    service.store.put_once("service_event", event_id, event)
    return service._ensure_task(event_id, spec=spec)
