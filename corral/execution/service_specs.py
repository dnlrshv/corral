"""Read execution properties from the controller's authoritative resolved request."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def request_spec(store, event: dict[str, Any], *, db=None) -> dict[str, Any]:
    """Keep admission identity separate from controller-resolved execution inputs.

    Claims pass their existing transaction so resource accounting and the claim
    observe the same state without opening a nested write transaction.
    """
    task_id = event.get("task_id")
    if not task_id:
        raise ValueError("service execution requires an admitted task")
    if db is None:
        spec = store.get("request", task_id)
    else:
        row = db.execute("SELECT value FROM records WHERE kind='request' AND key=?",
                         (task_id,)).fetchone()
        spec = json.loads(row[0]) if row else None
    if not isinstance(spec, dict):
        raise ValueError("service task has no authoritative request")
    workspace = spec.get("workspace")
    if not isinstance(workspace, str) or not Path(workspace).is_absolute():
        raise ValueError("service task has no resolved absolute workspace")
    if spec.get("host") != event.get("host"):
        raise PermissionError("service event host differs from authoritative request")
    return spec
