"""Trusted verification policy: binding, isolated import and deception refusal.

A declared ``external_verifier`` flag is a claim, not trust. Trust comes from controller
configuration: the executed verifier must be an inline controller-authored command, a
workspace file that is declared (therefore write-denied to the worker and hashed before and
after), or a file under a host-declared verifier root. Verification always runs outside the
worker boundary with an isolated import environment, and the workspace is scanned for the
files that silently hijack a verifier (``conftest.py``, ``sitecustomize.py``, ``Makefile``).
"""
from __future__ import annotations

import os
import platform
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .workspace import manifest

DECEPTION_NAMES: frozenset[str] = frozenset({
    "conftest.py", "sitecustomize.py", "usercustomize.py", "pytest.ini", "tox.ini", "noxfile.py",
    "setup.cfg", "pyproject.toml", "makefile", "gnuMakefile", "venv.cfg", ".pth",
    "conftest.mjs", "jest.config.js", "jest.setup.js", "setup.py",
})

ISOLATED_ENV = {
    "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin",
    "PYTHONNOUSERSITE": "1",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    "LC_ALL": "C",
}

_FLAG_WITH_VALUE = {"-c", "-m", "-W", "-X", "--module"}


@dataclass(frozen=True)
class Policy:
    kind: str
    argv: tuple[str, ...]
    script: str | None
    verifier_paths: tuple[str, ...]
    verifier_root: str | None
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {"kind": self.kind, "argv": list(self.argv), "script": self.script,
                "verifier_paths": list(self.verifier_paths), "verifier_root": self.verifier_root,
                "notes": list(self.notes),
                "deps_binding": "declared-paths+deception-scan; transitive imports not enumerated"}


def _script_position(argv: list[str]) -> str | None:
    """Return the first argv entry that is executed as code, skipping flag values."""
    index = 1
    while index < len(argv):
        item = argv[index]
        if item in _FLAG_WITH_VALUE:
            index += 2
            continue
        if item.startswith("-"):
            index += 1
            continue
        return item
    return None


def _is_deception(path: Path) -> bool:
    name = path.name.lower()
    return name in {entry.lower() for entry in DECEPTION_NAMES} or path.suffix == ".pth"


def scan_deception(workspace: str | Path, declared: tuple[str, ...] = ()) -> list[str]:
    """Report workspace files that could hijack a verifier and are not declared."""
    root = Path(workspace).resolve()
    declared_names = {Path(item).name.lower() for item in declared}
    offenders = []
    roots = [root]
    roots += [path.parent for path in root.glob("*") if path.is_dir() and path.name not in (".git",)]
    for directory in roots:
        try:
            entries = list(directory.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_file() and _is_deception(entry) and entry.name.lower() not in declared_names:
                offenders.append(str(entry.relative_to(root)))
    return sorted(set(offenders))


def _outside_worker_reach(path: Path, workspace: Path, worker_writable: tuple[str, ...]) -> bool:
    resolved = Path(os.path.realpath(str(path)))
    if resolved == workspace or workspace in resolved.parents:
        return False
    for writable in worker_writable:
        root = Path(writable).resolve()
        if resolved == root or root in resolved.parents:
            return False
    return True


def policy(spec: dict, workspace: str | Path, *, host: dict | None = None,
           worker_writable: tuple[str, ...] = ()) -> Policy:
    """Classify and validate the verifier, failing closed on any unbound execution path."""
    argv = spec.get("verify")
    if not argv:
        raise PermissionError("task declares no verification command")
    if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
        raise PermissionError("verification command must be a list of strings")
    root = Path(workspace).resolve()
    executable = Path(argv[0])
    if not executable.is_absolute():
        raise PermissionError("verification executable must be an absolute path outside worker reach")
    if not _outside_worker_reach(executable, root, worker_writable):
        raise PermissionError("verification executable must not live in a worker-writable path")
    declared = tuple(spec.get("verifier_paths") or ())
    script = _script_position(list(argv))
    notes: list[str] = []
    host_roots = tuple(str(item) for item in ((host or {}).get("verifier_roots") or ()))
    if spec.get("external_verifier") and not host_roots and script is not None:
        raise PermissionError(
            "declared external_verifier requires a controller-owned verifier root; the flag alone is not trust")

    if script is None:
        notes.append("inline controller-authored verification command; no workspace code executed")
        return Policy(kind="inline", argv=tuple(argv), script=None, verifier_paths=declared,
                      verifier_root=None, notes=tuple(notes))

    script_path = Path(script)
    if script_path.is_absolute():
        resolved = Path(os.path.realpath(str(script_path)))
        owning_root = next((item for item in host_roots
                            if resolved == Path(item).resolve()
                            or Path(item).resolve() in resolved.parents), None)
        if owning_root is None:
            if not _outside_worker_reach(resolved, root, worker_writable):
                raise PermissionError("absolute verifier script is inside a worker-writable path")
            raise PermissionError(
                "absolute verifier script is not under a controller-declared verifier root")
        if not resolved.is_file():
            raise PermissionError(f"external verifier script does not exist: {resolved}")
        notes.append(f"external verifier bound to controller-owned root {owning_root}")
        return Policy(kind="external", argv=tuple(argv), script=str(resolved), verifier_paths=declared,
                      verifier_root=owning_root, notes=tuple(notes))

    target = root / script_path
    if script_path.name not in {Path(item).name for item in declared} and str(script_path) not in declared:
        raise PermissionError(
            f"verification executes workspace file {script} that is not declared in verifier_paths")
    offenders = scan_deception(root, declared)
    if offenders:
        raise PermissionError("undeclared verifier-hijack files present in workspace: " + ", ".join(offenders))
    notes.append("workspace verifier is declared, write-denied to the worker and hashed before/after")
    return Policy(kind="bound-workspace", argv=tuple(argv), script=str(target), verifier_paths=declared,
                  verifier_root=None, notes=tuple(notes))


def bundle_digests(policy_kind: str, workspace: str | Path, declared: tuple[str, ...],
                   verifier_root: str | None) -> dict[str, str | None]:
    """Digest the verifier bundle actually bound by this policy."""
    if policy_kind == "bound-workspace":
        root = Path(workspace)
        return {name: _digest(root / name) for name in declared}
    if policy_kind == "external" and verifier_root:
        root = Path(verifier_root).resolve()
        bundle = {}
        for path in sorted(root.rglob("*")):
            if path.is_file():
                bundle[str(path.relative_to(root))] = _digest(path)
        return bundle
    return {}


def bound_bundle(policy: Policy, workspace: str | Path) -> dict:
    """Digest exactly what this policy executes, in one shape for pre- and post-comparison.

    An external policy binds its whole controller-owned root; a bound-workspace policy binds
    the declared paths; an inline controller-authored command binds no file at all.
    """
    return bundle_digests(policy.kind, workspace, policy.verifier_paths, policy.verifier_root)


def _digest(path: Path) -> str | None:
    import hashlib

    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


@dataclass
class Receipt:
    payload: dict = field(default_factory=dict)
    stdout: bytes = b""
    stderr: bytes = b""


def execute(policy: Policy, workspace: str | Path, *, candidate_paths: list[str], task: str,
            attempt: str, pre_verifier_manifest: dict | None, workspace_provenance: dict | None = None) -> Receipt:
    """Run the bound verifier outside the worker boundary and retain pre/post digests."""
    root = Path(workspace).resolve()
    candidate_pre = manifest(root, candidate_paths, workspace_provenance)
    offenders = scan_deception(root, policy.verifier_paths)
    bundle = bound_bundle(policy, root)
    # Compared against the same function's pre-dispatch value, so an external root swap and a
    # declared workspace verifier edit are both caught. A vacuous comparison is not a guard:
    # when no bundle is bound, integrity is unmeasured and is reported as None, never True.
    verifier_intact = None if pre_verifier_manifest is None else (pre_verifier_manifest == bundle)
    base = {"task": task, "attempt": attempt, "command": list(policy.argv),
            "candidate_pre": candidate_pre["digest"], "candidate": candidate_pre["digest"],
            "base": candidate_pre["base"], "environment": platform.platform(),
            "observed_os_host": platform.node(), "verifier_intact": verifier_intact,
            "policy": policy.as_dict(), "verifier_bundle": bundle,
            "import_isolation": "PYTHONSAFEPATH; no cwd import; PYTHONPATH scrubbed",
            "authority": "controller-configured-verifier", "exit_code": None, "policy_ok": True,
            "deception_scan": {"offenders": [], "declared": list(policy.verifier_paths)}}
    if offenders:
        base.update({"policy_ok": False, "unchanged": True,
                     "refused": "undeclared verifier-hijack files appeared during execution",
                     "deception_scan": {"offenders": offenders, "declared": list(policy.verifier_paths)}})
        return Receipt(payload=base)
    if verifier_intact is False:
        # Only a measured mismatch refuses. None means nothing was bound to measure.
        base.update({"policy_ok": False, "unchanged": True,
                     "refused": "declared verifier bundle changed during execution"})
        return Receipt(payload=base)
    env = dict(ISOLATED_ENV)
    if policy.kind == "external":
        # Controller-owned verifiers never import from the worker-writable cwd.
        env["PYTHONSAFEPATH"] = "1"
    else:
        # Inline/bound verifiers legitimately import candidate modules from the workspace;
        # the hijack vectors are closed by the deception scan and the env scrub instead.
        base["import_isolation"] = "cwd-importable; deception-scanned; PYTHONPATH scrubbed"
    for name in ("TMPDIR", "HOME"):
        if os.environ.get(name):
            env[name] = os.environ[name]
    env.pop("PYTHONPATH", None)
    completed = subprocess.run(list(policy.argv), cwd=str(root), capture_output=True, env=env)
    candidate_post = manifest(root, candidate_paths, workspace_provenance)
    base.update({"exit_code": completed.returncode, "candidate_post": candidate_post["digest"],
                 "unchanged": candidate_pre["digest"] == candidate_post["digest"]})
    return Receipt(payload=base, stdout=completed.stdout, stderr=completed.stderr)
