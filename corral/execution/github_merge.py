"""Authenticated, evidence-bound GitHub merge transport; advisory publishing stays separate."""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from typing import Any, Callable

from .github_support import (GitHubHTTPError, _NoRedirectHandler, execute_github_request,
                             parse_pr_identity, read_pages, verify_remote_candidate)
from .policy import (_get_auth_token, _strip_dynamic_protection_metadata,
                     _strip_dynamic_ruleset_metadata, compute_policy_digest)
from .policy_schema import validate_protection, validate_ruleset
from .store import digest


class GitHubMergeTransport:
    """One exact-candidate merge intent, with evidence before and after its PUT.

    A read-only preflight never creates a ``merge_intent``. Only the final fenced
    transition immediately before PUT records the durable external-effect intent.
    """

    transport_name = "github_merge_v1"

    def __init__(self, *, store, token: str | None, actor: str,
                 http_client: Callable[..., Any] | None = None, allow_network: bool = False,
                 auth_mode: str = "user-token", merge_policy: dict):
        if auth_mode != "user-token":
            raise PermissionError("installation-token merge identity is unsupported until its actor readback is configured")
        resolved = _get_auth_token(token)
        if not resolved:
            raise PermissionError("authenticated GitHub merge token is required")
        self.store, self.token, self.actor = store, resolved, actor
        self.account_ref = str(merge_policy.get("account_ref") or "")
        if not self.account_ref:
            raise PermissionError("merge policy requires a configured account reference")
        self.http_client, self.allow_network = http_client, allow_network
        self._opener = urllib.request.build_opener(_NoRedirectHandler())
        self.merge_policy = self._bind_policy(merge_policy)
        self._authenticate()

    @staticmethod
    def _bind_policy(policy: dict) -> dict:
        """Validate a controller-loaded immutable policy snapshot before any network effect."""
        required = ("repo", "base", "campaign_authorization", "required_checks",
                    "required_reviewers", "required_internal_reviews", "policy_snapshot",
                    "base_ref", "live_policy_digest", "account_ref")
        if (not isinstance(policy, dict) or any(key not in policy for key in required)
                or any(not policy.get(key) for key in required
                       if key not in {"required_reviewers", "required_internal_reviews"})):
            raise PermissionError("merge requires a complete controller policy binding")
        snapshot = policy["policy_snapshot"]
        if not isinstance(snapshot, dict):
            raise PermissionError("merge policy snapshot is incomplete or does not bind the candidate base")
        enforcement = snapshot.get("enforcement_contents")
        if (snapshot.get("repo") != policy["repo"] or snapshot.get("base_sha") != policy["base"]
                or snapshot.get("base_ref") != policy["base_ref"] or not isinstance(enforcement, dict)
                or enforcement.get("repo") != policy["repo"] or enforcement.get("base_sha") != policy["base"]
                or not isinstance(enforcement.get("rulesets"), list)
                or "classic_protection" not in enforcement
                or compute_policy_digest(enforcement) != snapshot.get("enforcement_digest")):
            raise PermissionError("merge policy snapshot is incomplete, corrupted, or does not bind the candidate base")
        # The fields compared live are explicitly projected from the digest-verified snapshot.
        live = {"rulesets": enforcement["rulesets"],
                "classic_protection": enforcement["classic_protection"]}
        if compute_policy_digest(live) != policy["live_policy_digest"]:
            raise PermissionError("merge live policy digest does not bind the verified snapshot")
        if policy.get("risk_excluded"):
            raise PermissionError("repository risk exclusion requires separate authorization")
        if not all(isinstance(name, str) and name for name in policy["required_checks"]):
            raise PermissionError("merge required checks must be explicit")
        if not all(isinstance(actor, str) and actor for actor in policy["required_reviewers"]):
            raise PermissionError("merge required reviewers must be explicit")
        internal = policy["required_internal_reviews"]
        if (not isinstance(internal, list)
                or any(not isinstance(item, dict) or not item
                       or set(item) - {"profile_id", "policy_id"}
                       or any(not isinstance(value, str) or not value for value in item.values())
                       for item in internal)):
            raise PermissionError("merge required internal reviews must be explicit")
        if not policy["required_reviewers"] and not internal:
            raise PermissionError("merge requires remote or trusted internal review evidence")
        if internal:
            value = policy.get("review_policy_digest")
            if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
                raise PermissionError("internal review requirements need a source policy digest")
            required = (*required, "review_policy_digest")
        return json.loads(json.dumps({key: policy[key] for key in (*required, "risk_excluded") if key in policy}))

    def _request(self, method: str, path: str, *, json_data: dict | None = None) -> Any:
        return execute_github_request(
            self._opener, self.http_client,
            {"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
             "User-Agent": "Corral-Merge"}, self.allow_network, 20, method, path, json_data,
            lambda message: message.replace(self.token, "[REDACTED_TOKEN]"),
        )

    def _authenticate(self) -> None:
        user = self._request("GET", "/user")
        login = (user if isinstance(user, dict) else json.loads(user)).get("login")
        if login != self.actor:
            raise PermissionError("authenticated GitHub merge actor does not match configuration")

    def _read(self, repo: str, number: int, head: str, base: str) -> dict:
        pull = verify_remote_candidate(self._request, repo, number, head, base)
        checks, page = [], 1
        while True:
            response = self._request("GET", f"/repos/{repo}/commits/{head}/check-runs?per_page=100&page={page}")
            batch = response.get("check_runs") if isinstance(response, dict) else None
            total = response.get("total_count") if isinstance(response, dict) else None
            if not isinstance(batch, list) or type(total) is not int or total < 0:
                raise PermissionError("GitHub check-run readback is malformed")
            checks.extend(batch)
            if len(batch) < 100:
                if len(checks) != total:
                    raise PermissionError("GitHub check-run pagination is incomplete")
                break
            if len(checks) >= total:
                if len(checks) != total:
                    raise PermissionError("GitHub check-run pagination is inconsistent")
                break
            page += 1
        return {"pull": pull, "checks": checks,
                "reviews": read_pages(self._request, f"/repos/{repo}/pulls/{number}/reviews")}

    def _live_policy_digest(self, repo: str) -> str:
        """Read normalized detailed rules and classic protection; 404 alone means no protection."""
        rulesets, page = [], 1
        while True:
            listed = self._request("GET", f"/repos/{repo}/rulesets?per_page=100&page={page}&includes_parents=true")
            if not isinstance(listed, list) or any(type(item.get("id")) is not int for item in listed if isinstance(item, dict)):
                raise PermissionError("GitHub ruleset readback is malformed")
            for item in listed:
                if not isinstance(item, dict) or type(item.get("id")) is not int or item["id"] <= 0:
                    raise PermissionError("GitHub ruleset listing is malformed")
                detail = self._request("GET", f"/repos/{repo}/rulesets/{item['id']}")
                if not isinstance(detail, dict):
                    raise PermissionError("GitHub ruleset detail readback is malformed")
                validate_ruleset(detail, item["id"])
                rulesets.append(_strip_dynamic_ruleset_metadata(detail))
            if len(listed) < 100:
                break
            page += 1
        rulesets.sort(key=lambda item: int(item["id"]))
        branch = urllib.parse.quote(self.merge_policy["base_ref"], safe="")
        try:
            protection = self._request("GET", f"/repos/{repo}/branches/{branch}/protection")
        except GitHubHTTPError as error:
            if error.status != 404:
                raise
            protection = None
        if protection is not None:
            if not isinstance(protection, dict):
                raise PermissionError("GitHub branch protection readback is malformed")
            validate_protection(protection)
            protection = _strip_dynamic_protection_metadata(protection)
        return compute_policy_digest({"rulesets": rulesets, "classic_protection": protection})

    def _eligible(self, facts: dict, pr: str, head: str, base: str, policy: dict) -> None:
        latest_reviews: dict[str, dict] = {}
        for item in facts["reviews"]:
            actor = item.get("user", {}).get("login") if isinstance(item.get("user"), dict) else None
            item_id = item.get("id") if isinstance(item, dict) else None
            if (not isinstance(actor, str) or type(item_id) is not int or item_id <= 0
                    or item.get("commit_id") != head):
                continue
            if item.get("state") in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
                if item_id >= latest_reviews.get(actor, {}).get("id", -1):
                    latest_reviews[actor] = item
        missing = {actor for actor in policy["required_reviewers"]
                   if latest_reviews.get(actor, {}).get("state") != "APPROVED"}
        if missing:
            raise PermissionError("required GitHub review missing")
        if policy["required_internal_reviews"]:
            from .internal_review import matching

            matching(self.store, pr=pr, head=head, base=base,
                     policy_digest=policy["review_policy_digest"],
                     requirements=policy["required_internal_reviews"])
        latest_checks: dict[str, dict] = {}
        for item in facts["checks"]:
            item_id = item.get("id") if isinstance(item, dict) else None
            name = item.get("name") if isinstance(item, dict) else None
            if not isinstance(name, str) or not name or type(item_id) is not int or item_id <= 0:
                raise PermissionError("GitHub check-run entry is malformed")
            if item_id >= latest_checks.get(name, {}).get("id", -1):
                latest_checks[name] = item
        if any(latest_checks.get(name, {}).get("status") != "completed"
               or latest_checks.get(name, {}).get("conclusion") != "success"
               for name in policy["required_checks"]):
            raise PermissionError("required GitHub check is not successful")

    def _intent(self, *, pr: str, owner: str, epoch: int, head: str, base: str) -> str:
        return digest({"pr": pr, "owner": owner, "epoch": epoch, "head": head, "base": base,
                       "policy": self.merge_policy, "actor": self.actor,
                       "account_ref": self.account_ref})

    def _audit_preflight_failure(self, intent: str, error: Exception) -> None:
        """Preserve read-only failure history without creating an external-effect intent."""
        value = {"intent": intent, "transport": self.transport_name,
                 "error_type": type(error).__name__, "error": str(error)}
        self.store.put_once("merge_preflight_failure", digest(value), value)

    def merge(self, *, pr: str, owner: str, epoch: int, head: str, base: str) -> dict:
        repo, number = parse_pr_identity(pr)
        if repo != self.merge_policy["repo"] or base != self.merge_policy["base"]:
            raise PermissionError("candidate does not match the controller-bound merge policy")
        resource = "pr:" + pr
        intent = self._intent(pr=pr, owner=owner, epoch=epoch, head=head, base=base)
        receipt = self.store.get("merge_receipt", intent)
        if receipt:
            return receipt
        if self.store.get("merge_intent", intent):
            observed = self.reconcile(intent)
            if observed["merged"]:
                return observed
            raise PermissionError("merge intent has an unresolved external effect; retry is prohibited")
        try:
            if self.store.ownership(resource) != (owner, epoch, "active"):
                raise PermissionError("merge ownership fence is stale")
            if self._live_policy_digest(repo) != self.merge_policy["live_policy_digest"]:
                raise PermissionError("live GitHub protection or rulesets drifted from the controller policy")
            self._eligible(self._read(repo, number, head, base), pr, head, base,
                           self.merge_policy)
        except Exception as error:
            self._audit_preflight_failure(intent, error)
            raise
        created = self.store.owned_operation(
            resource, owner, epoch, "merge_intent", intent,
            {"pr": pr, "head": head, "base": base, "policy": self.merge_policy,
             "actor": self.actor, "account_ref": self.account_ref,
             "transport": self.transport_name},
        )
        if not created:
            observed = self.reconcile(intent)
            if observed["merged"]:
                return observed
            raise PermissionError("merge intent has an unresolved external effect; retry is prohibited")
        response = self._request("PUT", f"/repos/{repo}/pulls/{number}/merge", json_data={"sha": head})
        ack_sha = response.get("sha") if isinstance(response, dict) else None
        if (response.get("merged") is not True or not isinstance(ack_sha, str)
                or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", ack_sha) is None):
            raise PermissionError("GitHub merge response did not confirm a merge commit")
        self.store.put_once("merge_ack", intent, {"merge_commit_sha": ack_sha})
        return self.reconcile(intent, require_merged=True)

    def reconcile(self, intent: str, *, require_merged: bool = False) -> dict:
        stored = self.store.get("merge_intent", intent)
        if not stored:
            raise ValueError("unknown merge intent")
        existing = self.store.get("merge_receipt", intent)
        if existing:
            return existing
        repo, number = parse_pr_identity(stored["pr"])
        pull = self._request("GET", f"/repos/{repo}/pulls/{number}")
        pull = pull if isinstance(pull, dict) else json.loads(pull)
        merged_by = (pull.get("merged_by") or {}).get("login") if isinstance(pull.get("merged_by"), dict) else None
        merge_sha = pull.get("merge_commit_sha")
        ack = self.store.get("merge_ack", intent)
        merged = (pull.get("merged") is True and isinstance(merge_sha, str)
                  and (pull.get("head") or {}).get("sha") == stored["head"]
                  and merged_by == stored["actor"]
                  and (ack is None or ack.get("merge_commit_sha") == merge_sha))
        if require_merged and not merged:
            raise PermissionError("merge acknowledgement was not confirmed by exact-head authenticated readback")
        receipt = {"intent": intent, "pr": stored["pr"], "head": stored["head"], "base": stored["base"],
                   "actor": stored["actor"], "transport": self.transport_name, "merged": bool(merged),
                   "configured_account_ref": stored["account_ref"],
                   "observed": True, "merge_commit_sha": merge_sha, "merged_by": merged_by}
        if merged:
            self.store.put_once("merge_receipt", intent, receipt)
        return receipt
