"""Fair, capacity-aware dispatch for the durable service."""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from .scheduler import ready

TICK_RESOURCE = "service-scheduler:tick"


def acquire_tick(service, runtime: dict[str, Any]) -> tuple[str, int] | None:
    """Serialize scheduler ticks and recover a lease only after its process is proven dead."""
    from .runtime_identity import process_status

    owner = "service-tick:" + uuid.uuid4().hex
    service.store.put_once("service_tick_identity", owner, runtime)
    current = service.store.ownership(TICK_RESOURCE)
    if current is not None and current[2] != "released":
        prior = service.store.get("service_tick_identity", current[0])
        if not isinstance(prior, dict) or process_status(prior) != "dead":
            return None
        try:
            service.store.transition_owner(TICK_RESOURCE, current[0], current[1], "released")
        except PermissionError:
            return None
    try:
        return owner, service.store.acquire(TICK_RESOURCE, owner)
    except PermissionError:
        return None


def release_tick(service, lease: tuple[str, int]) -> None:
    owner, epoch = lease
    service.store.transition_owner(TICK_RESOURCE, owner, epoch, "released")


def _host_order(service) -> list[str]:
    hosts = list(service.controller.hosts)
    if not hosts:
        return []
    cursor = service.store.get("service_scheduler", "host_cursor") or {}
    last = cursor.get("last_host")
    if last not in hosts:
        return hosts
    start = (hosts.index(last) + 1) % len(hosts)
    return hosts[start:] + hosts[:start]


def _busy_workspaces(events: dict[str, dict[str, Any]]) -> set[str]:
    return {
        str(Path(value.get("resolved_spec", {}).get("workspace")).resolve())
        for value in events.values()
        if value.get("status") in {"dispatching", "uncertain"}
        and value.get("resolved_spec", {}).get("workspace")
    }


def dispatch_ready(service, events: dict[str, dict[str, Any]], now: float,
                   runtime: dict[str, Any]) -> list[str]:
    """Dispatch fairly across hosts while preserving per-host capacity and workspace fencing."""
    from .service_dispatch import launch

    dispatched: list[str] = []
    hosts = _host_order(service)
    while len(dispatched) < service.max_dispatch:
        made_progress = False
        for host in hosts:
            host_cfg = service.controller.hosts[host]
            busy = _busy_workspaces(events)
            pending = [
                {"id": key, "route": value["route"], "mode": value["mode"],
                 "submitted": value["submitted"], "due": 0,
                 "cpu": value.get("resolved_spec", {}).get("cpu", 1),
                 "memory_mb": value.get("resolved_spec", {}).get("memory_mb", 0)}
                for key, value in events.items()
                if value.get("host") == host and value.get("status") == "prepared"
                and str(Path(value.get("resolved_spec", {}).get("workspace")).resolve())
                not in busy
            ]
            running = [
                value for value in events.values()
                if value.get("host") == host
                and value.get("status") in {"dispatching", "uncertain"}
            ]
            capacity = {**host_cfg, "interactive_boost_seconds":
                        host_cfg.get("interactive_boost_seconds", 60)}
            eligible = ready(pending, set(), running, capacity, now)
            if not eligible:
                continue
            event_id = eligible[0]
            event = service._claim(event_id, now, runtime, scheduler_host=host)
            if event is None:
                current = service.store.get("service_event", event_id)
                if current is not None:
                    events[event_id] = current
                continue
            made_progress = True
            try:
                if host != service.execution_host and not host_cfg.get("executor"):
                    raise PermissionError(
                        "nonlocal host requires an authenticated remote executor route")
                launcher = launch(service.controller_path, service.store.path.parent,
                                  event["task_id"], host,
                                  development_mode=service.development_mode)
                updated = {**event, "launcher_identity": launcher}
            except Exception as exc:
                updated = {**event, "status": "uncertain", "error": str(exc)}
            service.store.replace("service_event", event_id, updated)
            events[event_id] = updated
            dispatched.append(event_id)
            if len(dispatched) >= service.max_dispatch:
                break
        if not made_progress:
            break
    return dispatched
