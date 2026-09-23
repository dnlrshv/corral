"""Typed admission for one policy-bound PR repair task."""
from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from .internal_review import load as load_review
from .internal_review import report_body
from .store import canonical, digest
from .workspace import safe_path

_SHA = re.compile(r"^[0-9a-f]{40}$")


def _policy(repo: dict[str, Any], policy_id: str) -> dict[str, Any]:
    value = (repo.get("repair_policies") or {}).get(policy_id)
    if not isinstance(value, dict):
        raise PermissionError("review policy has no registered repair policy")
    required = ("owner", "owner_epoch", "profile_id", "objective_template",
                "candidate_paths", "verify", "branch", "allowed_prs",
                "commit_identity", "publisher_actor", "publisher_account_ref")
    if any(key not in value for key in required):
        raise PermissionError("repair policy binding is incomplete")
    if (value["owner"] != "corral" or isinstance(value["owner_epoch"], bool)
            or not isinstance(value["owner_epoch"], int) or value["owner_epoch"] <= 0):
        raise PermissionError("repair policy requires the active Corral owner epoch")
    for key in ("profile_id", "objective_template", "branch", "publisher_actor",
                "publisher_account_ref"):
        if not isinstance(value[key], str) or not value[key]:
            raise PermissionError(f"repair policy {key} is invalid")
    paths = value["candidate_paths"]
    if not isinstance(paths, list) or not paths or any(not isinstance(item, str) or not item
                                                       for item in paths):
        raise PermissionError("repair policy candidate_paths must be explicit")
    verify = value["verify"]
    if not isinstance(verify, list) or not verify or any(not isinstance(item, str) or not item
                                                         for item in verify):
        raise PermissionError("repair policy verifier command must be explicit")
    allowed_prs = value["allowed_prs"]
    if (not isinstance(allowed_prs, list) or not allowed_prs
            or any(isinstance(item, bool) or not isinstance(item, int) or item <= 0
                   for item in allowed_prs)):
        raise PermissionError("repair policy allowed_prs must be explicit")
    identity = value["commit_identity"]
    if (not isinstance(identity, dict) or not isinstance(identity.get("name"), str)
            or not identity["name"] or not isinstance(identity.get("email"), str)
            or "@" not in identity["email"]):
        raise PermissionError("repair policy commit identity is invalid")
    return json.loads(json.dumps(value))


def _checkout(workspace: str, head: str, branch: str, candidate_paths: list[str]) -> None:
    root = Path(workspace)
    inside = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
        text=True, capture_output=True, check=True).stdout.strip()
    if inside != "true":
        raise PermissionError("repair workspace must be a real isolated Git checkout")
    subprocess.run(["git", "check-ref-format", "--branch", branch],
                   text=True, capture_output=True, check=True)
    actual = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                            text=True, capture_output=True, check=True).stdout.strip()
    current_branch = subprocess.run(
        ["git", "-C", str(root), "symbolic-ref", "--short", "HEAD"],
        text=True, capture_output=True, check=True).stdout.strip()
    dirty = subprocess.run(["git", "-C", str(root), "status", "--porcelain=v1", "-z"],
                           capture_output=True, check=True).stdout
    if actual != head or current_branch != branch or dirty:
        raise PermissionError("repair workspace must be clean at the exact authorized PR branch head")
    for name in candidate_paths:
        safe_path(root, name)


def submit_pr_repair(service, repository: str, pr_number: int, review_receipt_id: str, *,
                     expected_head: str, expected_base: str,
                     host: str | None = None) -> dict[str, Any]:
    """Admit a repair from a trusted CHANGES_REQUIRED receipt; no caller objective exists."""
    if not _SHA.fullmatch(str(expected_head)) or not _SHA.fullmatch(str(expected_base)):
        raise ValueError("repair admission requires exact 40-character head and base SHAs")
    repo = service._repository(repository)
    github_repository = repo.get("github_repository")
    if not isinstance(github_repository, str) or "/" not in github_repository:
        raise PermissionError("repository profile requires github_repository")
    review = load_review(service.store, review_receipt_id)
    expected_pr = f"{github_repository}#{pr_number}"
    if (review.get("pr") != expected_pr or review.get("verdict") != "CHANGES_REQUIRED"
            or review.get("head") != expected_head or review.get("base") != expected_base):
        raise PermissionError("repair review receipt does not bind the requested candidate")
    policy = _policy(repo, review["policy_id"])
    if pr_number not in policy["allowed_prs"]:
        raise PermissionError("PR is outside the repair policy allowlist")
    resource = "pr:" + expected_pr
    owner = (policy["owner"], policy["owner_epoch"], "active")
    if service.store.ownership(resource) != owner:
        raise PermissionError("repair cohort ownership fence is stale")
    selected_host, workspace = service._host_workspace(repo, host)
    if selected_host not in set(policy.get("allowed_hosts") or repo.get("allowed_hosts") or ()):
        raise PermissionError("repair host is not authorized by the repair policy")
    _checkout(workspace, expected_head, policy["branch"], policy["candidate_paths"])
    objective = policy["objective_template"].replace("{repository}", github_repository).replace(
        "{pr}", str(pr_number)).replace("{review_report}", report_body(service.store, review))
    candidate_paths = list(policy["candidate_paths"])
    verifier_paths = list(policy.get("verifier_paths") or ())
    event_id = "github-repair:" + digest({"pr": expected_pr, "head": expected_head,
                                           "review_receipt": review_receipt_id,
                                           "policy": policy})
    spec = {"repo": repository, "workspace": workspace, "workspace_kind": "checkout",
            "host": selected_host, "mode": "interactive", "role": "repair",
            "profile_id": policy["profile_id"], "objective": objective,
            "candidate_paths": candidate_paths, "verifier_paths": verifier_paths,
            "verify": list(policy["verify"]), "pr_owner": policy["owner"],
            "pr_owner_epoch": policy["owner_epoch"], "review_receipt": review_receipt_id,
            "service_event_id": event_id, "github_repository": github_repository,
            "pr_number": pr_number,
            "expected_head": expected_head, "expected_base": expected_base,
            "repair_policy_id": review["policy_id"]}
    for key in ("cpu", "memory_mb", "result_file", "usage_file", "external_verifier"):
        if key in policy:
            spec[key] = policy[key]
    identity = {"event_id": event_id, "repository": github_repository,
                "repository_profile": repository, "pr_number": pr_number,
                "host": selected_host,
                "mode": "interactive", "role": "repair", "profile_id": policy["profile_id"],
                "route": service._profile_route(spec, selected_host),
                "pr_owner": policy["owner"], "pr_owner_epoch": policy["owner_epoch"],
                "review_receipt": review_receipt_id, "candidate_paths": candidate_paths,
                "expected_head": expected_head, "expected_base": expected_base,
                "repair_policy_id": review["policy_id"], "resolved_spec": spec}
    event = {**identity, "submitted": time.time(), "status": "admitted", "task_id": None}
    with service.store.transaction() as db:
        ownership = db.execute(
            "SELECT owner,epoch,status FROM owners WHERE resource=?", (resource,)).fetchone()
        if ownership != owner:
            raise PermissionError("repair cohort ownership changed during admission")
        row = db.execute(
            "SELECT value FROM records WHERE kind='service_event' AND key=?", (event_id,)
        ).fetchone()
        if row:
            prior = json.loads(row[0])
            if any(prior.get(key) != value for key, value in identity.items()):
                raise ValueError("repair service event identity conflicts with prior admission")
        else:
            db.execute("INSERT INTO records VALUES('service_event',?,?)",
                       (event_id, canonical(event)))
    return service._ensure_task(event_id, spec=spec)
