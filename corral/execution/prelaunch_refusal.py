"""Crash-safe proof that a controller attempt ended before worker launch."""
from __future__ import annotations

import json
from pathlib import Path

from .store import digest


def _generation(value) -> int:
    try:
        return max(1, int((value or {}).get("generation") or 1))
    except (TypeError, ValueError):
        return 1


def _claim_key(task_id: str, generation: int) -> str:
    return task_id if generation <= 1 else f"{task_id}:g{generation}"


def _contains_identity(value, task_id: str, attempt: str) -> bool:
    if isinstance(value, dict):
        return any(_contains_identity(item, task_id, attempt) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_identity(item, task_id, attempt) for item in value)
    return value in {task_id, attempt}


def _record(db, kind: str, key: str):
    row = db.execute("SELECT value FROM records WHERE kind=? AND key=?", (kind, key)).fetchone()
    return json.loads(row[0]) if row else None


def prove_db(db, task_id: str, generation: int, resource: str) -> dict:
    """Prove one claimed attempt ended before any worker or external effect existed."""
    state = _record(db, "state", task_id)
    if (not isinstance(state, dict) or state.get("status") != "refused-before-launch"
            or _generation(state) != generation or state.get("process_status") is not None
            or any(state.get(key) for key in ("pid", "pgid", "worker_identity"))):
        raise PermissionError("refused attempt has no conclusive prelaunch process state")
    attempt = state.get("attempt")
    claim = _record(db, "claim", _claim_key(task_id, generation))
    invocation = _record(db, "invocation", attempt) if isinstance(attempt, str) else None
    allowed_invocation = {"task", "generation", "role", "selection", "observed", "usage"}
    if (not isinstance(attempt, str) or not attempt or claim != {
            "attempt": attempt, "generation": generation}
            or not isinstance(invocation, dict) or set(invocation) - allowed_invocation
            or invocation.get("task") != task_id or _generation(invocation) != generation
            or invocation.get("observed") is not None
            or invocation.get("usage") != "unknown-until-native-events"):
        raise PermissionError("refused attempt lacks its exact never-launched invocation claim")
    owner = db.execute(
        "SELECT owner,epoch,status FROM owners WHERE resource=?", (resource,)).fetchone()
    allocation = _record(db, "allocation", task_id)
    if (owner != (task_id, state.get("epoch"), "released")
            or not isinstance(allocation, dict) or allocation.get("active") is not False):
        raise PermissionError("refused attempt still retains workspace ownership or allocation")
    first_result = _record(db, "result", task_id)
    if (_record(db, "cancel", task_id) is not None
            or (first_result is not None and _generation(first_result) == generation)):
        raise PermissionError("refused attempt has cancellation or fabricated result state")
    if _record(db, "generation_result", f"{task_id}:{generation}") is not None:
        raise PermissionError("refused attempt has a generation result and needs reconciliation")
    for (raw,) in db.execute("SELECT value FROM records WHERE kind='usage'"):
        if _contains_identity(json.loads(raw), task_id, attempt):
            raise PermissionError("refused attempt has provider usage and was not prelaunch")
    for bound_attempt, raw in db.execute("SELECT attempt_id,payload FROM publication_intents"):
        if bound_attempt == attempt or _contains_identity(json.loads(raw), task_id, attempt):
            raise PermissionError("refused attempt has an unresolved external effect")
    for kind in ("repair_publish_intent", "merge_intent"):
        for (raw,) in db.execute("SELECT value FROM records WHERE kind=?", (kind,)):
            if _contains_identity(json.loads(raw), task_id, attempt):
                raise PermissionError("refused attempt has an unresolved external effect")
    core = {"schema": "corral-prelaunch-refusal-checkpoint-v1", "task": task_id,
            "generation": generation, "attempt": attempt, "status": state["status"],
            "accepted": None, "process": "never-launched", "result": "never-created",
            "refused_state": state, "state_digest": digest(state), "claim_digest": digest(claim),
            "invocation_digest": digest(invocation), "allocation_digest": digest(allocation),
            "workspace_owner": {"owner": owner[0], "epoch": owner[1], "status": owner[2]},
            "external_effects": "none-observed"}
    return {**core, "digest": digest(core)}


def prove(controller, task_id: str, generation: int, request: dict) -> dict:
    host = controller.hosts.get(request["host"]) or {}
    if host.get("executor"):
        raise PermissionError("remote prelaunch refusal continuation is not locally provable")
    resource = "workspace:" + str(Path(request["workspace"]).resolve())
    with controller.store.transaction() as db:
        return prove_db(db, task_id, generation, resource)
