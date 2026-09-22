"""Ephemeral worker containment: Seatbelt profile generation plus a real launch probe.

The native model worker never runs as a trusted process. This module owns the boundary
description (what the worker may write, what it may never read or write) and proves the
boundary by actually launching a probe child under ``sandbox-exec`` before any harness
starts. A missing or partially failing boundary is a hard refusal, never a downgrade to
"private directory" isolation.

Proof discipline, required because a missing path proves nothing:

* denial checks only pass on ``EPERM``/``EACCES``; ``ENOENT``, ``ENOTDIR`` or a missing
  tool are inconclusive and fail the demonstration closed;
* denial targets are controller-created disposable sentinels that already exist, so no
  real credential file is ever opened and no probe file is planted in trusted state;
* sentinel digests are compared before/after the probe, so a mutated sentinel fails;
* the receipt states exactly which operations are contained (file read/write) and which
  are not (same-UID signals, Mach IPC, detached descendants, host reboot).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# Credential/account stores a coding worker never needs, whatever route it runs.
SENSITIVE_HOME_ENTRIES: tuple[str, ...] = (
    ".ssh",
    ".aws",
    ".gnupg",
    ".kube",
    ".docker",
    ".azure",
    ".aliyun",
    ".config/gcloud",
    ".config/gh",
    ".local/share/keyrings",
    ".npmrc",
    ".netrc",
    ".pypirc",
    ".git-credentials",
    ".gemini",
    ".antigravity",
    ".qwen",
    ".alos_env",
    ".codex/auth.json",
    ".codex/sessions",
    ".codex/memories",
    ".codex/history.jsonl",
)

SENSITIVE_HOME_LIBRARY: tuple[str, ...] = (
    "Library/Keychains",
    "Library/Application Support/Google/Antigravity",
)

# Repository-local secret files are denied by name pattern, wherever the workspace lives.
SECRET_FILE_DENY_REGEXES: tuple[str, ...] = (
    r"^/.*/\.env$",
    r"^/.*/\.env\.[^/]+$",
    r"^/.*/\.aos_env$",
    r"^/.*/[^/]+\.(pem|key|p12|pfx)$",
)
# Conventional non-secret templates stay usable by a coding worker.
SECRET_FILE_ALLOW_REGEXES: tuple[str, ...] = (
    r"^/.*/\.env\.(example|sample|template)$",
)

PROBE_NAME = ".corral-containment-probe"
CONTAINED_OPERATIONS: tuple[str, ...] = ("file-read*", "file-write*")
NOT_CONTAINED: tuple[str, ...] = (
    "signal delivery to same-UID processes (controller included)",
    "mach IPC and shared memory with same-UID processes",
    "detached descendants that outlive the process group",
    "host reboot / persistent host state",
    "network egress (allowed by policy so the harness can reach its provider)",
)
ISOLATION_CLAIM = ("scoped per-task file boundary enforced by ephemeral macOS Seatbelt; "
                   "not full OS isolation and not a hostile-same-UID kernel boundary")

_PROBE_SOURCE = """
import errno, json, os, sys
DENIAL = {errno.EPERM, errno.EACCES}
checks = json.loads(sys.argv[1])
report = []


def record(name, expected, observed, code=""):
    if expected == "allowed":
        passed = observed == "allowed"
    else:
        passed = observed == "denied" and code in ("EPERM", "EACCES")
    report.append({"check": name, "expected": expected, "observed": observed,
                   "errno": code or None, "passed": bool(passed)})


def attempt(action, label, expected):
    try:
        action()
        record(label, expected, "allowed")
    except OSError as error:
        code = errno.errorcode.get(error.errno, str(error.errno))
        record(label, expected, "denied" if error.errno in DENIAL else "error", code)


def create_and_remove(target):
    probe = os.path.join(target, "%s")
    fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.close(fd)
    os.unlink(probe)


def read_one_byte(target):
    with open(target, "rb") as handle:
        handle.read(1)


def open_for_write(target):
    fd = os.open(target, os.O_WRONLY | os.O_APPEND)
    os.close(fd)


for target in checks.get("write_allowed_dirs", []):
    attempt(lambda t=target: create_and_remove(t), "write:" + target, "allowed")
for target in checks.get("sentinels", []):
    attempt(lambda t=target: read_one_byte(t), "read-file:" + target, "denied")
    attempt(lambda t=target: open_for_write(t), "write-file:" + target, "denied")

print(json.dumps(report))
""" % PROBE_NAME


@dataclass(frozen=True)
class Boundary:
    """What one worker process may touch. Everything else is denied."""

    workspace: str
    scratch: str
    tmpdir: str
    deny: tuple[str, ...]
    allow: tuple[str, ...] = ()
    deny_write: tuple[str, ...] = ()
    write_allow: tuple[str, ...] = ()
    sentinels: tuple[str, ...] = ()
    network: bool = True
    label: str = "seatbelt-worker-boundary"

    def as_dict(self) -> dict:
        return {"workspace": self.workspace, "scratch": self.scratch, "tmpdir": self.tmpdir,
                "deny": list(self.deny), "allow": list(self.allow),
                "deny_write": list(self.deny_write), "write_allow": list(self.write_allow),
                "sentinels": list(self.sentinels),
                "network": self.network, "label": self.label}

    def writable_roots(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((self.workspace, self.scratch, self.tmpdir, *self.write_allow)))


def sensitive_account_stores(home: Path | None = None) -> list[str]:
    """Absolute credential/account stores denied to every worker by policy."""
    root = Path(home or Path.home()).resolve()
    stores = [str(root / entry) for entry in SENSITIVE_HOME_ENTRIES]
    stores += [str(root / entry) for entry in SENSITIVE_HOME_LIBRARY]
    return sorted({path for path in stores})


def _real(item: str) -> Path:
    return Path(os.path.realpath(str(item)))


def _inside(child: Path, parent: Path) -> bool:
    return child == parent or parent in child.parents


def _quote(path: str) -> str:
    return '"' + str(path).replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_profile(boundary: Boundary) -> str:
    """Render an ephemeral Seatbelt profile: default-deny write, curated deny read.

    Every path is realpath-normalized first: Seatbelt matches resolved paths, so an
    unresolved symlink alias (``/var`` vs ``/private/var``) would silently void a rule.
    """
    workspace = str(_real(boundary.workspace))
    scratch = str(_real(boundary.scratch))
    tmpdir = str(_real(boundary.tmpdir))
    deny = [str(_real(item)) for item in boundary.deny]
    allow = [str(_real(item)) for item in boundary.allow]
    deny_write = [str(_real(item)) for item in boundary.deny_write]
    write_allow = [str(_real(item)) for item in boundary.write_allow]
    rules = [
        "(version 1)",
        "(deny default)",
        "(allow process-exec)",
        "(allow process-fork)",
        "(allow process-info-pidinfo)",
        "(allow signal)",
        "(allow sysctl-read)",
        "(allow system-socket)",
        "(allow mach-lookup)",
        "(allow mach-register)",
        "(allow ipc-posix-shm*)",
        "(allow ipc-posix-sem*)",
        "(allow iokit-open)",
        "(allow file-ioctl)",
        "(allow file-read-metadata)",
        # Read is allowed broadly, then narrowed by explicit denials below: a complete
        # read allowlist is not demonstrable for third-party harness runtimes in M4 and
        # remains an open gate. Credential/controller denials are the enforced contract.
        "(allow file-read*)",
    ]
    if boundary.network:
        rules.append("(allow network*)")
    for denied in deny:
        rules.append(f"(deny file-read* file-write* (subpath {_quote(denied)}))")
        rules.append(f"(deny file-read* file-write* (literal {_quote(denied)}))")
    for allowed in allow:
        # Narrow read-only re-allow inside a denied parent: the route's own credential store.
        # Both forms are emitted because `subpath` alone does not match a bare file literal.
        # Write stays denied by the global `(deny file-write*)` emitted below.
        rules.append(f"(allow file-read* (subpath {_quote(allowed)}))")
        rules.append(f"(allow file-read* (literal {_quote(allowed)}))")
    rules += ["(deny file-write*)", f"(allow file-write* (subpath {_quote('/dev')}))"]
    for root in (workspace, scratch, tmpdir, *write_allow):
        # The worker's own roots are re-opened for read *and* write after the denials above,
        # because scratch legitimately lives inside denied controller state. refuse_overlaps
        # proves this can only ever re-open the declared per-task scratch, never a sibling.
        rules.append(f"(allow file-read* file-write* (subpath {_quote(root)}))")
    rules.append(
        '(allow file-write-data (literal "/dev/null") (literal "/dev/stdout") (literal "/dev/stderr"))')
    for denied in deny_write:
        # Emitted last so an in-workspace verifier denial wins over the workspace write allow.
        rules.append(f"(deny file-write* (subpath {_quote(denied)}))")
        rules.append(f"(deny file-write* (literal {_quote(denied)}))")
    for pattern in SECRET_FILE_DENY_REGEXES:
        rules.append(f'(deny file-read* file-write* (regex #"{pattern}"))')
    for pattern in SECRET_FILE_ALLOW_REGEXES:
        rules.append(f'(allow file-read* (regex #"{pattern}"))')
    return "\n".join(rules) + "\n"


def profile_digest(profile: str) -> str:
    return hashlib.sha256(profile.encode()).hexdigest()


def sandbox_exec() -> str | None:
    return shutil.which("sandbox-exec")


def wrapped(profile: str, command: list[str]) -> list[str]:
    binary = sandbox_exec()
    if not binary:
        raise EnvironmentError("sandbox-exec is required for worker containment but is not installed")
    return [binary, "-p", profile, *command]


def _digest_file(path: str) -> str | None:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def write_sentinel(path: Path | str, label: str) -> str:
    """Create one disposable controller-owned sentinel inside already-denied state."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"corral containment sentinel: {label}\n")
    target.chmod(0o600)
    return str(target.resolve())


def demonstrate(boundary: Boundary) -> dict:
    """Actually launch a probe child under the profile and report per-check results.

    No result is inferred from configuration: every check is an observed allow/deny with
    the errno the kernel returned.
    """
    binary = sandbox_exec()
    profile = build_profile(boundary)
    receipt: dict = {"available": binary is not None, "passed": False, "sandbox_exec": binary,
                     "checks": [], "blocker": None, "profile_digest": profile_digest(profile),
                     "contained_operations": list(CONTAINED_OPERATIONS),
                     "not_contained": list(NOT_CONTAINED), "isolation_claim": ISOLATION_CLAIM,
                     "configured_denials": sorted(set(boundary.deny) | set(boundary.deny_write)),
                     "sentinels": sorted({str(Path(item).resolve()) for item in boundary.sentinels})}
    if binary is None:
        receipt["blocker"] = "sandbox-exec not installed on this host"
        return receipt
    workspace = Path(boundary.workspace)
    scratch = Path(boundary.scratch)
    writable = [Path(item) for item in boundary.writable_roots()]
    for item in (workspace, scratch):
        item.mkdir(parents=True, exist_ok=True)
    missing_writable = [str(item) for item in writable if not item.is_dir()]
    if missing_writable:
        receipt["blocker"] = "declared writable runtime path is absent: " + ", ".join(missing_writable)
        return receipt
    sentinels = receipt["sentinels"]
    missing = [item for item in sentinels if not Path(item).is_file()]
    if missing:
        receipt["blocker"] = ("containment sentinels must pre-exist as disposable probe targets: "
                              + ", ".join(missing))
        return receipt
    before = {item: _digest_file(item) for item in sentinels}
    payload = {"write_allowed_dirs": [str(item.resolve()) for item in writable],
               "sentinels": sentinels}
    command = wrapped(profile, [sys.executable, "-I", "-", json.dumps(payload)])
    receipt["probe_command_digest"] = hashlib.sha256(json.dumps(command).encode()).hexdigest()
    try:
        completed = subprocess.run(command, cwd=str(scratch), capture_output=True, text=True,
                                   input=_PROBE_SOURCE,
                                   env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                                        "TMPDIR": boundary.tmpdir, "PYTHONDONTWRITEBYTECODE": "1"})
    except OSError as error:
        receipt["blocker"] = f"sandbox-exec launch failed: {type(error).__name__}"
        return receipt
    checks: list[dict] = []
    if completed.returncode != 0:
        receipt["blocker"] = (f"containment probe exited {completed.returncode}: "
                              f"{(completed.stderr or '').strip()[-400:] or 'no stderr'}")
        return receipt
    try:
        checks = json.loads(completed.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        receipt["blocker"] = "containment probe produced no parsable report"
        return receipt
    receipt["checks"] = checks
    mutated = [item for item in sentinels if _digest_file(item) != before[item]]
    failed = sorted({check["check"] for check in checks if not check.get("passed")})
    observed = {check["check"] for check in checks}
    required = {f"write:{Path(item).resolve()}" for item in writable}
    required |= {f"{kind}:{item}" for item in sentinels for kind in ("read-file", "write-file")}
    missing_checks = sorted(required - observed)
    if mutated:
        receipt["blocker"] = "containment sentinel changed during probe: " + ", ".join(mutated)
    elif failed:
        receipt["blocker"] = ("containment probe observed unexpected permissions or inconclusive "
                              "errno: " + ", ".join(failed))
    elif missing_checks:
        receipt["blocker"] = "containment probe did not cover required targets: " + ", ".join(missing_checks)
    receipt["passed"] = receipt["blocker"] is None and bool(checks)
    return receipt


def require(boundary: Boundary) -> dict:
    """Fail closed unless the boundary is demonstrated by a real sandboxed probe."""
    receipt = demonstrate(boundary)
    if not receipt.get("passed"):
        raise PermissionError("worker containment could not be demonstrated: "
                              + str(receipt.get("blocker") or "unknown blocker"))
    return receipt


def refuse_overlaps(boundary: Boundary, *, scratch_root: str | None = None) -> list[str]:
    """Reject configurations where an allow reopens a denied controller/credential subtree.

    Symlinks are resolved before comparison, so an alias cannot be used to smuggle a
    writable root inside trusted state or a grant around a denial.
    """
    problems: list[str] = []
    deny = {_real(item) for item in tuple(boundary.deny) + tuple(boundary.deny_write)}
    scratch_parent = _real(scratch_root) if scratch_root else None
    runtime_write = {_real(item) for item in boundary.write_allow}
    for raw in boundary.writable_roots():
        root = _real(raw)
        is_runtime_write = root in runtime_write
        if str(root) in ("/", str(Path.home())):
            problems.append(f"writable root is a whole-filesystem path: {root}")
        if is_runtime_write and not any(_inside(root, _real(grant)) for grant in boundary.allow):
            problems.append(f"runtime writable root lacks a declared read grant: {root}")
        for denied in deny:
            permitted_runtime = is_runtime_write and any(_inside(root, _real(grant)) for grant in boundary.allow)
            if _inside(root, denied) and not (scratch_parent and _inside(root, scratch_parent)) and not permitted_runtime:
                problems.append(f"writable root reopens a denied subtree: {root} under {denied}")
            if _inside(denied, root):
                problems.append(f"denied path is reachable for write from a writable root: {denied} under {root}")
    for raw in boundary.allow:
        granted = _real(raw)
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
