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
