"""Durable Corral service: event admission, schedules, fair dispatch and recovery."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from . import artifact_return, continuation
from .controller import Controller
from .service_specs import request_spec
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
                                     profiles=raw.get("profiles", []),
                                     secret_env=raw.get("secret_env"))
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
        if selected_role == "review":
            raise PermissionError("review tasks require controller-owned submit-pr-review")
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

    def submit_pr_review(self, repository: str, pr_number: int, policy_id: str, **kwargs):
        from .service_pr import submit_pr_review
        return submit_pr_review(self, repository, pr_number, policy_id, **kwargs)

    def submit_pr_repair(self, repository: str, pr_number: int,
                         review_receipt_id: str, **kwargs):
        from .service_repair import submit_pr_repair
        return submit_pr_repair(self, repository, pr_number, review_receipt_id, **kwargs)

    def publish_pr_repair(self, repository: str, event_id: str):
        from .github_branch import BranchPublisher
        return BranchPublisher(service=self, repository=repository).publish(event_id)

    def reconcile_pr_repair(self, repository: str, intent: str):
        from .github_branch import BranchPublisher
        return BranchPublisher(service=self, repository=repository).reconcile(intent)

    def submit_wave_plan(self, name: str, wave_id: str, **kwargs):
        from .service_wave import submit
        return submit(self, name, wave_id, **kwargs)

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
                from .service_prepare import claim
                if not claim(self.store, event_id, task_id, transfer_id):
                    return self.status(event_id)
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
                except Exception as exc:  # noqa: BLE001 - persist blocked admission before surfacing error
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

    def _claim(self, event_id: str, now: float, runtime: dict[str, Any], *,
               scheduler_host: str | None = None) -> dict | None:
        with self.store.transaction() as db:
            row = db.execute("SELECT value FROM records WHERE kind='service_event' AND key=?",
                             (event_id,)).fetchone()
            if not row:
                return None
            event = json.loads(row[0])
            if event.get("status") != "prepared":
                return None
            host = event.get("host")
            if scheduler_host is not None and host != scheduler_host:
                raise PermissionError("scheduler host does not match the admitted event")
            spec = request_spec(self.store, event, db=db)
            cpu, memory = spec.get("cpu", 1), spec.get("memory_mb", 0)
            if not isinstance(cpu, int) or isinstance(cpu, bool) or cpu <= 0:
                raise ValueError("invalid service CPU request")
            if not isinstance(memory, int) or isinstance(memory, bool) or memory < 0:
                raise ValueError("invalid service memory request")
            # Another dispatched service event or a launched wave task fences the workspace.
            from .service_wave import workspace_busy
            workspace = str(Path(spec.get("workspace")).resolve())
            if workspace_busy(self.store, db, workspace, event_id=event_id):
                return None
            # Capacity is reserved in the store, the single allocation authority that the
            # controller dispatch adopts. A refusal means not dispatched and nothing acquired.
            try:
                if not self.store.allocate(event["task_id"], host, cpu, memory,
                                           self.controller.capacity(host),
                                           reservation=event_id, db=db):
                    return None
            except PermissionError:
                return None
            # Bind the event to the generation its launcher will claim, so reconciliation can
            # tell "dispatched, launcher not yet claimed" from "scheduled, never dispatched".
            generation = continuation.pending_generation(self.store, event["task_id"], db=db) or 1
            claimed = {**event, "status": "dispatching", "dispatch_started": now,
                       "service_runtime": runtime, "dispatch_generation": generation}
            db.execute("UPDATE records SET value=? WHERE kind='service_event' AND key=?",
                       (canonical(claimed), event_id))
            if scheduler_host is not None:
                db.execute("INSERT OR REPLACE INTO records VALUES('service_scheduler',?,?)",
                           ("host_cursor", canonical({"last_host": scheduler_host})))
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
        from .runtime_identity import process_status

        reconciled = []
        for event_id, event in self.store.records("service_event").items():
            if event.get("status") not in {"dispatching", "uncertain"} or not event.get("task_id"):
                continue
            task_id = event["task_id"]
            # Observe the launcher before any controller state: a launcher writes its final
            # state before it exits, so a launcher already seen dead left nothing unread below.
            launcher = process_status(event.get("launcher_identity") or {})
            cancelled = self.store.get("cancel", task_id) is not None
            dispatched = event.get("dispatch_generation")
            if (dispatched is not None and self.store.get(
                    "claim", continuation.claim_key(task_id, dispatched)) is None):
                # The launcher has not claimed the generation this event dispatched. The claim
                # precedes any controller ownership or worker, so while the launcher lives the
                # dispatch is simply in flight; state from an earlier generation is not its own.
                if launcher == "alive":
                    continue
                # Only a launcher proven dead can no longer adopt the reservation. An
                # unobservable one (no recorded identity, or no birth token) is reported
                # uncertain but keeps its capacity; the event converges once a claim appears.
                if launcher == "dead":
                    self.store.release_reservation(task_id, event_id)
                if cancelled and launcher == "dead":
                    updated = {**event, "status": "cancelled", "reconciled": True,
                               "service_runtime": runtime}
                else:
                    updated = {**event, "status": "uncertain", "error": (
                        "dispatch acknowledgement lost before the launcher claimed generation "
                        f"{dispatched}; reconcile task before retry")}
                self.store.replace("service_event", event_id, updated)
                reconciled.append(event_id)
                continue
            task = self.controller.status(self.token, task_id)
            state = task.get("state") or {}
            if state.get("status") in TERMINAL:
                result = task.get("result") or {}
                pending = continuation.pending_generation(self.store, task_id)
                lineage = task.get("lineage") or {}
                expected_generation = max(
                    int(lineage.get("current_generation") or 1),
                    int(lineage.get("scheduled_generation") or 1),
                )
                state_generation = continuation.generation_of(state)
                result_generation = continuation.generation_of(result)
                amendment_pending = bool(
                    state.get("amended_objective_pending")
                    or result.get("amendment_pending")
                )
                status = None
                if cancelled:
                    spec = request_spec(self.store, event)
                    # The fence the controller's run path holds for this host kind.
                    resource = ("remote-workspace:" + spec["host"] + ":" + spec["workspace"]
                                if (self.controller.hosts.get(spec["host"]) or {}).get("executor")
                                else "workspace:" + str(Path(spec["workspace"]).resolve()))
                    ownership = self.store.ownership(resource)
                    allocation = self.store.get("allocation", task_id) or {}
                    released = ((ownership is None or ownership[0] != task_id
                                 or ownership[2] == "released")
                                and not (allocation.get("active")
                                         and allocation.get("reservation") != event_id))
                    if released and state.get("status") == "cancelled":
                        status = "cancelled"
                    elif released and state.get("status") == "completed" and result:
                        # The cancel landed after the attempt finished: a post-terminal no-op.
                        status = "completed" if result.get("accepted") else "failed"
                    else:
                        status = "uncertain"
                elif pending is not None:
                    status = "prepared"
                elif (state.get("status") == "completed"
                      and state_generation >= expected_generation
                      and result_generation >= expected_generation
                      and not amendment_pending):
                    status = "completed" if result.get("accepted") else "failed"
                elif state.get("status") != "completed":
                    status = state.get("status")
                if status is not None:
                    worker = {
                        key: state.get(key) for key in ("pid", "pgid", "attempt", "host")
                    }
                    worker.update(state.get("worker_identity") or {})
                    worker.setdefault("coverage", "partial-without-process-start-or-executable")
                    updated = {**event, "status": status, "reconciled": True,
                               "service_runtime": runtime, "worker_identity": worker}
                    if cancelled and status == "uncertain":
                        updated["error"] = (
                            "cancellation requested; controller ownership is unresolved"
                        )
                    if status != "uncertain":
                        # Leaving dispatch returns a reservation no controller dispatch adopted.
                        self.store.release_reservation(task_id, event_id)
                    self.store.replace("service_event", event_id, updated)
                    reconciled.append(event_id)
                    continue
            elif (launcher == "alive"
                  or (state.get("status") in ("running", "dispatching")
                      and process_status(state.get("worker_identity") or {}) == "alive")):
                continue
            else:
                self.store.replace("service_event", event_id, {**event, "status": "uncertain",
                                   "error": "dispatch acknowledgement lost; reconcile task before retry"})
            reconciled.append(event_id)
        return reconciled

    def tick(self, now: float | None = None) -> dict[str, Any]:
        from .runtime_identity import observe
        from .service_scheduler import acquire_tick, dispatch_lanes, release_tick

        runtime = observe()
        self.store.replace("service_runtime", "current", runtime)
        lease = acquire_tick(self, runtime)
        if lease is None:
            return {"created": [], "reconciled": [], "dispatched": [], "waves": [],
                    "busy": True,
                    "pending": [k for k, v in self.store.records("service_event").items()
                                if v.get("status") == "prepared"]}
        try:
            now = time.time() if now is None else now
            created = self._materialize_schedules(now)
            reconciled = self._reconcile(runtime)
            events = self.store.records("service_event")
            dispatched, waves = dispatch_lanes(self, events, now, runtime)
            return {"created": created, "reconciled": reconciled,
                    "dispatched": dispatched, "waves": waves, "busy": False,
                    "pending": [k for k, v in self.store.records("service_event").items()
                                if v.get("status") == "prepared"]}
        finally:
            release_tick(self, lease)

    def return_artifact(self, event_id: str, relative_path: str, destination: str | Path) -> Path:
        event = self.store.get("service_event", event_id)
        if not event or not event.get("task_id"):
            raise KeyError(event_id)
        fetched = self.controller.fetch_artifact(self.token, event["task_id"], relative_path)
        return artifact_return.write(fetched, destination)
