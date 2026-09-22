"""Load controller-owned provider variables from one private JSON file."""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path


def load(path: str | Path | None) -> dict[str, str] | None:
    """Return private variables without logging values; None preserves dev env behavior."""
    if path is None:
        return None
    target = Path(path)
    if not target.is_absolute() or target.is_symlink() or not target.is_file():
        raise PermissionError("secret_env must be an absolute regular non-symlink file")
    metadata = target.stat()
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o600:
        raise PermissionError("secret_env must be owned by the service user with mode 0600")
    value = json.loads(target.read_text())
    if (not isinstance(value, dict) or any(
            not isinstance(name, str) or not name or not name.replace("_", "").isalnum()
            or not isinstance(secret, str) or not secret for name, secret in value.items())):
        raise ValueError("secret_env must be a nonempty-string JSON mapping")
    return dict(value)


def select(names: tuple[str, ...], values: dict[str, str] | None) -> dict[str, str]:
    """Expose only route-declared names; None is the explicit development fallback."""
    source = os.environ if values is None else values
    return {name: source[name] for name in names if source.get(name)}
