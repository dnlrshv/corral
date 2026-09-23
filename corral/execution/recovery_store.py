"""Fenced transactional settlement for execution attempts."""
import json


def _result_record(task, generation):
    return ("result", task) if generation <= 1 else ("generation_result", f"{task}:{generation}")


def finish_attempt(store, *, task, resource, epoch, state, owner_status=None, owner_from=("active",),
                   release_allocation=False, result=None, expected_state=None):
    """Persist one attempt's outcome and release exactly its fences in a single transaction.

    ``owner_status=None`` leaves workspace ownership untouched (an epoch this attempt did not
    newly acquire is never released by it). A result is written once per generation and a
    different result for the same generation is refused, as is a state that changed since
    ``expected_state`` was read; nothing is persisted when any fence fails.
    """
    from .store import canonical

    with store.transaction() as db:
        if expected_state is not None:
            current = db.execute("SELECT value FROM records WHERE kind='state' AND key=?", (task,)).fetchone()
            if not current or current[0] != canonical(expected_state):
                raise PermissionError("attempt state changed before finalization")
        if owner_status is not None:
            row = db.execute("SELECT owner,epoch,status FROM owners WHERE resource=?", (resource,)).fetchone()
            if row is None or row[:2] != (task, epoch) or row[2] not in owner_from:
                raise PermissionError("stale ownership fence")
            db.execute("UPDATE owners SET status=? WHERE resource=? AND owner=? AND epoch=?",
                       (owner_status, resource, task, epoch))
        if result is not None:
            kind, key = _result_record(task, int(state.get("generation") or 1))
            old = db.execute("SELECT value FROM records WHERE kind=? AND key=?", (kind, key)).fetchone()
            if old and old[0] != canonical(result):
                raise ValueError("conflicting attempt result")
            if not old:
                db.execute("INSERT INTO records VALUES(?,?,?)", (kind, key, canonical(result)))
        db.execute("INSERT OR REPLACE INTO records VALUES('state',?,?)", (task, canonical(state)))
        if release_allocation:
            allocation = db.execute("SELECT value FROM records WHERE kind='allocation' AND key=?",
                                    (task,)).fetchone()
            if allocation:
                db.execute("INSERT OR REPLACE INTO records VALUES('allocation',?,?)",
                           (task, canonical({**json.loads(allocation[0]), "active": False})))


def settle_cancelled(store, *, resource, owner, epoch, task, generation, result, audit, expected_state, state):
    """Atomically persist a cancelled result and release exactly its fenced resources."""
    from .store import canonical

    result_kind, result_key = _result_record(task, generation)
    with store.transaction() as db:
        owner_row = db.execute("SELECT owner,epoch,status FROM owners WHERE resource=?", (resource,)).fetchone()
        if owner_row != (owner, epoch, "uncertain"):
            raise PermissionError("cancelled reconciliation has a stale ownership fence")
        current = db.execute("SELECT value FROM records WHERE kind='state' AND key=?", (task,)).fetchone()
        if not current or current[0] != canonical(expected_state):
            raise PermissionError("cancelled reconciliation state changed during collection")
        if db.execute("SELECT value FROM records WHERE kind='cancel' AND key=?", (task,)).fetchone() is None:
            raise PermissionError("cancelled reconciliation requires a durable cancel marker")
        claim_key = task if generation <= 1 else f"{task}:g{generation}"
        claim = db.execute("SELECT value FROM records WHERE kind='claim' AND key=?", (claim_key,)).fetchone()
        invocation = db.execute("SELECT value FROM records WHERE kind='invocation' AND key=?",
                                (str(expected_state.get("attempt") or ""),)).fetchone()
        if not claim or not invocation:
            raise PermissionError("cancelled reconciliation lacks claim or invocation binding")
        claim_value, invocation_value = json.loads(claim[0]), json.loads(invocation[0])
        if (claim_value.get("attempt") != expected_state.get("attempt") or claim_value.get("generation") != generation
                or invocation_value.get("task") != task or invocation_value.get("generation") != generation
                or invocation_value.get("attempt") not in (None, expected_state.get("attempt"))):
            raise PermissionError("cancelled reconciliation claim or invocation binding changed")
        old = db.execute("SELECT value FROM records WHERE kind=? AND key=?", (result_kind, result_key)).fetchone()
        raw_result = canonical(result)
        if old and old[0] != raw_result:
            raise ValueError("conflicting cancelled result")
        if not old:
            db.execute("INSERT INTO records VALUES(?,?,?)", (result_kind, result_key, raw_result))
        audit_key = task + f":g{generation}"
        old_audit = db.execute("SELECT value FROM records WHERE kind='reconciliation' AND key=?", (audit_key,)).fetchone()
        raw_audit = canonical(audit)
        if old_audit and old_audit[0] != raw_audit:
            raise ValueError("conflicting cancellation reconciliation")
        if not old_audit:
            db.execute("INSERT INTO records VALUES('reconciliation',?,?)", (audit_key, raw_audit))
        db.execute("INSERT OR REPLACE INTO records VALUES('state',?,?)", (task, canonical(state)))
        allocation = db.execute("SELECT value FROM records WHERE kind='allocation' AND key=?", (task,)).fetchone()
        if allocation:
            value = json.loads(allocation[0])
            db.execute("INSERT OR REPLACE INTO records VALUES('allocation',?,?)",
                       (task, canonical({**value, "active": False})))
        db.execute("UPDATE owners SET status='released' WHERE resource=? AND owner=? AND epoch=?",
                   (resource, owner, epoch))
