"""Registered decision/worker/review episodes preserve task lifetime identity."""
from .profiles import resolve


def select_phase(controller, token, task, phase, *, role, request, previous=None):
    controller.authorize(token)
    spec = controller.store.get("request", task)
    if spec is None:
        raise KeyError(task)
    host = controller.hosts[spec["host"]]
    selected = resolve(controller.profiles, role=role, routes=host.get("routes", []), **request)
    old = controller.store.get("phase", task + ":" + previous) if previous else None
    old_profile = old["selection"]["profile"] if old else None
    profile = selected["profile"]
    if not old or profile == old_profile:
        session_action = "continue" if old else "new-session"
    elif profile and old_profile and profile["harness"] == old_profile["harness"] and profile["in_session_change"]:
        session_action = "supported-in-session-change"
    else:
        session_action = "new-session-with-saved-context"
    value = {"task": task, "phase": phase, "role": role, "selection": selected,
             "session_action": session_action, "context": controller.context(task),
             "acceptance": spec["verify"], "usage": None, "observed_runtime": None}
    controller.store.put_once("phase", task + ":" + phase, value)
    return value
