"""Owned local process groups; uncertainty is never silently released."""
import hashlib
import os
import shutil
import signal
import socket
import subprocess
import time
from pathlib import Path

from . import containment


def boundary_for(cwd, protected_paths=(), verifier_paths=(), task_dir=None,
                 scratch=None, tmpdir=None, sentinels=()) -> containment.Boundary:
    """Translate controller-declared protections into a worker boundary description."""
    workspace = Path(cwd).resolve()
    deny = {str(Path(item).expanduser().resolve()) for item in tuple(protected_paths)}
    if task_dir is not None:
        # The task directory is trusted adapter/controller I/O; a worker never reads or writes it.
        deny.add(str(Path(task_dir).resolve()))
    # Account/credential stores are denied for every sandboxed worker, not just native runs.
    deny.update(containment.sensitive_account_stores())
    deny_write = {str((workspace / item).resolve()) for item in tuple(verifier_paths) if item}
    return containment.Boundary(
        workspace=str(workspace),
        scratch=str(Path(scratch).resolve()) if scratch else str(workspace),
        tmpdir=str(Path(tmpdir or os.environ.get("TMPDIR") or "/tmp").resolve()),
        deny=tuple(sorted(deny)),
        deny_write=tuple(sorted(deny_write)),
        sentinels=tuple(str(Path(item).resolve()) for item in tuple(sentinels)),
    )


class Process:
    def __init__(self, command, cwd, stdout, stderr, env=None,
                 protected_paths=(), verifier_paths=(), use_sandbox=False, task_dir=None,
                 seatbelt_profile=None, containment_label=None):
        clean = {k: os.environ[k] for k in ("PATH", "LANG", "TMPDIR") if k in os.environ}
        clean.update(env or {})
        self.containment = containment_label or "standard-clean-env"
        cmd = list(command)
        profile = seatbelt_profile
        if profile is None and use_sandbox:
            profile = containment.build_profile(
                boundary_for(cwd, protected_paths, verifier_paths, task_dir))
        if profile is not None:
            cmd = containment.wrapped(profile, cmd)
            if containment_label is None:
                self.containment = "seatbelt-worker-boundary"
        self.child = subprocess.Popen(cmd, cwd=cwd, stdout=stdout, stderr=stderr,
                                      env=clean, start_new_session=True)
        self.pgid = self.child.pid
        self.worker_identity = self._identity(cmd)

    def _identity(self, command):
        """Capture a launch token before a future PID can be reused.

        The controller stores this immutable observation with the attempt.  It is not a
        containment claim and a later reconciliation still refuses a live process group.
        """
        executable = shutil.which(str(command[0])) or str(command[0])
        path = Path(executable)
        try:
            resolved = str(path.resolve(strict=True))
            hasher = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    hasher.update(chunk)
            file_digest = hasher.hexdigest()
        except OSError:
            resolved, file_digest = str(path), None
        observed = _os_process_identity(self.child.pid)
        return {"pid": self.child.pid, "pgid": self.pgid, "observed_at_ns": time.time_ns(),
                "host": socket.gethostname(), "boot_identity": _boot_identity(),
                "os_started": observed.get("started"), "os_command": observed.get("command"),
                "executable": resolved, "executable_sha256": file_digest}

    def running_group(self):
        try:
            os.killpg(self.pgid, 0)
            return True
        except ProcessLookupError:
            return False

    def wait(self):
        return self.child.wait()

    def cancel(self, grace=0.2):
        # Process group is created by this object; never kill by command name.
        for sig in (signal.SIGTERM, signal.SIGKILL):
            if self.running_group():
                os.killpg(self.pgid, sig)
            deadline = time.monotonic() + grace
            while time.monotonic() < deadline:
                self.child.poll()
                if not self.running_group():
                    break
                time.sleep(0.01)
        self.child.poll()
        return {"parent_exit": self.child.returncode,
                "group_stopped": not self.running_group(),
                "detached_descendants": "unknown",
                "ownership": "uncertain"}


def _os_process_identity(pid):
    """Read an OS birth-time observation through a fixed trusted platform utility."""
    ps = shutil.which("ps")
    if not ps:
        return {"started": None, "command": None}
    completed = subprocess.run([ps, "-p", str(pid), "-o", "lstart=", "-o", "comm="],
                               capture_output=True, text=True)
    fields = completed.stdout.strip().split(maxsplit=5)
    if completed.returncode or len(fields) != 6:
        return {"started": None, "command": None}
    return {"started": " ".join(fields[:5]), "command": fields[5]}


def _boot_identity():
    sysctl = shutil.which("sysctl")
    if not sysctl:
        return None
    completed = subprocess.run([sysctl, "-n", "kern.boottime"], capture_output=True, text=True)
    return hashlib.sha256(completed.stdout.strip().encode()).hexdigest() if completed.returncode == 0 else None
