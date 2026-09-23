"""Finite automatic dependent wave runner and digest-bound artifact handoff.

Progresses a finite approved wave from accepted producer to verified consumer through
automatic digest-checked artifact handoff, versioned preparation, and capacity-aware
admission -- without manual polling, copying, or redispatching.

Artifact handoff binds:
- Accepted producer generation and candidate SHA256 digest (read from immutable artifacts)
- Declared candidate output scope only
- Consumer base revision and destination path (refuses intervening workspace changes)
- Versioned preparation transition with durable intent/receipt and workspace locking
- Precise blocker on failure: corrupted/missing producer artifact blocks only affected
  dependents while independent ready tasks proceed.
"""
from __future__ import annotations
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .handoff import execute_handoff, HANDOFF_KIND, BLOCKED_KIND
from . import continuation, scheduler
from .store import digest
from .workspace import manifest, safe_path

WAVE_KIND = "wave"
WAVE_STATE_KIND = "wave_state"


@dataclass
class HandoffSpec:
    producer: str
    producer_path: str
    consumer: str
    consumer_path: str
    expected_digest: str | None = None
    commit_binding: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {"producer": self.producer, "producer_path": self.producer_path,
                "consumer": self.consumer, "consumer_path": self.consumer_path,
                "expected_digest": self.expected_digest, "commit_binding": self.commit_binding}


def handoff_key(producer_task: str, producer_path: str, consumer_task: str, consumer_path: str) -> str:
    return f"{producer_task}:{producer_path}->{consumer_task}:{consumer_path}"


def _detect_cycle(tasks: list[dict[str, Any]]) -> list[str] | None:
    """Return cycle path if dependency cycle detected, else None."""
    deps = {t["id"]: list(t.get("dependencies", [])) for t in tasks}
    visited: dict[str, int] = {}  # 0: unvisited, 1: visiting, 2: visited
    cycle: list[str] = []

    def dfs(node: str, stack: list[str]) -> bool:
        visited[node] = 1
        stack.append(node)
        for dep in deps.get(node, []):
            if dep not in deps:
                continue
            if visited.get(dep, 0) == 1:
                cycle.extend(stack[stack.index(dep):] + [dep])
                return True
            if visited.get(dep, 0) == 0 and dfs(dep, stack):
                return True
        stack.pop()
        visited[node] = 2
        return False

    for t in tasks:
        if visited.get(t["id"], 0) == 0:
            if dfs(t["id"], []):
                return cycle
    return None


def _latest_dispatches(store) -> dict[str, dict[str, Any]]:
    latest = {}
    for key, record in store.records("wave_dispatch").items():
        task = record.get("task") or key
        generation = int(record.get("generation") or 1)
        if generation >= int((latest.get(task) or {}).get("generation") or 0):
            latest[task] = record
    return latest


class WaveRunner:
    """Manages progression of a finite approved wave through deterministic code."""

    def __init__(self, controller, token: str):
        self.controller = controller
        self.token = token

    def submit_wave(self, wave_id: str, tasks: list[dict[str, Any]], handoffs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Submit all tasks in a wave after full validation; completely idempotent."""
        import copy
        tasks = copy.deepcopy(tasks)
        handoffs = copy.deepcopy(handoffs or [])

        # Validate DAG and detect cycles before modifying store
        cycle = _detect_cycle([{"id": t.get("name") or t["request_id"], "dependencies": t["spec"].get("dependencies", [])} for t in tasks])
        if cycle:
            raise ValueError(f"dependency cycle detected in wave: {' -> '.join(cycle)}")

        plan_digest = digest({"wave_id": wave_id, "tasks": tasks, "handoffs": handoffs})
        existing = self.controller.store.get(WAVE_KIND, wave_id)
        if existing:
            if existing.get("digest") != plan_digest:
                raise ValueError("conflicting wave identity")
            return existing

        submitted_tasks: dict[str, str] = {}
        for item in tasks:
            name = item.get("name") or item["request_id"]
            task_id = digest({"request": item["request_id"], "repo": item["spec"]["repo"]})
            submitted_tasks[name] = task_id

        for item in tasks:
            deps = item["spec"].get("dependencies", [])
            item["spec"]["dependencies"] = [submitted_tasks.get(d, d) for d in deps]
            if handoffs:
                requested = item["spec"].get("host") or self.controller.default_host
                if requested != self.controller.default_host:
                    raise PermissionError(f"split-host wave topology unsupported with handoffs; task requested {requested}")
            self.controller.submit(self.token, item["request_id"], item["spec"])

        # Resolve aliases and snapshot initial consumer states
        resolved_handoffs = []
        for h in handoffs:
            h_copy = dict(h)
            prod = submitted_tasks.get(h_copy["producer"], h_copy["producer"])
            cons = submitted_tasks.get(h_copy["consumer"], h_copy["consumer"])
            h_copy["producer"] = prod
            h_copy["consumer"] = cons

            # Snapshot consumer destination at submission time
            c_spec = self.controller.context(cons)
            c_root = Path(c_spec["workspace"]).resolve()
            safe_path(c_root, h_copy["consumer_path"])
            h_copy["consumer_initial_manifest"] = manifest(c_root, [h_copy["consumer_path"]])
            resolved_handoffs.append(h_copy)

        wave_record = {
            "wave_id": wave_id,
            "tasks": submitted_tasks,
            "task_ids": list(submitted_tasks.values()),
            "handoffs": resolved_handoffs,
            "digest": plan_digest,
        }
        self.controller.store.put_once(WAVE_KIND, wave_id, wave_record)
        self.controller.store.replace(WAVE_STATE_KIND, wave_id, {"status": "running", "wave_id": wave_id})
        return wave_record

    def step(self, wave_id: str, execution_host: str, dispatcher=None, *,
             running: list[dict[str, Any]] | None = None,
             capacity: dict[str, Any] | None = None,
             dispatch_limit: int | None = None) -> dict[str, Any]:
        """Advance wave by one deterministic step without model inference.

        The maintained service passes a ``dispatcher`` that launches detached workers, the
        store's active allocations as ``running`` and the controller's registered
        ``capacity``; without them the runner dispatches in-process against its own view.
        """
        wave = self.controller.store.get(WAVE_KIND, wave_id)
        if not wave:
            raise KeyError(f"Wave not found: {wave_id}")

        task_ids = wave["task_ids"]
        handoffs = wave.get("handoffs", [])

        # Evaluate pending handoffs
        for h in handoffs:
            key = handoff_key(h["producer"], h["producer_path"], h["consumer"], h["consumer_path"])
            existing = self.controller.store.get(HANDOFF_KIND, key)
            if existing and existing.get("status") == "bound":
                continue
            prod_result = continuation.current_result(self.controller.store, h["producer"])
            prod_state = self.controller.store.get("state", h["producer"]) or {}
            if prod_result and prod_result.get("accepted") and not prod_state.get("amended_objective_pending"):
                execute_handoff(self.controller, self.token, h)

        requests = self.controller.store.records("request")
        states = self.controller.store.records("state")
        blocked_records = self.controller.store.records(BLOCKED_KIND)

        def is_task_complete(t: str) -> bool:
            res = continuation.current_result(self.controller.store, t)
            st = states.get(t, {})
            if not res or not res.get("accepted"):
                return False
            if continuation.pending_generation(self.controller.store, t):
                return False
            if res.get("amendment_pending") or st.get("amended_objective_pending"):
                return False
            return True

        completed_tasks = [t for t in task_ids if is_task_complete(t)]
        wave_dispatches = {
            task: record for task, record in _latest_dispatches(self.controller.store).items()
            if int(record.get("generation") or 1)
            == continuation.current_generation(self.controller.store, task)
        }
        blocked_tasks = {
            t for t in task_ids
            if (blocked_records.get(t) or {}).get("status") == "blocked"
            or (wave_dispatches.get(t) or {}).get("status") == "uncertain"
            or ((wave_dispatches.get(t) or {}).get("status") == "terminal"
                and (wave_dispatches.get(t) or {}).get("terminal_status") != "completed")
        }

        # Find ready candidates
        candidates = []
        for t in task_ids:
            if t in completed_tasks or t in blocked_tasks:
                continue
            spec = self.controller.context(t)
            if not set(spec.get("dependencies", [])).issubset(set(completed_tasks)):
                continue

            inbound = [h for h in handoffs if h["consumer"] == t]
            all_bound = all(
                (self.controller.store.get(HANDOFF_KIND, handoff_key(h["producer"], h["producer_path"], h["consumer"], h["consumer_path"])) or {}).get("status") == "bound"
                for h in inbound
            )
            if not all_bound:
                continue

            st = states.get(t)
            dispatch_state = (wave_dispatches.get(t) or {}).get("status")
            if dispatch_state in ("launching", "active", "uncertain"):
                continue
            if st and st.get("status") in ("running", "dispatching", "completed"):
                if not continuation.pending_generation(self.controller.store, t):
                    continue
            if self.controller.store.get("cancel", t):
                continue

            candidates.append({
                **spec, "id": t,
                "paused": spec.get("pause_dispatch", False),
                "submitted": self.controller.store.get("initial", t)["submitted"],
                "route": (spec.get("selection", {}).get("profile") or {}).get("route", "deterministic"),
            })

        host_cfg = self.controller.hosts[execution_host]
        limits = capacity if capacity is not None else {
            "cpu": host_cfg.get("cpu", 4), "memory_mb": host_cfg.get("memory_mb", 8192)}
        host = {
            "cpu": limits["cpu"], "memory_mb": limits["memory_mb"],
            "interactive_boost_seconds": 10, "routes": [*host_cfg.get("routes", []), "deterministic"],
        }
        # A candidate larger than the whole registered host can never be admitted; it blocks
        # instead of keeping the wave running forever.
        oversized = {c["id"] for c in candidates
                     if c.get("cpu", 1) > host["cpu"] or c.get("memory_mb", 0) > host["memory_mb"]}
        blocked_tasks |= oversized
        candidates = [c for c in candidates if c["id"] not in oversized]
        if running is None:
            running = [requests[k] for k, v in states.items() if v.get("status") in ("running", "dispatching") and v.get("host") == execution_host]
        admitted = scheduler.ready(candidates, completed_tasks, running, host, time.time())
        if dispatch_limit is not None:
            admitted = admitted[:dispatch_limit]

        dispatched = []
        for task_id in admitted:
            if dispatcher is None:
                res = self.controller.run(self.token, task_id, execution_host=execution_host)
                status = res.get("state", {}).get("status")
            else:
                status = dispatcher(wave_id, task_id, execution_host)
            dispatched.append({"task": task_id, "status": status})

        task_summary = {
            t: {
                "status": ("accepted" if is_task_complete(t)
                           else (wave_dispatches.get(t) or {}).get("status")
                           or states.get(t, {}).get("status", "submitted")),
                "blocked": t in blocked_tasks,
                "blocker": ((blocked_records.get(t) or {}).get("reason")
                            or (wave_dispatches.get(t) or {}).get("error")
                            or (wave_dispatches.get(t) or {}).get("terminal_status")
                            or ("exceeds registered host capacity" if t in oversized else None))
                           if t in blocked_tasks else None,
            }
            for t in task_ids
        }
        all_done = all(v["status"] == "accepted" for v in task_summary.values())
        has_running = any(
            states.get(t, {}).get("status") in ("running", "dispatching")
            or (wave_dispatches.get(t) or {}).get("status") in ("launching", "active")
            for t in task_ids)
        # A ready task may be temporarily capacity-blocked by interactive work. That is
        # still a live wave; only a wave with no runnable candidate can be terminal.
        is_terminal = all_done or (not has_running and not candidates)

        wave_status = "completed" if all_done else ("blocked" if is_terminal else "running")
        self.controller.store.replace(WAVE_STATE_KIND, wave_id, {"status": wave_status, "wave_id": wave_id, "summary": task_summary})

        return {
            "wave_id": wave_id, "status": wave_status, "all_completed": all_done,
            "admitted": admitted, "dispatched": dispatched, "task_summary": task_summary,
            "blocked_tasks": sorted(blocked_tasks), "is_terminal": is_terminal,
        }

    def run(self, wave_id: str, execution_host: str, poll_interval: float = 0.05, timeout: float | None = None) -> dict[str, Any]:
        """Run the wave to terminal state without artificial caps or blind retry loops."""
        deadline = (time.time() + timeout) if timeout else None
        last_status: dict[str, Any] = {}
        while True:
            last_status = self.step(wave_id, execution_host)
            if last_status["is_terminal"]:
                break
            if deadline and time.time() >= deadline:
                last_status["status"] = "stopped"
                break
            time.sleep(poll_interval)
        return last_status
