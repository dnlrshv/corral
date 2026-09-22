"""Durable trusted-code coordination from model review through advisory and merge."""
from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from . import continuation
from .advisory import compute_advisory_intent
from .github_support import compute_canonical_wire_hash, parse_pr_identity
from .internal_review import load as load_review
from .internal_review import record as record_review
from .internal_review import report_body
from .policy import compute_policy_digest
from .store import canonical, digest, record_advisory_approval

_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


class PRCoordinator:
    """One controller-configured PR lane with exact owner and publication bindings."""

    def __init__(self, *, store, pr: str, owner_epoch: int, publisher: str,
                 campaign_authorization: str, publication_policy_snapshot: dict[str, Any],
                 publisher_account_ref: str):
        repo, _number = parse_pr_identity(pr)
        if (isinstance(owner_epoch, bool) or not isinstance(owner_epoch, int) or owner_epoch <= 0
                or not publisher or not campaign_authorization or not publisher_account_ref):
            raise PermissionError("PR coordinator authority binding is incomplete")
        snapshot = json.loads(json.dumps(publication_policy_snapshot))
        enforcement = snapshot.get("enforcement_contents") if isinstance(snapshot, dict) else None
        if (not isinstance(enforcement, dict) or snapshot.get("repo") != repo
                or enforcement.get("repo") != repo
                or not _SHA.fullmatch(str(snapshot.get("base_sha") or ""))
                or enforcement.get("base_sha") != snapshot["base_sha"]
                or compute_policy_digest(enforcement) != snapshot.get("enforcement_digest")):
            raise PermissionError("PR coordinator publication policy snapshot is invalid")
        self.store, self.pr, self.repo = store, pr, repo
        self.owner_epoch, self.publisher = owner_epoch, publisher
        self.publisher_account_ref = publisher_account_ref
        self.campaign_authorization = campaign_authorization
        self.policy_snapshot = snapshot

    def _owner(self) -> None:
        if self.store.ownership("pr:" + self.pr) != ("corral", self.owner_epoch, "active"):
            raise PermissionError("PR coordinator owner epoch is stale")

    def _job(self) -> dict[str, Any] | None:
        return self.store.get("pr_coordination", self.pr)

    def _write(self, value: dict[str, Any], expected: tuple[str | None, ...]) -> dict[str, Any]:
        """Advance one lane atomically while retaining immutable transition history."""
        self._owner()
        with self.store.transaction() as db:
            owner = db.execute(
                "SELECT owner,epoch,status FROM owners WHERE resource=?", ("pr:" + self.pr,)
            ).fetchone()
            if owner != ("corral", self.owner_epoch, "active"):
                raise PermissionError("PR coordinator owner epoch changed during transition")
            row = db.execute(
                "SELECT value FROM records WHERE kind='pr_coordination' AND key=?", (self.pr,)
            ).fetchone()
            prior = json.loads(row[0]) if row else None
            prior_stage = prior.get("stage") if prior else None
            if prior_stage not in expected:
                if prior == value:
                    return prior
                raise PermissionError(f"PR coordinator transition from {prior_stage!r} is invalid")
            db.execute("INSERT OR REPLACE INTO records VALUES('pr_coordination',?,?)",
                       (self.pr, canonical(value)))
            event = {"pr": self.pr, "prior": prior_stage, "stage": value["stage"],
                     "head": value.get("head"), "evidence": value.get("evidence")}
            sequence = db.execute(
                "SELECT COUNT(*) FROM records WHERE kind='pr_coordination_transition'"
            ).fetchone()[0] + 1
            db.execute("INSERT INTO records VALUES('pr_coordination_transition',?,?)",
                       (digest({**event, "sequence": sequence}), canonical(event)))
        return value

    def accept_review(self, task_id: str) -> dict[str, Any]:
        """Bind an accepted inspection result and select repair or publication in ordinary code."""
        receipt = record_review(self.store, task_id)
        if receipt["pr"] != self.pr or receipt["base"] != self.policy_snapshot["base_sha"]:
            raise PermissionError("review candidate differs from the configured PR lane")
        current = self._job()
        if current and current.get("head") == receipt["head"] \
                and current.get("review_receipt") == receipt["receipt_id"]:
            return current
        expected = (None,) if current is None else ("repair-linked",)
        if current is not None and current.get("replacement_export_id") != receipt["export_id"]:
            raise PermissionError("re-review does not match the controller-linked repair candidate")
        value = {"schema": "corral-pr-coordination-v1", "pr": self.pr,
                 "owner_epoch": self.owner_epoch, "stage": "reviewed",
                 "review_verdict": receipt["verdict"],
                 "head": receipt["head"], "base": receipt["base"],
                 "review_receipt": receipt["receipt_id"],
                 "evidence": ([*current["evidence"], receipt["receipt_id"]]
                              if current else [receipt["receipt_id"]])}
        return self._write(value, expected)

    def link_repair(self, *, repair_task: str, replacement_export_id: str) -> dict[str, Any]:
        """Bind a completed repair task to the exact changed candidate it produced."""
        current = self._job()
        if (not current or current.get("stage") != "advisory-delivered"
                or current.get("review_verdict") != "CHANGES_REQUIRED"):
            raise PermissionError("repair requires a delivered CHANGES_REQUIRED review")
        request = self.store.get("request", repair_task)
        state = self.store.get("state", repair_task)
        export = self.store.get("trusted_export", replacement_export_id)
        generation = continuation.generation_of(state)
        result = continuation.results(self.store, repair_task).get(generation)
        if (not isinstance(request, dict) or request.get("role") not in {"implementation", "repair"}
                or not isinstance(state, dict) or state.get("status") != "completed"
                or not isinstance(result, dict) or result.get("accepted") is not True
                or continuation.generation_of(result) != generation
                or not isinstance(export, dict) or export.get("export_id") != replacement_export_id
                or digest({key: value for key, value in export.items()
                           if key != "export_id"}) != replacement_export_id
                or f"{export.get('repository')}#{export.get('pr_number')}" != self.pr
                or export.get("head") == current["head"]
                or export.get("base") != self.policy_snapshot["base_sha"]):
            raise PermissionError("repair task or replacement candidate binding is incomplete")
        attempt = state.get("attempt")
        invocation = self.store.get("invocation", attempt) if isinstance(attempt, str) else None
        if (not isinstance(invocation, dict) or invocation.get("task") != repair_task
                or continuation.generation_of(invocation) != generation):
            raise PermissionError("repair invocation does not match its terminal result")
        candidate_paths = request.get("candidate_paths")
        receipt = result.get("receipt") or {}
        artifact = result.get("artifact_directory")
        if (not isinstance(candidate_paths, list) or not candidate_paths
                or not all(isinstance(name, str) and name for name in candidate_paths)
                or not isinstance(artifact, str)):
            raise PermissionError("repair result has no accepted candidate artifact binding")
        try:
            manifest = json.loads((Path(artifact) / "candidate_manifest.json").read_text())
        except (OSError, ValueError) as error:
            raise PermissionError("repair candidate manifest is unavailable") from error
        core = {"base": manifest.get("base"), "files": manifest.get("files")}
        files = core["files"]
        if (not isinstance(files, dict) or manifest.get("digest") != digest(core)
                or receipt.get("candidate_post") != manifest.get("digest")):
            raise PermissionError("repair candidate manifest is not bound to its verifier receipt")
        selected = export.get("selected_files")
        diff_path = export.get("diff_path")
        if (not isinstance(selected, dict) or not isinstance(diff_path, str)
                or diff_path not in selected
                or export.get("selected_files_digest") != digest(selected)
                or export.get("export_digest") != export.get("selected_files_digest")
                or export.get("diff_sha256") != selected[diff_path].get("digest")):
            raise PermissionError("replacement export has no selected file binding")
        for name in candidate_paths:
            item, exported = files.get(name), selected.get(name)
            if not isinstance(item, dict) or not isinstance(exported, dict):
                raise PermissionError("replacement export omits a repaired candidate path")
            data = item.get("data")
            try:
                raw = base64.b64decode(data, validate=True) if isinstance(data, str) else None
            except ValueError as error:
                raise PermissionError("repair candidate manifest contains invalid bytes") from error
            if (raw is None or hashlib.sha256(raw).hexdigest() != item.get("digest")
                    or exported.get("digest") != item.get("digest")):
                raise PermissionError("replacement export bytes differ from accepted repair output")
        evidence = [*current["evidence"], digest({"repair_task": repair_task,
                    "attempt": attempt, "generation": generation,
                    "candidate_manifest": manifest["digest"],
                    "replacement_export_id": replacement_export_id})]
        value = {**current, "stage": "repair-linked", "repair_task": repair_task,
                 "replacement_export_id": replacement_export_id, "evidence": evidence}
        return self._write(value, ("advisory-delivered",))

    def prepare_advisory(self) -> dict[str, Any]:
        """Derive the COMMENT body and approval from the accepted review and policy snapshot."""
        current = self._job()
        if not current or current.get("stage") not in {
            "reviewed", "advisory-prepared", "advisory-delivered", "merged"
        }:
            raise PermissionError("advisory requires a completed internal review")
        if current["stage"] != "reviewed":
            return current
        receipt = load_review(self.store, current["review_receipt"])
        body = report_body(self.store, receipt)
        policy = self.policy_snapshot["enforcement_digest"]
        intent, payload = compute_advisory_intent(
            self.repo, self.pr, receipt["head"], receipt["base"], policy,
            self.publisher, body, {})
        wire_hash, _wire = compute_canonical_wire_hash(payload["head"], body, [])
        self.store.put_once("advisory_payload", intent, payload)
        record_advisory_approval(
            self.store, repo=self.repo, pr=self.pr, head=receipt["head"],
            base=receipt["base"], intent=intent,
            authorized_by=self.campaign_authorization, policy=policy,
            epoch=self.owner_epoch, canonical_wire_hash=wire_hash,
            policy_snapshot=self.policy_snapshot, publisher=self.publisher)
        value = {**current, "stage": "advisory-prepared", "advisory_intent": intent,
                 "publisher_identity": {"configured_account_ref": self.publisher_account_ref,
                                        "authenticated_actor": None},
                 "evidence": [*current["evidence"], intent]}
        return self._write(value, ("reviewed", "advisory-prepared"))

    def publish_advisory(self, transport) -> dict[str, Any]:
        """Publish once, or read back an uncertain existing effect before any resend."""
        current = self.prepare_advisory()
        intent = current["advisory_intent"]
        payload = self.store.get("advisory_payload", intent)
        if current["stage"] in {"advisory-delivered", "merged"}:
            receipt = self.store.get("advisory_receipt", intent)
            if not isinstance(receipt, dict) or not transport.has_advisory(intent):
                raise PermissionError("recorded advisory delivery no longer validates")
            return receipt
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT status FROM publication_intents WHERE intent=?", (intent,)
            ).fetchone()
        if row and row[0] in {"pending", "ambiguous"}:
            receipt = transport.reconcile(self.pr, intent, payload)
        else:
            receipt = transport.advisory(
                self.pr, current["review_receipt"], intent, payload)
        if not transport.has_advisory(intent):
            raise PermissionError("advisory delivery lacks an authenticated durable receipt")
        value = {**current, "stage": "advisory-delivered",
                 "advisory_review_id": receipt["review_id"],
                 "publisher_identity": {"configured_account_ref": self.publisher_account_ref,
                                        "authenticated_actor": receipt["bridge_actor"]},
                 "evidence": [*current["evidence"], digest(receipt)]}
        self._write(value, ("advisory-prepared", "advisory-delivered"))
        return receipt


    def merge(self, transport) -> dict[str, Any]:
        """Run the merge transport only after exact-candidate advisory delivery."""
        current = self._job()
        if not current or current.get("stage") not in {"advisory-delivered", "merged"}:
            raise PermissionError("merge requires delivered advisory evidence")
        if current.get("review_verdict") != "PASS":
            raise PermissionError("merge requires an exact-candidate PASS internal review")
        if current.get("stage") == "merged":
            intent = current["merge_intent"]
            return transport.reconcile(intent, require_merged=True)
        receipt = transport.merge(pr=self.pr, owner="corral", epoch=self.owner_epoch,
                                  head=current["head"], base=current["base"])
        value = {**current, "stage": "merged", "merge_intent": receipt["intent"],
                 "evidence": [*current["evidence"], digest(receipt)]}
        self._write(value, ("advisory-delivered",))
        return receipt
