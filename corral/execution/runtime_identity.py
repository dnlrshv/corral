"""Observable service identity and loaded-source pins for operational receipts."""
from __future__ import annotations

import hashlib
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import controller, service
from .store import digest


def process_start(pid: int) -> str | None:
    try:
        value = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], check=True,
                               text=True, capture_output=True).stdout.strip()
        return value or None
    except (OSError, subprocess.SubprocessError):
        return None


def _loaded(module: Any) -> dict[str, str]:
    path = Path(module.__file__).resolve()
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def observe() -> dict[str, Any]:
    pid = os.getpid()
    value = {
        "pid": pid, "process_start": process_start(pid),
        "executable": str(Path(sys.executable).resolve()), "host": socket.gethostname(),
        "loaded": {"service": _loaded(service), "controller": _loaded(controller)},
    }
    return {**value, "loaded_pin": digest(value["loaded"]),
            "coverage": "complete" if value["process_start"] else "process-start-unavailable"}


def launched(pid: int, executable: str, host: str) -> dict[str, Any]:
    start = process_start(pid)
    return {"pid": pid, "process_start": start, "executable": executable, "host": host,
            "coverage": "complete" if start else "process-start-unavailable"}


def alive(identity: dict[str, Any]) -> bool:
    return process_status(identity) == "alive"


def process_status(identity: dict[str, Any]) -> str:
    """Return alive, dead, or unknown without equating failed observation with death."""
    pid = identity.get("pid")
    if identity.get("os_started"):
        return _worker_process_status(identity)
    expected = identity.get("process_start")
    if not isinstance(pid, int) or not expected:
        return "unknown"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "dead"
    except (PermissionError, OSError):
        return "unknown"
    observed = process_start(pid)
    if observed is None:
        return "unknown"
    return "alive" if observed == expected else "dead"


def _worker_process_status(identity: dict[str, Any]) -> str:
    """Observe identities captured by ``Process`` without translating their fields."""
    from .process import _boot_identity, _os_process_identity

    pid = identity.get("pid")
    expected_start = identity.get("os_started")
    expected_boot = identity.get("boot_identity")
    if (not isinstance(pid, int) or not expected_start or not expected_boot
            or identity.get("birth_identity_observed") is not True):
        return "unknown"
    if identity.get("host") not in (None, socket.gethostname()):
        return "unknown"
    current_boot = _boot_identity()
    if current_boot is None:
        return "unknown"
    if current_boot != expected_boot:
        return "dead"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "dead"
    except (PermissionError, OSError):
        return "unknown"
    observed = _os_process_identity(pid).get("started")
    if observed is None:
        return "unknown"
    return "alive" if observed == expected_start else "dead"
