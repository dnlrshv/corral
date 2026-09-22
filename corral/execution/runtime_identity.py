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
    pid, expected = identity.get("pid"), identity.get("process_start")
    if not isinstance(pid, int) or not expected:
        return False
    return process_start(pid) == expected
