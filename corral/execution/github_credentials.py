"""Controller-configured GitHub credential loading without persisting token values."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any


def load(config: dict[str, Any]) -> tuple[str, dict[str, str]]:
    """Load one credential through an explicit protected-config mode."""
    if not isinstance(config, dict):
        raise PermissionError("GitHub credential loader config is missing")
    mode = config.get("mode")
    account_ref = config.get("account_ref")
    if not isinstance(account_ref, str) or not account_ref:
        raise PermissionError("GitHub credential account reference is required")
    if mode == "secret-env":
        name = config.get("env")
        if not isinstance(name, str) or not name:
            raise PermissionError("GitHub credential environment name is required")
        token = os.environ.get(name)
        if not token:
            raise PermissionError("GitHub credential environment is unavailable")
        return token, {"mode": mode, "account_ref": account_ref}
    if mode == "gh-cli":
        executable = Path(str(config.get("executable") or ""))
        hostname = config.get("hostname", "github.com")
        if (not executable.is_absolute() or not executable.is_file()
                or not os.access(executable, os.X_OK)):
            raise PermissionError("trusted gh executable must be an absolute executable")
        if not isinstance(hostname, str) or not hostname or hostname.startswith("-"):
            raise PermissionError("GitHub credential hostname is invalid")
        result = subprocess.run(
            [str(executable), "auth", "token", "--hostname", hostname],
            capture_output=True, text=True, check=False)
        token = result.stdout.strip() if result.returncode == 0 else ""
        if not token:
            raise PermissionError("configured gh account did not provide a token")
        return token, {"mode": mode, "account_ref": account_ref, "hostname": hostname}
    raise PermissionError("unsupported GitHub credential loader mode")
