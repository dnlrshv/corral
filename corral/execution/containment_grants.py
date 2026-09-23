"""Validation for narrowly re-opened native runtime write grants."""
from __future__ import annotations

import os
from pathlib import Path


def _real(item: str) -> Path:
    return Path(os.path.realpath(str(item)))


def _inside(child: Path, parent: Path) -> bool:
    return child == parent or parent in child.parents


def refuse_overlaps(boundary, *, scratch_root: str | None = None) -> list[str]:
    """Reject writable grants that reopen controller or account-state subtrees."""
    problems: list[str] = []
    deny = {_real(item) for item in tuple(boundary.deny) + tuple(boundary.deny_write)}
    scratch_parent = _real(scratch_root) if scratch_root else None
    runtime_dirs = {_real(item) for item in boundary.write_allow}
    runtime_files = {_real(item) for item in boundary.write_file_allow}
    grants = {_real(item) for item in boundary.allow}
    for raw in boundary.writable_roots():
        root = _real(raw)
        runtime = root in runtime_dirs
        if str(root) in ("/", str(Path.home())):
            problems.append(f"writable root is a whole-filesystem path: {root}")
        if runtime and not any(_inside(root, grant) for grant in grants):
            problems.append(f"runtime writable root lacks a declared read grant: {root}")
        for denied in deny:
            permitted = runtime and any(_inside(root, grant) for grant in grants)
            if _inside(root, denied) and not (scratch_parent and _inside(root, scratch_parent)) and not permitted:
                problems.append(f"writable root reopens a denied subtree: {root} under {denied}")
            if _inside(denied, root):
                problems.append(f"denied path is reachable for write from a writable root: {denied} under {root}")
    for file in runtime_files:
        if not any(_inside(file, grant) and file != grant for grant in grants):
            problems.append(f"runtime writable file lacks a declared parent read grant: {file}")
        for denied in deny:
            permitted = any(_inside(file, grant) and file != grant for grant in grants)
            if _inside(file, denied) and not permitted:
                problems.append(f"runtime writable file reopens a denied subtree: {file} under {denied}")
            if _inside(denied, file):
                problems.append(f"denied path is reachable for write from a file grant: {denied} under {file}")
    for granted in grants:
        matched = {denied for denied in deny if denied == granted}
        if not matched:
            problems.append(f"grant is not an exact denied root, so it reopens more than declared: {granted}")
        for denied in deny:
            if denied != granted and _inside(denied, granted):
                problems.append(f"grant reopens a denied subtree: {granted} contains {denied}")
        for raw_root in boundary.writable_roots():
            if _inside(granted, _real(raw_root)):
                problems.append(f"grant is inside a worker-writable root: {granted}")
    return sorted(set(problems))
