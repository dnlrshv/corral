"""Content-bound input/result transfer with explicit paths and no checkout sync."""
import base64
import hashlib
import os
import stat
import subprocess
from pathlib import Path

from corral.redaction import check_file_text_safe

from .repair_objects import (_HARDENING, _METADATA_LIMIT, LOCAL_TIMEOUT_SECONDS, _read_regular,
                             git_environment)
from .store import digest

#: Overrides for every Git call in a worker-writable checkout: the repair hardening (no hooks,
#: fsmonitor, external diff, user-level attributes or maintenance), never sign, and never start
#: a transport (a repository could otherwise lazily fetch objects through a command it names).
CHECKOUT_GIT_HARDENING = (*_HARDENING, "-c", "commit.gpgSign=false", "-c", "protocol.allow=never")


def _run_checkout_git(root: Path, git_dir: Path, args, *, input=None, env=None,
                      what: str = "checkout") -> str:
    command = ["git", *CHECKOUT_GIT_HARDENING, "--git-dir=" + str(git_dir),
               "--work-tree=" + str(root), *args]
    try:
        result = subprocess.run(command, input=input, capture_output=True, check=False, cwd=root,
                                env=git_environment(env), timeout=LOCAL_TIMEOUT_SECONDS,
                                **({"stdin": subprocess.DEVNULL} if input is None else {}))
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{what} Git operation timed out: {args[0]}") from None
    if result.returncode:
        raise RuntimeError(f"{what} Git operation failed: {args[0]}")
    return result.stdout.decode()


def checkout_git_dir(root) -> Path:
    """The checkout's own repository directory, never a redirect to another repository.

    ``<root>/.git`` is worker-writable, and Git follows a gitfile, a symlink or a
    ``commondir`` file there to any repository. The directory is resolved with hardened
    ``rev-parse --absolute-git-dir``, which reads configuration but runs nothing it names, and
    is accepted only as one of two things:

    * the checkout's own ``.git`` directory, not a symlink and with no separate common
      directory;
    * the linked-worktree directory ``<common>/worktrees/<name>`` named by a ``.git`` gitfile,
      where that directory's ``gitdir`` file names this checkout's ``.git``. That back-link is
      how the repository registered the checkout as its worktree, and a worker cannot plant it
      in a repository outside its reach.

    In either case no symlink may stand in for ``HEAD``, ``packed-refs``, the index or the
    object store, or anywhere under the refs and reflogs, so Git cannot read or write another
    repository's refs through one. Anything else raises ``PermissionError``.
    """
    root = Path(root).resolve()
    dot_git = root / ".git"
    try:
        mode = os.lstat(dot_git).st_mode
    except OSError:
        raise PermissionError("checkout has no Git metadata of its own") from None
    if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
        raise PermissionError("checkout .git is a symlink or special file; refusing a redirect")
    try:
        lines = _run_checkout_git(root, dot_git, ("rev-parse", "--absolute-git-dir",
                                                  "--path-format=absolute", "--git-common-dir")
                                  ).splitlines()
    except RuntimeError as error:
        raise PermissionError("checkout Git directory is unreadable") from error
    if len(lines) != 2:
        raise PermissionError("checkout Git directory is unreadable")
    git_dir, common = (Path(os.path.realpath(line)) for line in lines)
    if stat.S_ISDIR(mode):
        if git_dir != dot_git or common != dot_git:
            raise PermissionError("checkout .git redirects to another repository")
    else:
        back = _read_regular(git_dir / "gitdir", _METADATA_LIMIT)
        named = back.decode(errors="replace").strip() if back is not None else ""
        # Git may record the back-link relative to the worktree directory itself.
        if (git_dir.parent != common / "worktrees" or not named
                or Path(os.path.realpath(os.path.join(git_dir, named))) != dot_git):
            raise PermissionError("checkout .git redirects to a repository that did not "
                                  "register this checkout as its worktree")
    for base in dict.fromkeys((git_dir, common)):
        if any(os.path.islink(base / name) for name in ("HEAD", "packed-refs", "index", "objects")):
            raise PermissionError("checkout Git metadata holds a symlink; refusing a redirect")
        for name in ("refs", "logs"):
            if _holds_symlink(base / name):
                raise PermissionError("checkout Git metadata holds a symlink; refusing a redirect")
    return git_dir


def _holds_symlink(path: Path) -> bool:
    """Whether ``path`` or anything beneath it is a symlink; a missing path holds none."""
    if os.path.islink(path):
        return True
    try:
        entries = list(os.scandir(path))
    except (FileNotFoundError, NotADirectoryError):
        return False
    return any(entry.is_symlink() or (entry.is_dir(follow_symlinks=False)
                                      and _holds_symlink(Path(entry.path)))
               for entry in entries)


def checkout_git(root, *args, input=None, env=None, what: str = "checkout") -> str:
    """Run one hardened Git command against the checkout's own repository (see above)."""
    root = Path(root).resolve()
    return _run_checkout_git(root, checkout_git_dir(root), args, input=input, env=env, what=what)


def checkout_head(root) -> str:
    """The commit the checkout at ``root`` has checked out, read with hardened Git."""
    return checkout_git(root, "rev-parse", "--verify", "--end-of-options", "HEAD^{commit}").strip()


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
        base = checkout_head(root)
    files = {}
    for name in paths:
        path = safe_path(root, name)
        data = path.read_bytes() if path.exists() else None
        if data and check_file_text_safe(
            data.decode("utf-8", errors="ignore"), source_name=name
        ):
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
        if data and check_file_text_safe(
            data.decode("utf-8", errors="ignore"), source_name=name
        ):
            raise PermissionError("credential-shaped input refused")
        decoded.append((name, data, value["mode"]))
    for name, data, mode in decoded:
        safe_write_manifest_file(root, name, data, mode)
    return manifest(root, list(incoming["files"]))
