"""Content-bound input/result transfer with explicit paths and no checkout sync."""
import base64
import hashlib
import os
import subprocess
from pathlib import Path

from .store import digest


def file_digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def safe_path(root, name):
    relative = Path(name)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ValueError("unsafe relative path")
    if any(p in (".git", ".ssh", ".env") or p.startswith(".env.")
           or p.endswith((".pem", ".key")) for p in relative.parts):
        raise PermissionError("protected input path")
    target = Path(root).resolve() / relative
    if any(p.is_symlink() for p in (target, *target.parents) if p != Path(root).resolve()):
        raise PermissionError("symlink transfer refused")
    if not target.resolve().is_relative_to(Path(root).resolve()):
        raise PermissionError("path escapes workspace")
    return target


def manifest(root, paths, workspace_provenance=None):
    root = Path(root)
    if workspace_provenance and workspace_provenance.get("kind") == "immutable_snapshot":
        base = workspace_provenance["head"]
    else:
        base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    files = {}
    for name in paths:
        path = safe_path(root, name)
        data = path.read_bytes() if path.exists() else None
        if data and (b"-----BEGIN PRIVATE KEY" in data or b"ghp_" in data):
            raise PermissionError("credential-shaped input refused")
        files[name] = {"digest": file_digest(path),
                       "data": base64.b64encode(data).decode() if data is not None else None,
                       "mode": path.stat().st_mode & 0o777 if data is not None else None}
    value = {"base": base, "files": files}
    return {**value, "digest": digest(value)}


def safe_write_manifest_file(root, name, data, mode):
    root_path = Path(root).resolve()
    rel = Path(name)
    root_fd = os.open(root_path, os.O_RDONLY | os.O_DIRECTORY)
    curr_fd = root_fd
    fds_to_close = []
    try:
        for component in rel.parts[:-1]:
            try:
                os.mkdir(component, 0o755, dir_fd=curr_fd)
            except FileExistsError:
                pass
            next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=curr_fd)
            fds_to_close.append(next_fd)
            curr_fd = next_fd
        filename = rel.parts[-1]
        if data is None:
            try:
                os.unlink(filename, dir_fd=curr_fd)
            except FileNotFoundError:
                pass
        else:
            temp_name = filename + f".corral-transfer-{os.getpid()}"
            try:
                os.unlink(temp_name, dir_fd=curr_fd)
            except FileNotFoundError:
                pass
            fd = os.open(temp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=curr_fd)
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.rename(temp_name, filename, src_dir_fd=curr_fd, dst_dir_fd=curr_fd)
    finally:
        for fd in reversed(fds_to_close):
            os.close(fd)
        os.close(root_fd)


def apply_manifest(root, incoming, expected):
    """Validate every target before any write; caller owns exclusive workspace."""
    core = {"base": incoming["base"], "files": incoming["files"]}
    if digest(core) != incoming["digest"]:
        raise ValueError("input manifest digest mismatch")
    current = manifest(root, list(incoming["files"]))
    if current["digest"] != expected["digest"] or current["base"] != incoming["base"]:
        raise PermissionError("checkout changed; refuse overwrite")
    decoded = []
    for name, value in incoming["files"].items():
        safe_path(root, name)
        data = base64.b64decode(value["data"], validate=True) if value["data"] is not None else None
        if data is not None and hashlib.sha256(data).hexdigest() != value["digest"]:
            raise ValueError("file digest mismatch")
        decoded.append((name, data, value["mode"]))
    for name, data, mode in decoded:
        safe_write_manifest_file(root, name, data, mode)
    return manifest(root, list(incoming["files"]))
