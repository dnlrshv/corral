"""Atomic JSON updates on the destination filesystem."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def write_json(path: Path, payload: dict | list) -> None:
    """Readers see either complete version of a controller/worker JSON message."""
    encoded = json.dumps(payload).encode("utf-8")
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
