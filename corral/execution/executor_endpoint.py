"""Forced-command boundary for one registered Corral remote executor."""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from typing import Any, Callable

TASK_ID = re.compile(r"^[0-9a-f]{64}$")
ACTIONS = {
    "submit", "dispatch", "status", "steer", "continue", "cancel", "reconcile",
    "snapshot", "fetch-artifact", "transfer",
}


def _path(value: Any) -> str:
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise PermissionError("executor workspace must be an absolute registered path")
    return str(Path(value).resolve())


def _scope(config: dict[str, Any], spec: Any) -> None:
    """Refuse a task outside this endpoint's registered host, repositories and workspaces."""
    if not isinstance(spec, dict):
        raise ValueError("executor task requires a task specification")
    if spec.get("host") != config["executor_host"]:
        raise PermissionError("executor host is not registered")
    if spec.get("repo") not in set(config["allowed_repositories"]):
        raise PermissionError("executor repository is not registered")
    allowed = {_path(item) for item in config["allowed_workspaces"]}
    if _path(spec.get("workspace")) not in allowed:
        raise PermissionError("executor workspace is not registered")
    parent = spec.get("logical_parent")
    epoch = spec.get("authority_epoch")
    if not isinstance(parent, str) or not TASK_ID.fullmatch(parent):
        raise PermissionError("executor task lacks a logical authority binding")
    if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 0:
        raise PermissionError("executor task lacks an authority epoch")


def registered_task(config: dict[str, Any], task_id: str) -> dict[str, Any] | None:
    """Read one admitted request from the executor controller's store, without writing."""
    controller = json.loads(Path(config["controller_config"]).read_text())
    # The controller CLI this endpoint runs has cwd "/", so a relative state path is
    # resolved there too.
    database = Path("/", controller["state"]) / "controller.sqlite"
    if not database.is_file():
        return None
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        row = db.execute("SELECT value FROM records WHERE kind='request' AND key=?",
                         (task_id,)).fetchone()
    return json.loads(row[0]) if row else None


def validate(config: dict[str, Any], request: dict[str, Any], *,
             lookup: Callable[[dict[str, Any], str], Any] = registered_task) -> dict[str, Any]:
    """Bind the authenticated authority to registered hosts, repositories and workspaces.

    A submission is checked from its own specification. Every other action names a task,
    so it is checked against the request this executor admitted for that task: an unknown
    task, or one outside the registered scope, is refused before the controller runs.
    """
    action = request.get("action")
    if action not in ACTIONS:
        raise PermissionError("action is not allowed by the executor endpoint")
    if action == "submit":
        request_id, spec = request.get("request_id"), request.get("spec")
        if not isinstance(request_id, str) or not request_id.startswith("controller:"):
            raise PermissionError("executor submission requires controller request identity")
        if not isinstance(spec, dict):
            raise ValueError("executor submission requires a task specification")
        _scope(config, spec)
    else:
        task_id = request.get("task_id")
        if not isinstance(task_id, str) or not TASK_ID.fullmatch(task_id):
            raise PermissionError("executor action requires an exact task identity")
        spec = lookup(config, task_id)
        if spec is None:
            raise PermissionError("executor task is not registered")
        _scope(config, spec)
    return request


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args(argv)
    endpoint = json.loads(args.config.read_text())
    limit = int(endpoint.get("max_request_bytes", 2 * 1024 * 1024))
    payload = sys.stdin.buffer.read(limit + 1)
    if len(payload) > limit:
        raise ValueError("executor request exceeds configured size")
    request = validate(endpoint, json.loads(payload))
    controller_config = Path(endpoint["controller_config"]).resolve()
    env = {key: os.environ[key] for key in ("HOME", "LANG", "PATH", "TMPDIR")
           if key in os.environ}
    completed = subprocess.run(
        [sys.executable, "-I", "-m", "corral.execution.cli", "--config",
         str(controller_config)],
        input=json.dumps(request, sort_keys=True).encode(), capture_output=True, cwd="/", env=env,
    )
    sys.stdout.buffer.write(completed.stdout)
    sys.stderr.buffer.write(completed.stderr)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
