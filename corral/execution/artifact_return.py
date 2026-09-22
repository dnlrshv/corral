"""Digest-checked artifact return without overwriting existing developer changes."""
from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
from typing import Any


def write(fetched: dict[str, Any], destination: str | Path) -> Path:
    data = base64.b64decode(fetched["data"], validate=True)
    if hashlib.sha256(data).hexdigest() != fetched["digest"]:
        raise ValueError("artifact digest mismatch")
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                     fetched.get("mode") or 0o644)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
    except FileExistsError:
        if target.read_bytes() != data:
            raise PermissionError("destination exists with newer or different content")
    return target
