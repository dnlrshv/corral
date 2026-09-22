"""Dependency-ready capacity admission with aging; no inference or preemption."""
from .store import digest


def ready(tasks, completed, running, host, now):
    available_cpu = host["cpu"] - sum(t.get("cpu", 1) for t in running)
    available_memory = host["memory_mb"] - sum(t.get("memory_mb", 0) for t in running)
    candidates = [t for t in tasks if set(t.get("dependencies", [])) <= set(completed)
                  and t.get("due", 0) <= now and not t.get("paused")
                  and t["route"] in host["routes"]]
    # Finite interactive boost plus unbounded aging gives waves eventual priority.
    candidates.sort(key=lambda t: (-(now - t["submitted"] +
                                    (host["interactive_boost_seconds"] if t["mode"] == "interactive" else 0)),
                                    t["id"]))
    admitted = []
    for task in candidates:
        cpu, memory = task.get("cpu", 1), task.get("memory_mb", 0)
        if cpu <= 0 or memory < 0:
            raise ValueError("invalid resource request")
        if cpu <= available_cpu and memory <= available_memory:
            admitted.append(task["id"])
            available_cpu -= cpu
            available_memory -= memory
    return admitted


def due_occurrence(store, schedule, now):
    """Coalesce missed intervals to latest; schedule does not grant permissions."""
    interval = schedule["interval"]
    if interval <= 0:
        raise ValueError("interval must be positive")
    slot = int((now - schedule["start"]) // interval)
    if slot < 0:
        return None
    key = digest({"schedule": schedule["id"], "slot": slot})
    if not store.put_once("occurrence", key, {"slot": slot, "missed_policy": "coalesce-latest"}):
        return None
    return key
