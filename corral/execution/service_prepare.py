"""Atomic ownership for resumable service-side source preparation."""
from __future__ import annotations

import json
import time
import uuid

from .runtime_identity import process_status, observe
from .store import canonical


def claim(store, event_id: str, task_id: str, transfer_id: str) -> bool:
    candidate = {"claim_id": str(uuid.uuid4()), "task_id": task_id,
                 "transfer_id": transfer_id, "claimed_at": time.time(),
                 "process": observe()}
    with store.transaction() as db:
        row = db.execute("SELECT value FROM records WHERE kind='service_prepare_claim' AND key=?",
                         (event_id,)).fetchone()
        if row is None:
            db.execute("INSERT INTO records VALUES('service_prepare_claim',?,?)",
                       (event_id, canonical(candidate)))
            return True
        existing = json.loads(row[0])
        if ((existing.get("task_id"), existing.get("transfer_id"))
                != (task_id, transfer_id)):
            raise ValueError("source preparation identity changed")
        state = process_status(existing.get("process") or {})
        if state != "dead":
            return False
        db.execute("UPDATE records SET value=? WHERE kind='service_prepare_claim' AND key=?",
                   (canonical(candidate), event_id))
        return True
