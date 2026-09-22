"""Authenticated executor delegation beneath one logical controller authority.

The remote endpoint holds an attempt journal, not an independent scheduling
policy: it receives one explicitly authorized immutable task from the controller.
"""
import time
import uuid

from . import continuation
from .client import Client


def binding_for(controller, task_id, host, epoch):
    binding = controller.store.get("executor_binding", task_id)
    if binding is None:
        spec = controller.store.get("request", task_id)
        dependencies = [controller.store.get("executor_binding", dependency)
                        for dependency in spec.get("dependencies", [])]
        delegated = {**spec, "host": host["executor_host"], "logical_parent": task_id,
                     "authority_epoch": epoch, "dependencies": [value["task"] for value in dependencies if value]}
        response = Client(host["executor"]).call("submit", request_id="controller:" + task_id, spec=delegated)
        binding = {"task": response["task"], "epoch": epoch}
        controller.store.put_once("executor_binding", task_id, binding)
    return binding


def workspace_call(controller, task_id, action, **payload):
    spec = controller.store.get("request", task_id)
    host = controller.hosts[spec["host"]]
    resource = "remote-workspace:" + spec["host"] + ":" + spec["workspace"]
    if action in ("snapshot", "fetch-artifact"):
        binding = binding_for(controller, task_id, host, 0)
        return Client(host["executor"]).call(action, task_id=binding["task"], **payload)
    owner = "transfer:" + task_id + ":" + payload["transfer_id"]
    existing = controller.store.ownership(resource)
    epoch = existing[1] if existing and existing[0] == owner else controller.store.acquire(resource, owner)
    binding = binding_for(controller, task_id, host, epoch)
    try:
        result = Client(host["executor"]).call(action, task_id=binding["task"], **payload)
    except (RuntimeError, OSError, ConnectionError):
        controller.store.transition_owner(resource, owner, epoch, "uncertain")
        raise
    controller.store.transition_owner(resource, owner, epoch, "released")
    return result


def run_remote(controller, token, task_id, host):
    spec = controller.store.get("request", task_id)
    for dependency in spec.get("dependencies", []):
        result = continuation.current_result(controller.store, dependency)
        if not result or not result["accepted"]:
            raise PermissionError("logical-controller dependency not accepted")
        if continuation.pending_generation(controller.store, dependency) is not None:
            raise PermissionError("logical-controller dependency has a pending continuation")
    current = controller.store.get("state", task_id)
    pending_gen = continuation.pending_generation(controller.store, task_id)
    if pending_gen:
        generation = pending_gen
    else:
        generation = current.get("generation", 1) if current else 1

    if current and current["status"] == "completed" and generation == 1 and not pending_gen:
        return controller.status(token, task_id)
    if controller.store.get("cancel", task_id) and not current:
        return {**controller.status(token, task_id), "dispatch": "cancelled-before-start"}
    if controller.context(task_id).get("pause_dispatch") and not current:
        raise PermissionError("dispatch paused")
    remote = Client(host["executor"])
    resource = "remote-workspace:" + spec["host"] + ":" + spec["workspace"]
    epoch = controller.store.acquire(resource, task_id)
    if current is None or pending_gen is not None:
        attempt = str(uuid.uuid4())
        claim_k = continuation.claim_key(task_id, generation)
        try:
            controller.store.put_once("claim", claim_k, {"attempt": attempt, "generation": generation})
        except ValueError:
            return controller.status(token, task_id)
        current = {"status": "dispatching", "attempt": attempt, "epoch": epoch,
                   "host": spec["host"], "transport": "authenticated-executor-endpoint",
                   "generation": generation}
        controller.store.replace("state", task_id, current)
    try:
        binding = binding_for(controller, task_id, host, epoch)
        # Remote submit/dispatch are durable and idempotent. A lost response is
        # reconciled against the same task identity, never a new dev worker.
        for key, amendment in controller.status(token, task_id)["amendments"].items():
            remote.call("steer", task_id=binding["task"], amendment_id=key,
                        amendment=amendment["value"])
        remote.call("dispatch", task_id=binding["task"])
        current["status"] = "running"
        controller.store.replace("state", task_id, current)
        controller.store.put_once("invocation", current["attempt"], {
            "task": task_id, "generation": generation, "role": "controller", "usage": None,
            "executor_task": binding["task"], "profile": spec["selection"], "observed": None})
        while True:
            for key, amendment in controller.status(token, task_id)["amendments"].items():
                remote.call("steer", task_id=binding["task"], amendment_id=key,
                            amendment=amendment["value"])
            if controller.store.get("cancel", task_id):
                remote.call("cancel", task_id=binding["task"])
            snapshot = remote.call("status", task_id=binding["task"])
            remote_results = snapshot.get("results", {})
            remote_generations = snapshot.get("lineage", {}).get("generations", [])
            for remote_gen in remote_generations:
                g_remote = remote_gen["generation"]
                if g_remote >= generation and continuation.schedules(controller.store, task_id).get(g_remote) is None:
                    if remote_gen.get("continuation_id"):
                        cont_id = remote_gen["continuation_id"]
                        amend_key = f"{binding['task']}:{cont_id}"
                        objective = snapshot.get("amendments", {}).get(amend_key, {}).get("value", {}).get("objective", "")
                        record = {
                            "task": task_id, "continuation_id": cont_id, "generation": g_remote,
                            "objective_digest": remote_gen.get("objective_digest"),
                            "binding": None, "binding_digest": remote_gen.get("binding_digest"),
                            "remote_source": host.get("executor"),
                            "verifier_policy": remote_gen.get("verifier_policy", {}),
                            "prior": remote_gen.get("prior", {}),
                            "executor_task": binding["task"]
                        }
                        controller.store.put_once("continuation", f"{task_id}:{cont_id}", record)
                        controller.store.put_once("claim", continuation.claim_key(task_id, g_remote), {"attempt": remote_gen.get("attempt", ""), "generation": g_remote})
                        if objective:
                            seq = len(controller.store.records("amendment")) + 1
                            controller.store.put_once("amendment", f"{task_id}:{cont_id}", {"sequence": seq, "value": {"objective": objective}})
            if not remote_results and snapshot.get("result"):
                r_gen = snapshot["result"].get("generation", 1)
                remote_results = {str(r_gen): snapshot["result"]}

            for g_str, r_res in remote_results.items():
                g = int(g_str)
                if g >= generation and continuation.results(controller.store, task_id).get(g) is None:
                    result = {**r_res, "executor_task": binding["task"],
                              "logical_task": task_id, "transport": "authenticated-executor-endpoint",
                              "generation": g}
                    if controller.store.get("cancel", task_id):
                        result["accepted"] = False
                        result["logical_cancellation"] = "preserved despite executor completion race"
                    continuation.record_result(controller.store, task_id, g, result)
                    # Use a unique attempt key per generation recorded to deduplicate usage properly
                    usage_key = r_res.get("receipt", {}).get("attempt") or (snapshot["state"]["attempt"] + f"-{g}")
                    controller.store.put_once("remote_usage", usage_key, {
                        "task": task_id, "generation": g, "executor_task": binding["task"], "usage": result.get("usage")})

            if str(generation) in remote_results:
                new_state = {**current, "status": "completed",
                             "generation": generation, "executor_state": snapshot["state"]}
                if "amended_objective_pending" in snapshot["state"]:
                    new_state["amended_objective_pending"] = snapshot["state"]["amended_objective_pending"]
                controller.store.replace("state", task_id, new_state)
                controller.store.transition_owner(resource, task_id, epoch,
                    "uncertain" if controller.store.get("cancel", task_id) else "released")
                return controller.status(token, task_id)
            if snapshot["state"].get("status") == "uncertain":
                controller.store.replace("state", task_id, {**current, "status": "uncertain",
                    "executor_state": snapshot["state"]})
                return controller.status(token, task_id)
            time.sleep(.2)
    except (RuntimeError, OSError, ConnectionError) as error:
        controller.store.replace("state", task_id, {**current, "status": "uncertain",
                                                   "transport_error": type(error).__name__})
        # Keep the owner active so the SAME logical owner can reconcile later.
        return controller.status(token, task_id)
