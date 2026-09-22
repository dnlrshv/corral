"""Reusable agent-facing client API and commands.

Eliminates per-run hand-authored controller JSON and raw evidence directory paths.
Supports independent host, repository, model, and effort profiles with configurable
defaults (Mini2 default when configured, never silently substituted).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .client import Client


@dataclass
class AgentConfig:
    controller_config: Path
    python: str = sys.executable
    source: Path | None = None
    transport: str = "local"
    ssh_host: str | None = None
    ssh_options: list[str] | None = None
    default_host: str = "mini2"
    profiles: dict[str, dict[str, Any]] | None = None

    @classmethod
    def load(cls, path: Path | str) -> AgentConfig:
        data = json.loads(Path(path).read_text())
        return cls(
            controller_config=Path(data["controller_config"]),
            python=data.get("python", sys.executable),
            source=Path(data["source"]) if data.get("source") else None,
            transport=data.get("transport", "local"),
            ssh_host=data.get("ssh_host"),
            ssh_options=data.get("ssh_options"),
            default_host=data.get("default_host", "mini2"),
            profiles=data.get("profiles", {}),
        )

    def as_client_config(self) -> dict[str, Any]:
        cfg: dict[str, Any] = {
            "python": self.python,
            "controller_config": str(self.controller_config),
            "source": str(self.source) if self.source else str(Path.cwd()),
            "transport": self.transport,
        }
        if self.transport == "ssh":
            if not self.ssh_host:
                raise ValueError("ssh transport requires ssh_host")
            cfg["ssh_host"] = self.ssh_host
            cfg["ssh_options"] = self.ssh_options or []
        return cfg


class CorralAgent:
    """High-level client API for submitting, steering, continuing, and running tasks."""

    def __init__(self, config: AgentConfig | dict[str, Any] | Path | str):
        if isinstance(config, (str, Path)):
            self.config = AgentConfig.load(config)
        elif isinstance(config, dict):
            self.config = AgentConfig(**config)
        else:
            self.config = config
        self.client = Client(self.config.as_client_config())

    def submit(
        self,
        repo: str | Path,
        objective: str,
        *,
        request_id: str | None = None,
        candidate_paths: list[str] | None = None,
        verifier_paths: list[str] | None = None,
        command: list[str] | None = None,
        verify: list[str] | None = None,
        host: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        role: str = "implementation",
        profile_id: str | None = None,
        dependencies: list[str] | None = None,
        result_file: str = "result.json",
        usage_file: str = "usage.json",
        tools: list[str] | None = None,
        inspection_paths: list[str] | None = None,
        inspection_diff_path: str | None = None,
        inspection_provenance: dict[str, Any] | None = None,
    ) -> str:
        """Submit a task without manually authoring JSON files."""
        repo_path = str(Path(repo).resolve())
        req_id = request_id or hashlib.sha256(f"{repo_path}:{objective}:{time.time()}".encode()).hexdigest()[:16]
        target_host = host or self.config.default_host

        spec: dict[str, Any] = {
            "repo": repo_path,
            "workspace": repo_path,
            "objective": objective,
            "host": target_host,
            "role": role,
            "candidate_paths": candidate_paths or [],
            "verifier_paths": verifier_paths or [],
            "dependencies": dependencies or [],
            "result_file": result_file,
            "usage_file": usage_file,
        }
        if profile_id and self.config.profiles and profile_id in self.config.profiles:
            profile = self.config.profiles[profile_id]
            if "workspace" in profile:
                spec["workspace"] = profile["workspace"]
            if "verify" in profile and not verify:
                spec["verify"] = profile["verify"]
            if "verifier_paths" in profile and not verifier_paths:
                spec["verifier_paths"] = profile["verifier_paths"]
            if "external_verifier" in profile:
                spec["external_verifier"] = profile["external_verifier"]
            if "candidate_paths" in profile and not candidate_paths:
                spec["candidate_paths"] = profile["candidate_paths"]
            if "model" in profile and not model:
                model = profile["model"]
            if "effort" in profile and not effort:
                effort = profile["effort"]
            if "host" in profile and not host:
                spec["host"] = profile["host"]

        if command:
            spec["command"] = command
        if verify:
            spec["verify"] = verify
        if profile_id and not (self.config.profiles and profile_id in self.config.profiles):
            raise ValueError(f"Unknown profile: {profile_id}")
        if model:
            spec["model"] = model
        if effort:
            spec["effort"] = effort
        if inspection_paths is not None:
            spec["inspection_paths"] = inspection_paths
        if inspection_diff_path is not None:
            spec["inspection_diff_path"] = inspection_diff_path
        if inspection_provenance is not None:
            spec["inspection_provenance"] = inspection_provenance
        if tools is not None:
            spec["tools"] = tools
        elif inspection_paths is not None:
            spec["tools"] = ["inspect-packet", "report"]
        elif profile_id or model:
            spec["tools"] = ["read", "search", "edit", "shell", "test"]

        if inspection_paths is not None:
            inspection_candidates = [*inspection_paths]
            if inspection_diff_path:
                inspection_candidates.append(inspection_diff_path)
            spec["candidate_paths"] = list(dict.fromkeys(
                [*spec["candidate_paths"], *inspection_candidates]))

        response = self.client.call("submit", request_id=req_id, spec=spec)
        return str(response["task"])

    def status(self, task_id: str) -> dict[str, Any]:
        """Query read-only task status, receipts, and lineage."""
        return self.client.call("status", task_id=task_id)

    def dispatch(self, task_id: str) -> dict[str, Any]:
        """Dispatch task worker execution."""
        return self.client.call("dispatch", task_id=task_id)

    def steer(self, task_id: str, amendment_id: str, amendment: dict[str, Any]) -> dict[str, Any]:
        """Acknowledge and apply steering amendment at safe boundary."""
        return self.client.call("steer", task_id=task_id, amendment_id=amendment_id, amendment=amendment)

    def continue_task(self, task_id: str, continuation_id: str, amendment: dict[str, Any]) -> dict[str, Any]:
        """Schedule post-terminal continuation under the same aggregate task ID."""
        return self.client.call("continue", task_id=task_id, continuation_id=continuation_id, continuation=amendment)

    def cancel(self, task_id: str) -> dict[str, Any]:
        """Request permanent cancellation of task."""
        return self.client.call("cancel", task_id=task_id)

    def snapshot(self, task_id: str, paths: list[str]) -> dict[str, Any]:
        """Snapshot specified paths from the task workspace."""
        return self.client.call("snapshot", task_id=task_id, paths=paths)

    def return_artifact(self, task_id: str, relative_path: str, destination: str | Path, generation: int | None = None) -> Path:
        """Return an accepted artifact to a local destination safely, rejecting modified/nonaccepted output."""
        # We allow retrieving from prior accepted generations even if the current generation failed
        # fetch-artifact directly queries the immutable controller storage
        fetched = self.client.call("fetch-artifact", task_id=task_id, path=relative_path, generation=generation)

        import base64
        import hashlib
        import os

        data = base64.b64decode(fetched["data"])
        actual_digest = hashlib.sha256(data).hexdigest()
        if actual_digest != fetched["digest"]:
            raise ValueError("artifact data corruption during transfer")

        dest = Path(destination)
        if dest.is_dir():
            dest = dest / Path(relative_path).name

        dest.parent.mkdir(parents=True, exist_ok=True)

        try:
            # Try fresh publication (O_CREAT | O_EXCL)
            fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, fetched.get("mode", 0o644))
            with os.fdopen(fd, "wb") as f:
                f.write(data)
        except FileExistsError:
            # Destination exists. Prevent overwrite of newer dev files.
            # Only allow if contents are exactly identical.
            existing_data = dest.read_bytes()
            if existing_data != data:
                raise PermissionError(f"destination {dest} already exists and differs from artifact. Conflict refused.")

        return dest

    def wait(self, task_id: str, poll_interval: float = 0.1, timeout: float | None = None) -> dict[str, Any]:
        """Wait for task completion without model inference."""
        deadline = (time.time() + timeout) if timeout else None
        while True:
            st = self.status(task_id)
            state = st.get("state") or {}
            status_val = state.get("status")
            if status_val in ("completed", "uncertain", "failed", "refused-before-launch"):
                pending = st.get("lineage", {}).get("pending_generation")
                if pending:
                    self.dispatch(task_id)
                elif not state.get("amended_objective_pending"):
                    return st
            if deadline and time.time() >= deadline:
                return st
            time.sleep(poll_interval)

    def run(
        self,
        repo: str | Path,
        objective: str,
        *,
        poll_interval: float = 0.1,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """High-level execution: submit -> dispatch -> wait -> status."""
        task_id = self.submit(repo, objective, **kwargs)
        self.dispatch(task_id)
        return self.wait(task_id, poll_interval=poll_interval, timeout=timeout)


    def wave_start(self, wave_id: str, tasks: list[dict[str, Any]], handoffs: list[dict[str, Any]] | None = None, **kwargs: Any) -> dict[str, Any]:
        """Start or resume a wave of dependent tasks."""
        return self.client.call("run-wave", wave_id=wave_id, tasks=tasks, handoffs=handoffs, **kwargs)

    def wave_status(self, wave_id: str) -> dict[str, Any]:
        """Query wave status and blocked details."""
        return self.client.call("wave-status", wave_id=wave_id)

    def wave_reconcile(self, wave_id: str) -> dict[str, Any]:
        """Reconcile an uncertain wave runner."""
        return self.client.call("reconcile-wave", wave_id=wave_id)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path, help="Client configuration path")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # run
    run_parser = subparsers.add_parser("run", help="Submit, dispatch, and wait for task")
    run_parser.add_argument("--repo", required=True, type=Path)
    run_parser.add_argument("--objective", required=True)
    run_parser.add_argument("--host", default=None)
    run_parser.add_argument("--model", default=None)
    run_parser.add_argument("--effort", default=None)
    run_parser.add_argument("--candidates", nargs="*", default=[])
    run_parser.add_argument("--profile", default=None)
    run_parser.add_argument("--role", default="implementation")
    run_parser.add_argument("--inspection-paths", nargs="*")
    run_parser.add_argument("--inspection-diff")
    run_parser.add_argument("--inspection-kind", choices=("git-checkout", "immutable-snapshot"))
    run_parser.add_argument("--inspection-repo")
    run_parser.add_argument("--head")
    run_parser.add_argument("--base")
    run_parser.add_argument("--pr", type=int)

    # status
    status_parser = subparsers.add_parser("status", help="Query task status")
    status_parser.add_argument("--task-id", required=True)

    # continue
    cont_parser = subparsers.add_parser("continue", help="Schedule continuation")
    cont_parser.add_argument("--task-id", required=True)
    cont_parser.add_argument("--continuation-id", required=True)
    cont_parser.add_argument("--objective", required=True)

    # amend
    amend_parser = subparsers.add_parser("amend", help="Amend running task objective")
    amend_parser.add_argument("--task-id", required=True)
    amend_parser.add_argument("--amendment-id", required=True)
    amend_parser.add_argument("--objective", required=True)

    # wave-start
    wave_start_parser = subparsers.add_parser("wave-start", help="Submit and run a wave")
    wave_start_parser.add_argument("--wave-id", required=True)
    wave_start_parser.add_argument("--spec", required=True, type=Path, help="JSON wave specification (tasks, handoffs)")

    # wave-status
    wave_status_parser = subparsers.add_parser("wave-status", help="Query wave status")
    wave_status_parser.add_argument("--wave-id", required=True)

    # wave-reconcile
    wave_reconcile_parser = subparsers.add_parser("wave-reconcile", help="Reconcile uncertain wave runner")
    wave_reconcile_parser.add_argument("--wave-id", required=True)

    # cancel
    cancel_parser = subparsers.add_parser("cancel", help="Cancel task")
    cancel_parser.add_argument("--task-id", required=True)

    args = parser.parse_args()
    agent = CorralAgent(args.config)

    if args.command == "run":
        provenance_values = (args.inspection_kind, args.inspection_repo, args.head, args.base)
        if any(provenance_values) and not all(provenance_values):
            parser.error("inspection provenance requires --inspection-kind, --inspection-repo, --head, and --base")
        inspection_provenance = None
        if all(provenance_values):
            inspection_provenance = {
                "kind": args.inspection_kind, "repo": args.inspection_repo,
                "head": args.head, "base": args.base,
            }
            if args.pr is not None:
                inspection_provenance["pr"] = args.pr
        result = agent.run(args.repo, args.objective, host=args.host, model=args.model,
                           effort=args.effort, candidate_paths=args.candidates,
                           profile_id=args.profile, role=args.role,
                           inspection_paths=args.inspection_paths,
                           inspection_diff_path=args.inspection_diff,
                           inspection_provenance=inspection_provenance)
        print(json.dumps(result, indent=2))
    elif args.command == "status":
        print(json.dumps(agent.status(args.task_id), indent=2))
    elif args.command == "continue":
        agent.continue_task(args.task_id, args.continuation_id, {"objective": args.objective})
        agent.dispatch(args.task_id)
        final_res = agent.wait(args.task_id)
        print(json.dumps(final_res, indent=2))
    elif args.command == "amend":
        print(json.dumps(agent.steer(args.task_id, args.amendment_id, {"objective": args.objective}), indent=2))
    elif args.command == "wave-start":
        spec = json.loads(args.spec.read_text())
        print(json.dumps(agent.wave_start(args.wave_id, spec.get("tasks", []), spec.get("handoffs")), indent=2))
    elif args.command == "wave-status":
        print(json.dumps(agent.wave_status(args.wave_id), indent=2))
    elif args.command == "wave-reconcile":
        print(json.dumps(agent.wave_reconcile(args.wave_id), indent=2))
    elif args.command == "cancel":
        print(json.dumps(agent.cancel(args.task_id), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
