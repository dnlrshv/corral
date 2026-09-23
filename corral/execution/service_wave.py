"""Resolve named finite-wave plans from trusted service configuration."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import continuation
from .store import digest
from .wave import WaveRunner


def _plan(service, name: str) -> dict[str, Any]:
    plans = service.config.get("wave_plans") or {}
    plan = plans.get(name) if isinstance(plans, dict) else None
    if not isinstance(plan, dict) or plan.get("enabled", True) is not True:
        raise PermissionError(f"named wave plan is not enabled: {name}")
    if not isinstance(plan.get("tasks"), list) or not plan["tasks"]:
        raise ValueError("named wave plan requires tasks")
    return plan


def resolve(service, name: str, wave_id: str, *, objective: str | None = None,
            host: str | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build controller specs only from registered repositories and plan defaults."""
    plan = _plan(service, name)
    if objective is not None and plan.get("allow_objective_override") is not True:
        raise PermissionError("wave plan does not allow an objective override")
    if host is not None and plan.get("allow_host_override") is not True:
        raise PermissionError("wave plan does not allow a host override")
    objective_task = plan.get("objective_task")
    resolved = []
    names: set[str] = set()
    for raw in plan["tasks"]:
        if not isinstance(raw, dict):
            raise ValueError("wave task must be an object")
        task_name = raw.get("name")
        repository = raw.get("repository")
        if not isinstance(task_name, str) or not task_name or task_name in names:
            raise ValueError("wave task names must be unique nonempty strings")
        names.add(task_name)
        repo = service._repository(repository)
        selected_host, workspace = service._host_workspace(repo, host or raw.get("host"))
        workspace_key = raw.get("workspace_key")
        if workspace_key is not None:
            registered = (repo.get("wave_workspaces") or {}).get(selected_host, {})
            workspace = registered.get(workspace_key) if isinstance(registered, dict) else None
            if not isinstance(workspace, str) or not Path(workspace).is_absolute():
                raise PermissionError("wave task workspace_key is not registered for the host")
        defaults = dict(repo.get("task_defaults") or {})
        role = raw.get("role", defaults.get("role", "implementation"))
        if role == "review" or role not in set(repo.get("allowed_roles") or ()):
            raise PermissionError("wave task role is not allowed by repository profile")
        task_objective = (objective if objective is not None and task_name == objective_task
                          else raw.get("objective"))
        if not isinstance(task_objective, str) or not task_objective.strip():
            raise ValueError(f"wave task {task_name} requires an objective")
        spec = {**defaults, "repo": repository, "workspace": workspace,
                "objective": task_objective, "host": selected_host, "mode": "wave",
                "role": role, "profile_id": raw.get("profile_id", defaults.get("profile_id")),
                "candidate_paths": list(raw.get("candidate_paths",
                                                 defaults.get("candidate_paths", ()))),
                "dependencies": list(raw.get("dependencies", ())),
                "workspace_kind": raw.get("workspace_kind", "checkout"),
                "wave_workspace_key": workspace_key,
                "source_reason": f"named-wave-plan:{name}",
                "source_confidence": "controller-registered"}
        resolved.append({"name": task_name,
                         "request_id": f"service-wave:{wave_id}:{task_name}", "spec": spec})
    for item in resolved:
        unknown = set(item["spec"]["dependencies"]) - names
        if unknown:
            raise ValueError(f"wave task has unknown dependencies: {sorted(unknown)}")
    handoffs = plan.get("handoffs") or []
    if not isinstance(handoffs, list):
        raise ValueError("wave handoffs must be a list")
    return resolved, handoffs


def submit(service, name: str, wave_id: str, *, objective: str | None = None,
           host: str | None = None) -> dict[str, Any]:
    tasks, handoffs = resolve(service, name, wave_id, objective=objective, host=host)
    record = WaveRunner(service.controller, service.token).submit_wave(wave_id, tasks, handoffs)
    binding = {"plan": name, "wave_id": wave_id, "tasks": tasks, "handoffs": handoffs,
               "resolved_digest": digest({"tasks": tasks, "handoffs": handoffs})}
    service.store.put_once("service_wave_plan", wave_id, binding)
    return {"plan": binding, "wave": record}


def _dispatch(service, wave_id: str, task_id: str, host: str) -> str:
    from .service_dispatch import launch
    from .store import canonical

    generation = continuation.current_generation(service.store, task_id)
    key = continuation.claim_key(task_id, generation)
    spec = service.controller.context(task_id)
    claim = {"wave_id": wave_id, "task": task_id, "host": host,
             "workspace": spec.get("workspace"), "cpu": spec.get("cpu", 1),
             "memory_mb": spec.get("memory_mb", 0), "generation": generation,
             "status": "launching"}
    with service.store.transaction() as db:
        row = db.execute(
            "SELECT value FROM records WHERE kind='wave_dispatch' AND key=?", (key,)).fetchone()
        if row:
            return __import__("json").loads(row[0]).get("status", "unknown")
        db.execute("INSERT INTO records VALUES('wave_dispatch',?,?)",
                   (key, canonical(claim)))
    try:
        identity = launch(service.controller_path, service.store.path.parent, task_id, host,
                          development_mode=service.development_mode)
    except Exception as error:
        service.store.replace("wave_dispatch", key,
                              {**claim, "status": "uncertain", "error": str(error)})
        return "uncertain"
    service.store.replace("wave_dispatch", key,
                          {**claim, "status": "active", "launcher_identity": identity})
    return "active"


def _cancel_settled(service, task_id: str, state: dict[str, Any]) -> bool:
    if state.get("status") != "cancelled":
        return False
    spec = service.controller.context(task_id)
    owner = service.store.ownership("workspace:" + str(Path(spec["workspace"]).resolve()))
    allocation = service.store.get("allocation", task_id)
    return ((owner is None or owner[2] == "released")
            and (allocation is None or allocation.get("active") is False))


def _reconcile_dispatches(service) -> set[str]:
    from .runtime_identity import process_status

    terminal = {"completed", "failed", "refused-before-launch", "cancelled", "reconciled"}
    reconciled = set()
    for key, record in service.store.records("wave_dispatch").items():
        if record.get("status") not in ("launching", "active", "uncertain"):
            continue
        task_id = record.get("task") or key
        generation = int(record.get("generation") or 1)
        state = (service.controller.status(service.token, task_id).get("state") or {})
        state_status = state.get("status")
        result = continuation.results(service.store, task_id).get(generation)
        completed = result is not None and result.get("accepted") is not None
        same_generation = continuation.generation_of(state) == generation
        if ((completed or (same_generation and state_status in terminal))
                and (state_status != "cancelled"
                     or _cancel_settled(service, task_id, state))):
            terminal_status = ("completed" if result and result.get("accepted")
                               else "failed" if result else state_status)
            service.store.replace("wave_dispatch", key,
                                  {**record, "status": "terminal",
                                   "terminal_status": terminal_status})
            reconciled.add(task_id)
            continue
        launcher = process_status(record.get("launcher_identity") or {})
        worker = process_status(state.get("worker_identity") or {})
        if launcher == "alive" or worker == "alive":
            continue
        if launcher == "unknown" or worker == "unknown":
            continue
        service.store.replace("wave_dispatch", key, {**record, "status": "uncertain",
                              "error": "wave dispatch identity is not observably alive; reconcile before retry"})
        reconciled.add(task_id)
    return reconciled


def progress(service) -> list[dict[str, Any]]:
    """Advance admitted waves in code without blocking on a model worker."""
    reconciled = _reconcile_dispatches(service)
    progressed = []
    runner = WaveRunner(service.controller, service.token)
    for wave_id, state in service.store.records("wave_state").items():
        wave = service.store.get("wave", wave_id) or {}
        task_ids = wave.get("task_ids") or []
        continuation_pending = any(
            continuation.pending_generation(service.store, task) is not None
            for task in task_ids)
        if (state.get("status") != "running" and not continuation_pending
                and not (state.get("status") == "blocked"
                         and reconciled.intersection(task_ids))):
            continue
        if not task_ids:
            continue
        hosts = {service.controller.context(task)["host"] for task in task_ids}
        if len(hosts) != 1:
            raise PermissionError("automatic wave progression requires one explicit execution host")
        host = next(iter(hosts))
        states = service.store.records("state")
        service_running = []
        from .service_specs import request_spec
        for event in service.store.records("service_event").values():
            if event.get("host") != host or event.get("status") not in {
                    "dispatching", "uncertain"}:
                continue
            task_state = states.get(event.get("task_id"), {})
            if task_state.get("status") not in {"running", "dispatching"}:
                service_running.append(request_spec(service.store, event))
        for reservation in running_resources(service, host):
            task_state = states.get(reservation.get("task"), {})
            if task_state.get("status") not in {"running", "dispatching"}:
                service_running.append(reservation)
        result = runner.step(
            wave_id, host, dispatcher=lambda w, task, h: _dispatch(service, w, task, h),
            additional_running=service_running, dispatch_limit=service.max_dispatch)
        progressed.append(result)
        break
    return progressed


def running_resources(service, host: str | None = None) -> list[dict[str, Any]]:
    """Expose launching wave reservations to the shared service capacity scheduler."""
    return [record for record in service.store.records("wave_dispatch").values()
            if (host is None or record.get("host") == host)
            and record.get("status") in ("launching", "active", "uncertain")]


def pending(service) -> bool:
    waves = service.store.records("wave")
    for wave_id, value in service.store.records("wave_state").items():
        if value.get("status") == "running":
            return True
        if any(continuation.pending_generation(service.store, task) is not None
               for task in (waves.get(wave_id) or {}).get("task_ids", ())):
            return True
    return False


def select_lane(store, *, service_ready: bool, wave_ready: bool) -> str:
    """Alternate lanes; caller must hold the durable service tick lease."""
    current = (store.get("scheduler_cursor", "service-wave") or {}).get("next", "service")
    if service_ready and wave_ready:
        selected = current if current in ("service", "wave") else "service"
        store.replace("scheduler_cursor", "service-wave",
                      {"next": "wave" if selected == "service" else "service"})
        return selected
    return "wave" if wave_ready else "service"
