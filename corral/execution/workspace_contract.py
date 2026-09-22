"""Controller-owned workspace provenance captured before a worker can start."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .store import digest
from .workspace import file_digest, safe_path

SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _candidate_files(root: Path, paths: list[str]) -> dict:
    files = {}
    for name in paths:
        path = safe_path(root, name)
        files[name] = {"digest": file_digest(path), "mode": path.stat().st_mode & 0o777 if path.exists() else None}
    return files


def preflight(spec: dict, workspace: str | Path) -> dict:
    """Capture either real-checkout Git provenance or declared immutable export provenance.

    Snapshot mode never invokes Git: the controller records the provided source identity and
    its own candidate-file digests before the adapter/model receives any prompt.
    """
    root = Path(workspace).resolve()
    if not root.is_dir():
        raise PermissionError("workspace does not exist")
    kind = spec.get("workspace_kind", "checkout")
    paths = list(spec.get("candidate_paths") or [])
    if kind == "checkout":
        if not (root / ".git").exists():
            raise PermissionError("checkout workspace requires Git metadata before inference")
        completed = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                                   capture_output=True, text=True, check=False)
        head = completed.stdout.strip()
        if completed.returncode or not head:
            raise PermissionError("checkout workspace has no readable Git HEAD")
        value = {"kind": kind, "head": head, "files": _candidate_files(root, paths)}
    elif kind == "immutable_snapshot":
        declared = spec.get("snapshot_provenance")
        if not isinstance(declared, dict) or any(not isinstance(declared.get(key), str) or not declared[key]
                                                 for key in ("repo", "head", "base", "export_id", "export_digest")):
            raise PermissionError("immutable snapshot requires repo, head, base, export_id and export_digest provenance")
        if not SHA.fullmatch(declared["head"]) or not SHA.fullmatch(declared["base"]):
            raise PermissionError("immutable snapshot head and base must be full Git SHAs")
        if not SHA256.fullmatch(declared["export_digest"]):
            raise PermissionError("immutable snapshot export_digest must be a SHA-256")
        files = _candidate_files(root, paths)
        if declared["export_digest"] != digest(files):
            raise PermissionError("immutable snapshot export_digest does not bind controller-captured candidate files")
        value = {"kind": kind, "repo": declared["repo"], "head": declared["head"],
                 "base": declared["base"], "export_id": declared["export_id"],
                 "export_digest": declared["export_digest"],
                 "files": files}
    else:
        raise PermissionError("workspace_kind must be checkout or immutable_snapshot")
    return {**value, "digest": digest(value)}
