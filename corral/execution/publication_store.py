"""Atomic immutable intent outcomes and authenticated delivery receipts."""

import json
import time
from typing import Any


def record_outcome(
    store: Any,
    intent: str,
    resource: str,
    status: str,
    error: str | None = None,
    review_id: int | None = None,
    attempt_id: str | None = None,
    receipt: dict[str, Any] | None = None,
) -> None:
    if status not in ("ambiguous", "delivered"):
        raise ValueError("unsupported publication outcome")
    if status == "delivered" and (type(review_id) is not int or review_id <= 0):
        raise ValueError("positive authenticated review id required")
    with store.transaction() as db:
        row = db.execute(
            "SELECT resource,status,review_id,attempt_id FROM publication_intents WHERE intent=?",
            (intent,),
        ).fetchone()
        if not row or row[0] != resource:
            raise PermissionError("publication intent resource mismatch or missing")
        _, old_status, old_review, stored_attempt = row
        if attempt_id is not None and attempt_id != stored_attempt:
            raise PermissionError("publication attempt identity mismatch")
        if old_status == "delivered":
            if status != "delivered" or old_review != review_id:
                raise PermissionError("delivered outcome is immutable")
        existing = db.execute(
            "SELECT value FROM records WHERE kind='advisory_receipt' AND key=?",
            (intent,),
        ).fetchone()
        if receipt is not None:
            if (
                receipt.get("intent") != intent
                or "pr:" + receipt.get("pr", "") != resource
                or receipt.get("review_id") != review_id
            ):
                raise PermissionError("receipt binding mismatch")
            if (
                existing
                and json.loads(existing[0]).get("transport") == "synthetic_comment"
            ):
                db.execute(
                    "DELETE FROM records WHERE kind='advisory_receipt' AND key=?",
                    (intent,),
                )
                existing = None
            if existing:
                previous = json.loads(existing[0])
                ignored = {"candidate", "reconciled", "stale_reconciliation"}
                if {k: v for k, v in previous.items() if k not in ignored} != {
                    k: v for k, v in receipt.items() if k not in ignored
                }:
                    raise PermissionError("conflicting immutable delivery receipt")
            else:
                db.execute(
                    "INSERT INTO records VALUES('advisory_receipt',?,?)",
                    (
                        intent,
                        json.dumps(receipt, sort_keys=True, separators=(",", ":")),
                    ),
                )
        db.execute(
            "UPDATE publication_intents SET status=?,error=?,review_id=?,updated_at=? WHERE intent=?",
            (
                status,
                error,
                review_id if review_id is not None else old_review,
                time.time(),
                intent,
            ),
        )
        # A migrated intent with no attempt identity has no authority over any lease.
        if stored_attempt:
            db.execute(
                "UPDATE leases SET status=? WHERE resource=? AND attempt_id=?",
                (
                    "completed" if status == "delivered" else "ambiguous",
                    resource,
                    stored_attempt,
                ),
            )
        if receipt is not None:
            pending = {
                "pr": receipt["pr"],
                "intent": intent,
                "status": status,
                "review_id": review_id,
            }
            db.execute(
                "INSERT OR REPLACE INTO records VALUES('advisory_pending',?,?)",
                (intent, json.dumps(pending, sort_keys=True)),
            )


def record_absence(
    store: Any,
    intent: str,
    resource: str,
    *,
    attempt_id: str,
    prior_status: str,
    prior_updated_at: float,
    evidence: dict[str, Any],
) -> None:
    """Settle one unconfirmed attempt as never delivered after authenticated readback.

    Only the exact attempt that was read back settles, and only while it is still
    unresolved and unchanged, has no delivery receipt and no live lease holder. Its
    lease is released so the cohort can publish again; the immutable intent may be
    attempted again under a new attempt identity, and a later readback that finds
    the review still records it as delivered.
    """
    from .store import lease_holder_alive

    if prior_status not in ("pending", "ambiguous") or (prior_status == "pending" and not attempt_id):
        raise ValueError("only an ambiguous or identified pending attempt can be proven absent")
    with store.transaction() as db:
        row = db.execute(
            "SELECT resource,status,attempt_id,updated_at FROM publication_intents WHERE intent=?",
            (intent,),
        ).fetchone()
        if not row or row[0] != resource:
            raise PermissionError("publication intent resource mismatch or missing")
        if tuple(row[1:]) != (prior_status, attempt_id, prior_updated_at):
            raise PermissionError("publication attempt changed during absence readback")
        if db.execute(
            "SELECT 1 FROM records WHERE kind='advisory_receipt' AND key=?", (intent,)
        ).fetchone():
            raise PermissionError("a delivery receipt forbids an absence outcome")
        lease = db.execute(
            "SELECT attempt_id,status,holder_pid,acquired_at FROM leases WHERE resource=?",
            (resource,),
        ).fetchone()
        if (lease and lease[0] == attempt_id and lease[1] in ("in_flight", "active")
                and lease_holder_alive(lease[2], lease[3])):
            raise PermissionError("publication attempt holder is still alive")
        db.execute(
            "UPDATE publication_intents SET status='absent',updated_at=? WHERE intent=?",
            (time.time(), intent),
        )
        db.execute("DELETE FROM leases WHERE resource=? AND attempt_id=?", (resource, attempt_id))
        db.execute(
            "INSERT INTO records VALUES('advisory_absence',?,?)",
            (f"{intent}:{attempt_id}", json.dumps(evidence, sort_keys=True, separators=(",", ":"))),
        )
        pending = db.execute(
            "SELECT value FROM records WHERE kind='advisory_pending' AND key=?", (intent,)
        ).fetchone()
        if pending:
            db.execute(
                "UPDATE records SET value=? WHERE kind='advisory_pending' AND key=?",
                (json.dumps({**json.loads(pending[0]), "status": "absent"}, sort_keys=True), intent),
            )
