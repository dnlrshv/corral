"""Read-only filesystem and Git helpers for instruction linting."""
from __future__ import annotations

import os
from pathlib import Path
import stat
import subprocess


def git(root: Path, args: list[str], *, input: bytes | None = None, errors: list[str] | None = None):
    try:
        result = subprocess.run(['git', '-C', str(root), *args], input=input,
                                capture_output=True, check=False, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        result = None
    if result is None or (result.returncode not in (0, 1) and args[0] != 'rev-parse'):
        if errors is not None and 'git unavailable; reference checks may be unverifiable' not in errors:
            errors.append('git unavailable; reference checks may be unverifiable')
        return None
    return result


def is_git(root: Path, errors: list[str] | None = None) -> bool:
    result = git(root, ['rev-parse', '--is-inside-work-tree'], errors=errors)
    return result is not None and result.returncode == 0 and result.stdout.strip() == b'true'


def has_symlink(path: Path, root: Path) -> bool:
    """Probe components without traversing a symlink, including dangling links."""
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return True
    current = root
    for part in parts:
        current = current / part
        try:
            if stat.S_ISLNK(os.lstat(current).st_mode):
                return True
        except OSError:
            return False
    return False
