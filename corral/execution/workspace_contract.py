"""Controller-owned workspace provenance captured before a worker can start."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .store import digest
from .workspace import file_digest, safe_path

SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")

TRUSTED_EXPORT_FIELDS = (
    "export_id", "repository", "pr_number", "head", "base", "policy_id",
    "policy_digest", "workspace", "selected_files", "selected_files_digest",
    "diff_path", "diff_sha256", "export_digest", "auth_mode",
)


def _trusted_export(store, export_id: str) -> dict:
    if not isinstance(export_id, str) or not SHA256.fullmatch(export_id):
        raise PermissionError("trusted export id must be a SHA-256")
    value = store.get("trusted_export", export_id) if store is not None else None
    if not isinstance(value, dict) or any(key not in value for key in TRUSTED_EXPORT_FIELDS):
        raise PermissionError("trusted export is not registered by the controller")
    if value["export_id"] != export_id or digest(
            {key: item for key, item in value.items() if key != "export_id"}) != export_id:
        raise PermissionError("trusted export identity digest is invalid")
    if (not isinstance(value["repository"], str) or not value["repository"]
            or isinstance(value["pr_number"], bool) or not isinstance(value["pr_number"], int)
            or value["pr_number"] <= 0
            or not SHA.fullmatch(str(value["head"])) or not SHA.fullmatch(str(value["base"]))
            or not isinstance(value["policy_id"], str) or not value["policy_id"]
            or not SHA256.fullmatch(str(value["policy_digest"]))
            or not isinstance(value["auth_mode"], str) or not value["auth_mode"]):
        raise PermissionError("trusted export candidate binding is invalid")
    workspace = Path(str(value["workspace"]))
    if not workspace.is_absolute() or not workspace.is_dir():
        raise PermissionError("trusted export workspace is unavailable")
    files = value["selected_files"]
    if not isinstance(files, dict) or not files:
        raise PermissionError("trusted export selected_files must be a nonempty mapping")
    for name, entry in files.items():
        safe_path(workspace, name)
        if (not isinstance(entry, dict) or not SHA256.fullmatch(str(entry.get("digest") or ""))
                or (entry.get("mode") is not None
                    and (isinstance(entry["mode"], bool) or not isinstance(entry["mode"], int)))):
            raise PermissionError("trusted export file binding is invalid")
    if (value["selected_files_digest"] != digest(files)
            or value["export_digest"] != value["selected_files_digest"]
            or value["diff_path"] not in files
            or value["diff_sha256"] != files[value["diff_path"]]["digest"]):
        raise PermissionError("trusted export file digest binding is invalid")
    return value


def resolve_spec(store, spec: dict) -> dict:
    """Resolve an opaque export id into controller-owned review inputs."""
    export_id = spec.get("trusted_export_id")
    if export_id is None:
        return spec
    if spec.get("role", "review") != "review":
        raise PermissionError("trusted export tasks are inspection reviews")
    caller_owned = {
        "repo", "workspace", "workspace_kind", "snapshot_provenance", "candidate_paths",
        "inspection_paths", "inspection_diff_path", "inspection_base_ref", "inspection_pr",
    }
    supplied = sorted(caller_owned & spec.keys())
    if supplied:
        raise PermissionError("trusted export submission cannot override controller fields: "
                              + ", ".join(supplied))
    record = _trusted_export(store, export_id)
    diff_path = record["diff_path"]
    inspection_paths = sorted(name for name in record["selected_files"] if name != diff_path)
    if not inspection_paths:
        raise PermissionError("trusted export has no source document for inspection")
    return {**spec, "role": "review", "repo": record["repository"], "workspace": record["workspace"],
            "workspace_kind": "immutable_snapshot",
            "candidate_paths": sorted(record["selected_files"]),
            "inspection_paths": inspection_paths, "inspection_diff_path": diff_path,
            "inspection_pr": record["pr_number"], "tools": ["inspect-packet", "report"]}


def _candidate_files(root: Path, paths: list[str]) -> dict:
    files = {}
    for name in paths:
        path = safe_path(root, name)
        files[name] = {"digest": file_digest(path), "mode": path.stat().st_mode & 0o777 if path.exists() else None}
    return files


def preflight(spec: dict, workspace: str | Path, *, store=None) -> dict:
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
    elif kind == "immutable_snapshot" and spec.get("trusted_export_id") is not None:
        declared = _trusted_export(store, spec["trusted_export_id"])
        if root != Path(declared["workspace"]).resolve():
            raise PermissionError("trusted export workspace does not match the task")
        files = _candidate_files(root, list(declared["selected_files"]))
        if files != declared["selected_files"]:
            raise PermissionError("trusted export bytes changed before inference")
        value = {"kind": kind, "repo": declared["repository"], "head": declared["head"],
                 "base": declared["base"], "pr": declared["pr_number"],
                 "export_id": declared["export_id"], "export_digest": declared["export_digest"],
                 "trusted_export_id": declared["export_id"], "policy_id": declared["policy_id"],
                 "policy_digest": declared["policy_digest"], "auth_mode": declared["auth_mode"],
                 "files": files}
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
