"""Durable Corral service: event admission, schedules, fair dispatch and recovery."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from . import continuation
from .controller import Controller
from .scheduler import ready
from .store import canonical

TERMINAL = {"completed", "failed", "refused-before-launch", "uncertain", "cancelled"}


class Service:
    """One service view over the controller's authoritative Store."""

    def __init__(self, config: str | Path | dict[str, Any]):
        self.config_path = Path(config).resolve() if not isinstance(config, dict) else None
        self.config = json.loads(self.config_path.read_text()) if self.config_path else dict(config)
        controller_path = Path(self.config["controller_config"])
        if not controller_path.is_absolute() and self.config_path:
            controller_path = self.config_path.parent / controller_path
        self.controller_path = controller_path.resolve()
        raw = json.loads(controller_path.read_text())
        self.controller = Controller(raw["state"], raw["token"], raw["hosts"],
                                     default_host=raw["default_host"],
                                     profiles=raw.get("profiles", []))
        self.token = raw["token"]
        self.execution_host = raw.get("execution_host", raw["default_host"])
        self.repositories = self.config.get("repositories", {})
        self.schedules = self.config.get("schedules", [])
        self.max_dispatch = int(self.config.get("max_dispatch_per_tick", 1))
        self.development_mode = self.config.get("development_mode") is True
        if self.max_dispatch < 1:
            raise ValueError("max_dispatch_per_tick must be positive")

    @property
    def store(self):
        return self.controller.store

    def _repository(self, name: str) -> dict[str, Any]:
        repo = self.repositories.get(name)
        if not isinstance(repo, dict) or repo.get("enabled", True) is not True:
            raise PermissionError(f"repository profile is not enabled: {name}")
        return repo

    def _host_workspace(self, repo: dict[str, Any], requested: str | None) -> tuple[str, str]:
        host = requested or repo.get("default_host") or self.controller.default_host
        if host not in self.controller.hosts:
            raise PermissionError(f"unsupported host: {host}")
        if host != self.execution_host and not self.controller.hosts[host].get("executor"):
            raise PermissionError("nonlocal host requires an authenticated remote executor route")
        workspaces = repo.get("workspaces", {})
        workspace = workspaces.get(host)
        if not isinstance(workspace, str) or not Path(workspace).is_absolute():
            raise PermissionError(f"repository has no registered workspace for host: {host}")
        allowed = set(repo.get("allowed_hosts") or workspaces)
        if host not in allowed:
            raise PermissionError(f"host is not allowed by repository profile: {host}")
        return host, workspace

    def _profile_route(self, spec: dict[str, Any], host: str) -> str:
        profile_id = spec.get("profile_id")
        if profile_id:
            profile = next((p for p in self.controller.profiles if p.id == profile_id), None)
            if profile is None:
                raise PermissionError("profile is not registered by controller")
            return profile.route
        routes = list(self.controller.hosts[host].get("routes", []))
        return routes[0] if routes else "deterministic"

    def submit(
        self, event_id: str, repository: str, objective: str, *, host: str | None = None,
        mode: str = "interactive", role: str | None = None, profile_id: str | None = None,
        candidate_paths: list[str] | None = None, source_snapshot: dict | None = None,
        source_reason: str = "interactive-cli", inspection_paths: list[str] | None = None,
        diff_path: str | None = None, workspace_kind: str = "checkout",
    ) -> dict[str, Any]:
        if not event_id or not objective.strip():
            raise ValueError("event_id and objective are required")
        if mode not in ("interactive", "wave", "scheduled"):
            raise ValueError("invalid service mode")
        repo = self._repository(repository)
        selected_host, workspace = self._host_workspace(repo, host)
        defaults = dict(repo.get("task_defaults", {}))
        allowed_roles = set(repo.get("allowed_roles", ("implementation", "review", "repair")))
        selected_role = role or defaults.get("role", "implementation")
        if selected_role not in allowed_roles:
            raise PermissionError("role is not allowed by repository profile")
        selected_profile = profile_id or defaults.get("profile_id")
        outputs = list(candidate_paths if candidate_paths is not None
                       else defaults.get("candidate_paths", []))
        inspected = list(inspection_paths or [])
        if workspace_kind not in {"checkout", "immutable_snapshot"}:
            raise ValueError("invalid workspace_kind")
        if selected_role == "review" and not inspected:
            raise ValueError("review admission requires inspection_paths")
        if diff_path is not None and diff_path not in inspected:
            raise ValueError("diff_path must be one of inspection_paths")
        spec = {**defaults, "repo": repository, "workspace": workspace,
                "objective": objective, "host": selected_host, "mode": mode,
                "role": selected_role, "candidate_paths": outputs,
                "source_reason": source_reason, "source_confidence": "controller-registered",
                "workspace_kind": workspace_kind, "inspection_paths": inspected,
                "diff_path": diff_path}
        if selected_profile:
            spec["profile_id"] = selected_profile
        spec["service_event_id"] = event_id
        spec["source_snapshot_digest"] = source_snapshot.get("digest") if source_snapshot else None
        identity = {"event_id": event_id, "repository": repository, "objective": objective,
                 "host": selected_host, "mode": mode, "role": selected_role,
                 "profile_id": selected_profile, "candidate_paths": outputs,
                 "source_snapshot": source_snapshot, "source_reason": source_reason,
                 "inspection_paths": inspected, "diff_path": diff_path,
                 "workspace_kind": workspace_kind,
                 "route": self._profile_route(spec, selected_host), "resolved_spec": spec}
        prior = self.store.get("service_event", event_id)
        if prior is not None:
            if any(prior.get(key) != value for key, value in identity.items()):
                raise ValueError("service event identity already has different content")
            return self._ensure_task(event_id)
        event = {**identity, "submitted": time.time(), "status": "admitted", "task_id": None}
        self.store.put_once("service_event", event_id, event)
        return self._ensure_task(event_id, spec=spec)

    def _ensure_task(self, event_id: str, spec: dict | None = None) -> dict[str, Any]:
        event = self.store.get("service_event", event_id)
        if event is None:
            raise KeyError(event_id)
        if event.get("status") not in ("admitted", "task-created", "preparing"):
            return self.status(event_id)
        spec = dict(event["resolved_spec"] if spec is None else spec)
        task_id = event.get("task_id") or self.controller.submit(
            self.token, "service:" + event_id, spec)
        updated = {**event, "task_id": task_id, "status": "task-created"}
        self.store.replace("service_event", event_id, updated)
        snapshot = event.get("source_snapshot")
        if snapshot and not updated.get("source_transfer"):
            updated = {**updated, "status": "preparing"}
            self.store.replace("service_event", event_id, updated)
            transfer_id = "service-source:" + event_id
            receipt = self.store.get("transfer_receipt", task_id + ":" + transfer_id)
            if receipt is None:
                paths = list(snapshot.get("files", {}))
                expected = self.controller.snapshot(self.token, task_id, paths)
                if snapshot.get("base") != expected.get("base"):
                    updated = {**updated, "status": "blocked",
                               "error": "source and registered workspace Git bases differ"}
                    self.store.replace("service_event", event_id, updated)
                    return self.status(event_id)
                try:
                    receipt = self.controller.transfer(
                        self.token, task_id, transfer_id, snapshot, expected)
                except Exception as exc:
                    updated = {**updated, "status": "blocked", "error": str(exc)}
                    self.store.replace("service_event", event_id, updated)
                    return self.status(event_id)
            updated = {**updated, "source_transfer": receipt}
            self.store.replace("service_event", event_id, updated)
        updated = {**updated, "status": "prepared"}
        self.store.replace("service_event", event_id, updated)
        return self.status(event_id)

    def status(self, event_id: str) -> dict[str, Any]:
        event = self.store.get("service_event", event_id)
        if event is None:
            raise KeyError(event_id)
        task = (self.controller.status(self.token, event["task_id"])
                if event.get("task_id") else None)
        return {"event": event, "task": task}

    def amend(self, event_id: str, amendment_id: str, objective: str) -> dict[str, Any]:
        event = self.store.get("service_event", event_id)
        if not event or not event.get("task_id"):
            raise KeyError(event_id)
        self.controller.steer(self.token, event["task_id"], amendment_id,
                              {"objective": objective})
        return self.status(event_id)

    def pause(self, event_id: str, amendment_id: str, paused: bool) -> dict[str, Any]:
        event = self.store.get("service_event", event_id)
        if not event or not event.get("task_id"):
            raise KeyError(event_id)
        self.controller.steer(self.token, event["task_id"], amendment_id,
                              {"pause_dispatch": paused})
        return self.status(event_id)

    def cancel(self, event_id: str) -> dict[str, Any]:
        event = self.store.get("service_event", event_id)
        if not event or not event.get("task_id"):
            raise KeyError(event_id)
        self.controller.cancel(self.token, event["task_id"])
        status = "cancelled" if event.get("status") in {"admitted", "task-created", "prepared"} \
            else event.get("status")
        self.store.replace("service_event", event_id, {**event, "status": status,
                           "cancellation_requested": True})
        return self.status(event_id)

    def continue_event(self, event_id: str, continuation_id: str,
                       objective: str) -> dict[str, Any]:
        event = self.store.get("service_event", event_id)
        if not event or not event.get("task_id"):
            raise KeyError(event_id)
        self.controller.continue_task(self.token, event["task_id"], continuation_id,
                                      {"objective": objective})
        self.store.replace("service_event", event_id, {**event, "status": "prepared"})
        return self.status(event_id)

    def _claim(self, event_id: str, now: float, runtime: dict[str, Any]) -> dict | None:
        with self.store.transaction() as db:
            row = db.execute("SELECT value FROM records WHERE kind='service_event' AND key=?",
                             (event_id,)).fetchone()
            if not row:
                return None
            event = json.loads(row[0])
            if event.get("status") != "prepared":
                return None
            claimed = {**event, "status": "dispatching", "dispatch_started": now,
                       "service_runtime": runtime}
            db.execute("UPDATE records SET value=? WHERE kind='service_event' AND key=?",
                       (canonical(claimed), event_id))
            return claimed

    def _materialize_schedules(self, now: float) -> list[str]:
        created = []
        for schedule in self.schedules:
            if not schedule.get("enabled", True):
                continue
            interval = int(schedule["interval_seconds"])
            if interval <= 0:
                raise ValueError("schedule interval_seconds must be positive")
            slot = int((now - float(schedule.get("start", 0))) // interval)
            if slot < 0:
                continue
            event_id = f"schedule:{schedule['id']}:{slot}"
            if self.store.get("service_event", event_id):
                continue
            self.submit(event_id, schedule["repository"], schedule["objective"],
                        host=schedule.get("host"), mode="scheduled",
                        role=schedule.get("role"), profile_id=schedule.get("profile_id"),
                        candidate_paths=schedule.get("candidate_paths"),
                        source_reason="service-schedule")
            created.append(event_id)
        return created

    def _reconcile(self, runtime: dict[str, Any]) -> list[str]:
        from .runtime_identity import alive

        reconciled = []
        for event_id, event in self.store.records("service_event").items():
            if event.get("status") != "dispatching" or not event.get("task_id"):
                continue
            task = self.controller.status(self.token, event["task_id"])
            state = task.get("state") or {}
            if state.get("status") in TERMINAL:
                result = task.get("result") or {}
                if (state.get("status") == "completed" and result.get("accepted") and
                        continuation.pending_generation(self.store, event["task_id"])):
                    status = "prepared"
                elif self.store.get("cancel", event["task_id"]):
                    status = "cancelled"
                elif state.get("status") == "completed":
                    status = "completed" if result.get("accepted") else "failed"
                else:
                    status = state.get("status")
                worker = state.get("worker_identity") or {
                    key: state.get(key) for key in ("pid", "pgid", "attempt", "host")
                }
                worker.setdefault("coverage", "partial-without-process-start-or-executable")
                self.store.replace("service_event", event_id, {**event, "status": status,
                                   "reconciled": True, "service_runtime": runtime,
                                   "worker_identity": worker})
            elif alive(event.get("launcher_identity") or {}):
                continue
            elif state.get("status") in ("running", "dispatching") and alive(
                    state.get("worker_identity") or {}):
                continue
            else:
                self.store.replace("service_event", event_id, {**event, "status": "uncertain",
                                   "error": "dispatch acknowledgement lost; reconcile task before retry"})
            reconciled.append(event_id)
        return reconciled

    def tick(self, now: float | None = None) -> dict[str, Any]:
        from .runtime_identity import observe

        runtime = observe()
        self.store.replace("service_runtime", "current", runtime)
        now = time.time() if now is None else now
        created = self._materialize_schedules(now)
        reconciled = self._reconcile(runtime)
        dispatched = []
        events = self.store.records("service_event")
        for host, host_cfg in self.controller.hosts.items():
            busy_workspaces = {value.get("resolved_spec", {}).get("workspace")
                               for value in events.values()
                               if value.get("status") == "dispatching"}
            pending = [{"id": key, "route": value["route"], "mode": value["mode"],
                        "submitted": value["submitted"], "due": 0,
                        "cpu": value.get("resolved_spec", {}).get("cpu", 1),
                        "memory_mb": value.get("resolved_spec", {}).get("memory_mb", 0)}
                       for key, value in events.items()
                       if value.get("host") == host and value.get("status") == "prepared"
                       and value.get("resolved_spec", {}).get("workspace") not in busy_workspaces]
            running = [value for value in events.values()
                       if value.get("host") == host and value.get("status") == "dispatching"]
            capacity = {**host_cfg, "interactive_boost_seconds":
                        host_cfg.get("interactive_boost_seconds", 60)}
            for event_id in ready(pending, set(), running, capacity, now):
                if len(dispatched) >= self.max_dispatch:
                    break
                event = self._claim(event_id, now, runtime)
                if event is None:
                    continue
                try:
                    if host != self.execution_host and not host_cfg.get("executor"):
                        raise PermissionError(
                            "nonlocal host requires an authenticated remote executor route")
                    from .service_dispatch import launch
                    launcher = launch(self.controller_path, self.store.path.parent,
                                      event["task_id"], host,
                                      development_mode=self.development_mode)
                    self.store.replace("service_event", event_id,
                                       {**event, "launcher_identity": launcher})
                    busy_workspaces.add(event.get("resolved_spec", {}).get("workspace"))
                except Exception as exc:
                    self.store.replace("service_event", event_id,
                                       {**event, "status": "uncertain", "error": str(exc)})
                dispatched.append(event_id)
            if len(dispatched) >= self.max_dispatch:
                break
        return {"created": created, "reconciled": reconciled, "dispatched": dispatched,
                "pending": [k for k, v in self.store.records("service_event").items()
                            if v.get("status") == "prepared"]}

    def return_artifact(self, event_id: str, relative_path: str, destination: str | Path) -> Path:
        event = self.store.get("service_event", event_id)
        if not event or not event.get("task_id"):
            raise KeyError(event_id)
        fetched = self.controller.fetch_artifact(self.token, event["task_id"], relative_path)
        data = base64.b64decode(fetched["data"], validate=True)
        if hashlib.sha256(data).hexdigest() != fetched["digest"]:
            raise ValueError("artifact digest mismatch")
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                         fetched.get("mode") or 0o644)
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
        except FileExistsError:
            if target.read_bytes() != data:
                raise PermissionError("destination exists with newer or different content")
        return target
