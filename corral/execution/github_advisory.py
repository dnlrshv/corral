"""Real GitHub advisory COMMENT transport with candidate-bound approval and ownership gating."""

from __future__ import annotations

import json
import os
import time
import urllib.request
import uuid
from typing import Any, Callable

from .github_support import (
    _NoRedirectHandler,
    execute_github_request,
    find_remote_matching_review,
    has_intent_provenance,
    parse_pr_identity,
    persist_delivery_receipt,
    verify_remote_candidate,
)
from .publication_store import record_absence
from .store import lease_holder_alive


class GitHubAdvisoryTransport:
    """GitHub advisory review COMMENT transport with candidate-bound approval and ownership gating."""

    transport_name = "github_advisory_comment"

    def __init__(
        self,
        *,
        http_client: Callable[..., Any] | None = None,
        bridge_token: str | None = None,
        bridge_actor: str | None = None,
        store: Any = None,
        allow_network: bool = False,
        authorized_bridge_actors: frozenset[str] | None = None,
        timeout: float = 20.0,
        policy_inputs: dict | None = None,
        absence_reads: int = 3,
        absence_quiet_seconds: float = 300.0,
    ):
        if absence_reads < 2:
            raise ValueError("absence proof requires at least two consistent readbacks")
        self.http_client = http_client
        self.bridge_token = bridge_token
        self.store = store
        self.allow_network = allow_network
        self.timeout = timeout
        self.policy_inputs = policy_inputs
        # A failed POST is proven absent only after this many consecutive readbacks,
        # and only once GitHub can no longer be processing a request sent that long ago.
        self.absence_reads = absence_reads
        self.absence_quiet_seconds = absence_quiet_seconds
        self.authorized_bridge_actors = (frozenset({"github-actions[bot]"})
                                         if authorized_bridge_actors is None
                                         else authorized_bridge_actors)
        self.candidates: dict[str, str] = {}
        self.advisory_calls: list[dict[str, Any]] = []
        self.validated_bridge_actor: str | None = None

        self._opener = urllib.request.build_opener(_NoRedirectHandler())

        if bridge_actor or bridge_token or http_client:
            self._validate_bridge_identity(expected_actor=bridge_actor)

    def _sanitize(self, msg: str) -> str:
        return (
            msg.replace(self.bridge_token, "[REDACTED_TOKEN]")
            if self.bridge_token and self.bridge_token in msg
            else msg
        )

    def _validate_bridge_identity(self, expected_actor: str | None = None) -> str:
        """Authenticate bridge actor against GET /user to prevent forged caller claims."""
        try:
            user_data = self._request("GET", "/user")
        except Exception as exc:
            raise PermissionError(
                f"failed to authenticate bridge identity: {self._sanitize(str(exc))}"
            ) from exc

        data = user_data if isinstance(user_data, dict) else json.loads(user_data)
        login = data.get("login")
        if not login or login not in self.authorized_bridge_actors:
            raise PermissionError(
                f"authenticated bridge login '{login}' is not in authorized bridge actors"
            )
        if expected_actor and login != expected_actor:
            raise PermissionError(
                f"bridge actor mismatch: expected '{expected_actor}', authenticated '{login}'"
            )
        self.validated_bridge_actor = login
        return login

    def _request(
        self, method: str, path: str, *, json_data: dict[str, Any] | None = None
    ) -> Any:
        return execute_github_request(
            opener=self._opener,
            http_client=self.http_client,
            headers=self._headers(),
            allow_network=self.allow_network,
            timeout=self.timeout,
            method=method,
            path=path,
            json_data=json_data,
            sanitize_fn=self._sanitize,
        )

    def _headers(self) -> dict[str, str]:
        h = {
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "Corral-Advisory-Transport",
        }
        if self.bridge_token:
            h["Authorization"] = f"Bearer {self.bridge_token}"
        return h

    def _parse_pr(self, pr: str) -> tuple[str, int]:
        return parse_pr_identity(pr)

    def _verify_candidate_remote(
        self, repo: str, number: int, expected_head: str, expected_base: str
    ) -> dict[str, Any]:
        return verify_remote_candidate(
            self._request, repo, number, expected_head, expected_base
        )

    def _verify_approval_and_ownership(
        self, pr: str, intent: str, payload: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        from .publication_validation import verify_approval

        if self.store is None:
            raise ValueError("durable store required")
        return verify_approval(self, pr, intent, payload)

    def _find_remote_matching_review(
        self,
        repo: str,
        number: int,
        head: str,
        expected_body: str,
        expected_comments: list[Any] | None = None,
    ) -> dict[str, Any] | None:
        return find_remote_matching_review(
            self._request,
            repo,
            number,
            head,
            expected_body,
            expected_comments,
            self.validated_bridge_actor,
        )

    def has_advisory(self, intent: str) -> bool:
        """Verify presence of a genuine delivered review receipt matching this real transport."""
        if self.store is None:
            return False
        receipt = self.store.get("advisory_receipt", intent)
        if not receipt or not isinstance(receipt, dict):
            return False
        if receipt.get("transport") != self.transport_name:
            return False
        if receipt.get("advisory") is not True:
            return False
        if receipt.get("intent") != intent:
            return False
        rev_id = receipt.get("review_id")
        if not isinstance(rev_id, int) or isinstance(rev_id, bool) or rev_id <= 0:
            return False
        if (
            self.validated_bridge_actor
            and receipt.get("bridge_actor") != self.validated_bridge_actor
        ):
            return False
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT resource,status,payload,review_id FROM publication_intents WHERE intent=?",
                (intent,),
            ).fetchone()
        if not row or row[1] != "delivered" or row[3] != rev_id:
            return False
        payload = json.loads(row[2])
        if not has_intent_provenance(
            self.store, intent, row[0], payload["head"], payload["base"], payload
        ):
            return False
        return all(
            receipt.get(k) == payload[k]
            for k in ("repo", "pr", "head", "base", "policy", "publisher")
        )

    def advisory(
        self,
        pr: str,
        expected: str,
        intent: str,
        payload: dict[str, Any],
        lose_ack: bool = False,
    ) -> dict[str, Any]:
        """POST once after approval, live policy and ownership validation.

        GitHub cannot atomically predicate a review POST on base/policy. Drift after
        POST retains an ambiguous intent; reconciliation can establish delivery of
        the old candidate but cannot claim that candidate is still current.
        """
        epoch, wire = self._verify_approval_and_ownership(pr, intent, payload)
        repo, number = self._parse_pr(pr)
        resource = "pr:" + pr
        with self.store.transaction() as db:
            prior = db.execute(
                "SELECT status FROM publication_intents WHERE intent=?", (intent,)
            ).fetchone()
        if prior:
            if prior[0] == "delivered" and self.has_advisory(intent):
                if not has_intent_provenance(
                    self.store,
                    intent,
                    resource,
                    payload["head"],
                    payload["base"],
                    payload,
                ):
                    raise PermissionError("delivered intent payload mismatch")
                return self.store.get("advisory_receipt", intent)
            # An attempt proven absent by authenticated readback may be attempted again;
            # the readback before POST below still refuses if a late review has landed.
            if prior[0] != "absent":
                raise PermissionError(
                    f"recorded {prior[0]} intent requires reconciliation; re-posting prohibited"
                )
        attempt_id = str(uuid.uuid4())
        acquired, reason, _, lease_epoch = self.store.acquire_lease(
            resource, "corral", payload["head"], os.getpid(), attempt_id
        )
        if not acquired or lease_epoch != epoch:
            raise PermissionError(f"cannot acquire publication lease: {reason}")
        try:
            matching = self._find_remote_matching_review(
                repo, number, payload["head"], payload["body"], payload.get("comments")
            )
            if matching:
                raise PermissionError(
                    "remote review exists without local attempted-payload provenance; reconciliation required"
                )
            # Remote reads can take time. Recheck the actual candidate/policy/actor
            # immediately before recording the immutable attempt and issuing POST.
            self._verify_approval_and_ownership(pr, intent, payload)
            self.store.record_intent_pending(
                intent,
                resource,
                "corral",
                epoch,
                payload["head"],
                payload["base"],
                payload,
                attempt_id,
            )
        except Exception:
            self.store.release_lease(resource, attempt_id)
            raise
        self.advisory_calls.append(
            {"pr": pr, "intent": intent, "expected": expected, "lose_ack": lose_ack}
        )
        observed = None
        try:
            observed = self._request(
                "POST", f"/repos/{repo}/pulls/{number}/reviews", json_data=wire
            )
            if lose_ack:
                raise ConnectionError("simulated lost advisory acknowledgement")
            from .github_support import validate_review_receipt

            validate_review_receipt(
                self._request,
                repo,
                number,
                observed,
                payload["head"],
                payload["body"],
                payload.get("comments"),
                self.validated_bridge_actor,
            )
            self._verify_approval_and_ownership(pr, intent, payload)
            receipt = self._receipt(
                pr, expected, intent, payload, observed, reconciled=False
            )
            persist_delivery_receipt(
                self.store, intent, resource, receipt, attempt_id=attempt_id
            )
            return receipt
        except Exception as exc:
            self.store.record_intent_outcome(
                intent,
                resource,
                "ambiguous",
                error=self._sanitize(str(exc)),
                attempt_id=attempt_id,
            )
            self.store.replace(
                "advisory_pending",
                intent,
                {
                    "pr": pr,
                    "intent": intent,
                    "candidate": expected,
                    "status": "ambiguous",
                    "head": payload["head"],
                    "base": payload["base"],
                    "error": self._sanitize(str(exc)),
                    "observed_review": observed,
                    "updated_at": time.time(),
                },
            )
            raise

    def _receipt(
        self,
        pr: str,
        candidate: str | None,
        intent: str,
        payload: dict[str, Any],
        review: dict[str, Any],
        *,
        reconciled: bool,
        is_stale: bool = False,
    ) -> dict[str, Any]:
        return {
            "pr": pr,
            "intent": intent,
            "candidate": candidate,
            "repo": payload["repo"],
            "head": payload["head"],
            "base": payload["base"],
            "policy": payload["policy"],
            "publisher": payload["publisher"],
            "bridge_actor": self.validated_bridge_actor,
            "transport": self.transport_name,
            "advisory": True,
            "reconciled": reconciled,
            "stale_reconciliation": is_stale,
            "delivered": True,
            "current_candidate_verified": not reconciled,
            "review_id": review["id"],
            "html_url": review.get("html_url"),
        }

    def reconcile(
        self, pr: str, intent: str, payload: dict[str, Any], is_stale: bool = False
    ) -> dict[str, Any]:
        """Reconcile the immutable attempted payload, including an old base/policy."""
        from .publication_validation import validate_payload

        validate_payload(pr, intent, payload)
        self._validate_bridge_identity(expected_actor=payload["publisher"])
        repo, number = self._parse_pr(pr)
        resource = "pr:" + pr
        if not has_intent_provenance(
            self.store, intent, resource, payload["head"], payload["base"], payload
        ):
            raise PermissionError(
                "missing exact immutable attempted-payload provenance"
            )
        if self.has_advisory(intent):
            return self.store.get("advisory_receipt", intent)
        attempt = self._absence_candidate(intent, resource)
        reads = 0
        while True:
            matching = self._find_remote_matching_review(
                repo, number, payload["head"], payload["body"], payload.get("comments")
            )
            if matching:
                receipt = self._receipt(
                    pr,
                    self.candidates.get(pr),
                    intent,
                    payload,
                    matching,
                    reconciled=True,
                    is_stale=is_stale,
                )
                persist_delivery_receipt(self.store, intent, resource, receipt)
                return receipt
            if not isinstance(attempt, dict):
                break
            if attempt["status"] == "absent":
                evidence = self.store.get(
                    "advisory_absence", f"{intent}:{attempt['attempt_id']}"
                )
                return self._absent_result(pr, intent, evidence, is_stale)
            try:
                self._verify_candidate_remote(
                    repo, number, payload["head"], payload["base"]
                )
            except PermissionError:
                attempt = "candidate-changed"
                break
            reads += 1
            if reads >= self.absence_reads:
                evidence = {
                    "intent": intent,
                    "resource": resource,
                    "attempt_id": attempt["attempt_id"],
                    "prior_status": attempt["status"],
                    "consistent_reads": reads,
                    "head": payload["head"],
                    "base": payload["base"],
                    "bridge_actor": self.validated_bridge_actor,
                    "settled_at": time.time(),
                }
                record_absence(
                    self.store,
                    intent,
                    resource,
                    attempt_id=attempt["attempt_id"],
                    prior_status=attempt["status"],
                    prior_updated_at=attempt["updated_at"],
                    evidence=evidence,
                )
                return self._absent_result(pr, intent, evidence, is_stale)
        return {
            "pr": pr,
            "intent": intent,
            "advisory": False,
            "reconciled": False,
            "delivered": "unknown",
            "uncertain": True,
            "status": "ambiguous_pending",
            "stale_reconciliation": is_stale,
            "absence_unproven": attempt,
        }

    def _absence_candidate(self, intent: str, resource: str) -> dict[str, Any] | str:
        """The attempt that authenticated readback may settle as absent, or why not.

        The attempt must no longer be in flight (its outcome was recorded ambiguous,
        or the lease holder of its pending attempt is gone) and be older than the
        quiet period during which GitHub could still be processing its POST. A
        migrated intent without an attempt identity settles only once ambiguous.
        """
        with self.store.transaction() as db:
            status, attempt_id, updated_at = db.execute(
                "SELECT status,attempt_id,updated_at FROM publication_intents WHERE intent=?",
                (intent,),
            ).fetchone()
            lease = db.execute(
                "SELECT attempt_id,status,holder_pid,acquired_at FROM leases WHERE resource=?",
                (resource,),
            ).fetchone()
        attempt = {"status": status, "attempt_id": attempt_id, "updated_at": updated_at}
        if status == "absent":
            return attempt
        if status not in ("pending", "ambiguous"):
            return f"intent-{status}"
        if status == "pending" and not attempt_id:
            return "attempt-identity-missing"
        if (status == "pending" and lease and lease[0] == attempt_id
                and lease[1] in ("in_flight", "active")
                and lease_holder_alive(lease[2], lease[3])):
            return "attempt-in-flight"
        if time.time() - updated_at < self.absence_quiet_seconds:
            return "quiet-period"
        return attempt

    def _absent_result(
        self, pr: str, intent: str, evidence: dict[str, Any] | None, is_stale: bool
    ) -> dict[str, Any]:
        return {
            "pr": pr,
            "intent": intent,
            "advisory": False,
            "reconciled": True,
            "delivered": False,
            "uncertain": False,
            "status": "absent",
            "stale_reconciliation": is_stale,
            "absence": evidence,
        }

    def reconcile_shared_authority(
        self,
        pr: str,
        intent: str,
        payload: dict[str, Any] | None = None,
        is_stale: bool = False,
    ) -> dict[str, Any]:
        if payload is None and self.store is not None:
            with self.store.transaction() as db:
                row = db.execute(
                    "SELECT payload FROM publication_intents WHERE intent=?", (intent,)
                ).fetchone()
            payload = json.loads(row[0]) if row else {}
        return self.reconcile(pr, intent, payload, is_stale=is_stale)

    def merge(
        self, pr: str, expected: str, intent: str, lose_ack: bool = False
    ) -> dict[str, Any]:
        raise PermissionError("advisory transport does not possess merge authority")
