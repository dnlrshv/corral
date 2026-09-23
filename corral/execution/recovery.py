"""Explicit trusted reconciliation; never repeat an uncertain worker effect.

Reconciliation executes verification code, so it enforces the same binding, deception scan,
isolated import environment, integrity comparison and, for native tasks, the same verifier
containment as the dispatch path. A raw ``spec["verify"]`` subprocess is never run here: an
uncertain worker may have tampered with the verifier, and integrity that cannot be measured
is reported as unmeasured, not as true.

An attempt still recorded as dispatching or running belongs to the process that dispatched
it, which alone finishes it. Reconciliation settles such an attempt only once that process is
proven dead (its recorded birth identity names no live process), for example after a launcher
was killed, and then releases the attempt's workspace ownership and host allocation together
with its result in one transaction.
"""
import json
import socket
import time
from pathlib import Path

from . import completion, continuation, routes, verifier, verifier_containment
from .store import digest
from .workspace import manifest

#: Attempt states that only the dispatching process moves on from.
OPEN_STATES = ("dispatching", "running")


def _abandoned_dispatch(state):
    """Prove that the process that dispatched a still-open attempt can no longer finish it.

    An attempt the dispatcher handed over (``uncertain``) needs no proof: its ownership is
    already fenced for reconciliation. An open attempt is settled only when the dispatcher's
    recorded birth identity is observed dead on this host; a live, unobservable, foreign or
    unrecorded dispatcher refuses, so a running dispatch is never settled underneath itself.
    """
    if state["status"] not in OPEN_STATES:
        return None
    from .runtime_identity import process_status

    identity = state.get("dispatcher_identity")
    status = "unrecorded"
    if isinstance(identity, dict) and identity:
        status = (process_status(identity) if identity.get("host") == socket.gethostname()
                  else "observed-on-another-host")
    if status != "dead":
        raise PermissionError(
            f"attempt is still {state['status']} under its dispatcher, which is not proven dead "
            f"({status}); wait for it or stop it before reconciling")
    return {"identity": identity, "status": "dead", "attempt_status": state["status"]}


def _verifier_boundary(controller, spec, host, *, task, generation, attempt, directory, workspace):
    """Rebuild the dispatch's verifier containment for a native re-run, or refuse.

    The re-run executes candidate code exactly as dispatch verification does, so it gets the
    same boundary: controller state, artifacts, host-protected paths and the provider secret
    file denied, and no network unless the host opts in. An inspection-only route never
    executes candidate code at all, so it has nothing to re-run here.
    """
    if not completion.is_native(spec):
        return {}
    declared = (spec.get("selection") or {}).get("profile") or {}
    profile = next((item for item in controller.profiles if item.id == declared.get("id")), None)
    route = routes.declared_routes(host).get(profile.route) if profile is not None else None
    if route is None:
        raise PermissionError("native reconciliation requires its controller-registered route")
    if route.inspection_only:
        raise PermissionError("inspection-only tasks never execute candidate code; cancel the "
                              "task and reconcile to settle it")
    prepared = verifier_containment.prepare(
        workspace=workspace, state_dir=controller.store.path.parent,
        artifacts=controller.artifacts, task_dir=directory,
        task_id=continuation.scratch_id(task, generation), attempt=attempt + "-reconciliation",
        protected_paths=tuple(host.get("protected_paths") or ()),
        probe_sentinels=tuple(host.get("verifier_probe_sentinels") or ()),
        network=verifier_containment.network_opt_in(host))
    return {"seatbelt_profile": prepared.profile, "containment_evidence": prepared.evidence,
            "containment_env": {"TMPDIR": prepared.boundary.tmpdir,
                                "HOME": prepared.boundary.scratch}}


def _structured_for(spec, workspace, directory):
    """Read the structured completion from the trusted channel for this dispatch kind."""
    structured, _adapter_result = completion.structured(directory, workspace, spec,
                                                        completion.is_native(spec))
    return structured


def _terminal_observation(value):
    """Accept only a controller-authenticated, evidence-shaped settlement observation.

    A cancellation must never be released because a caller supplied a convenient Boolean.
    The controller records concrete process, artifact and external-delivery observations so
    a later audit can distinguish a stopped worker from an unobserved publication effect.
    """
    if not isinstance(value, dict):
        raise PermissionError("reconciliation observation must be an object")
    process, artifact, delivery = (value.get(name) for name in ("process", "artifact", "delivery"))
    if not all(isinstance(item, dict) for item in (process, artifact, delivery)):
        raise PermissionError("reconciliation requires process, artifact and delivery evidence")
    if not isinstance(process.get("identity"), dict) or not process["identity"]:
        raise PermissionError("reconciliation process evidence requires a recorded identity")
    if process.get("status") not in ("absent", "exited", "terminated"):
        raise PermissionError("reconciliation process is not conclusively terminal")
    if artifact.get("status") not in ("preserved", "unavailable"):
        raise PermissionError("reconciliation artifact outcome is unobserved")
    if delivery.get("status") not in ("absent", "delivered") or delivery.get("searched") is not True:
        raise PermissionError("reconciliation delivery effect must be read back")
    receipt = {"schema": "corral-reconciliation-observation-v1", "observed_at": time.time(),
               "process": process, "artifact": artifact, "delivery": delivery}
    receipt["digest"] = digest({key: value for key, value in receipt.items() if key != "digest"})
    return receipt


def _cancelled(controller, task, state, observation, dispatcher=None):
    """Persist a rejected cancellation without executing a verifier or candidate code."""
    settled = _terminal_observation(observation)
    generation = continuation.generation_of(state)
    directory = continuation.artifact_dir(controller.artifacts, task, generation)
    directory.mkdir(parents=True, exist_ok=True)
    usage_path = directory / "usage.json"
    usage = (json.loads(usage_path.read_text()) if usage_path.exists()
             else {"coverage": "unknown-after-cancelled-interruption"})
    result = {"structured": None, "accepted": False, "terminal_status": "cancelled",
              "reason": "cancelled-after-evidence-bound-reconciliation", "receipt": {
                  "authority": "controller-cancellation-reconciliation",
                  "observation": settled, "worker_restarted": False,
                  "verifier_executed": False, "candidate_execution": "not-repeated"},
              "usage": usage, "endpoint": state.get("endpoint", "local"), "generation": generation,
              "artifact_directory": str(directory), "attempt": state.get("attempt")}
    if dispatcher is not None:
        result["receipt"]["dispatcher"] = dispatcher
    # The event is separate from the historical terminal task state.  It is useful for
    # accounting without recasting a cancelled task as successful or merely "reconciled".
    audit = {
        "event": "cancelled-reconciled", "task": task, "generation": generation,
        "attempt": state.get("attempt"), "observation": settled, "result_digest": digest(result)}
    resource = "workspace:" + str(Path(controller.context(task)["workspace"]).resolve())
    # A dead dispatcher never moved its ownership from active to uncertain; the attempt's
    # own epoch and unchanged state still fence the release.
    controller.store.settle_cancelled(resource=resource, owner=task, epoch=state["epoch"], task=task,
                                      generation=generation, result=result, audit=audit, expected_state=state,
                                      state={**state, "status": "cancelled",
                                             "reconciliation": "cancelled-reconciled"},
                                      owner_from=("active", "uncertain") if dispatcher else ("uncertain",))
    _materialize_cancelled_receipt(result)
    return result


def _materialize_cancelled_receipt(result):
    """Publish the convenience receipt only from the durable cancellation result."""
    directory = Path(result["artifact_directory"])
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / "cancellation-reconciliation.json"
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(json.dumps(result, indent=2, sort_keys=True))
    temporary.replace(destination)


def reconcile(controller, token, task, probe):
    """Probe is controller-installed code, never a field from a worker result."""
    controller.authorize(token)
    state = controller.store.get("state", task)
    existing = continuation.current_result(controller.store, task)
    if existing and existing.get("terminal_status") == "cancelled":
        _materialize_cancelled_receipt(existing)
        return existing
    if not state or state["status"] not in ("uncertain", *OPEN_STATES):
        raise ValueError("task has no uncertain execution to reconcile")
    dispatcher = _abandoned_dispatch(state)
    observation = probe(task, dict(state))
    if controller.store.get("cancel", task):
        return _cancelled(controller, task, state, observation, dispatcher)
    if not observation.get("execution_stopped") or not observation.get("effects_reconciled"):
        return {"status": "uncertain", "observation": observation, "worker_restarted": False}
    generation = continuation.generation_of(state)
    # Reconcile the interrupted generation by its own binding and in its own artifact
    # directory: a continued generation must never be judged by, or write into, the accepted
    # prior generation's receipts.
    spec = continuation.effective_spec(controller, task, generation)
    workspace = spec["workspace"]
    host = controller.trusted_host(spec["host"]) if spec["host"] in controller.hosts else {}
    attempt = str(state.get("attempt") or "unknown-attempt")
    directory = continuation.artifact_dir(controller.artifacts, task, generation)
    directory.mkdir(parents=True, exist_ok=True)
    boundary = _verifier_boundary(controller, spec, host, task=task, generation=generation,
                                  attempt=attempt, directory=directory, workspace=workspace)

    # Same trust rules as dispatch: the executed verifier must be controller-bound, and a
    # workspace hijack file (conftest.py, sitecustomize.py, Makefile, ...) refuses the run.
    policy = verifier.policy(spec, workspace, host=host, worker_writable=(workspace,))
    bound = verifier.bound_bundle(policy, workspace)
    persisted = state.get("verifier_bundle")
    if bound and persisted is not None:
        verifier_intact = persisted == bound
    else:
        # Either nothing is bound to measure (an inline controller-authored command) or the
        # pre-dispatch digest was never persisted. Neither may be reported as intact.
        verifier_intact = None
    workspace_provenance = state.get("workspace_provenance")
    before = manifest(workspace, spec["candidate_paths"], workspace_provenance)
    record = verifier.execute(policy, workspace, candidate_paths=spec["candidate_paths"],
                              task=task, attempt=attempt,
                              pre_verifier_manifest=(persisted if bound and persisted is not None
                                                     else None), workspace_provenance=workspace_provenance,
                              timeout=verifier.timeout_for(host), **boundary)
    receipt = dict(record.payload)
    if verifier_intact is None and bound:
        receipt["verifier_intact"] = None
        receipt["policy_ok"] = False
        receipt["refused"] = ("verifier bundle integrity is unmeasured after interruption; "
                              "no pre-dispatch digest was persisted")
    receipt.update({"observation": observation, "worker_restarted": False,
                    "authority": "controller-reconciliation-verifier",
                    "endpoint": spec["endpoint"], "candidate": before["digest"],
                    "recovered_from_status": state["status"]})
    if dispatcher is not None:
        receipt["dispatcher"] = dispatcher
    structured = _structured_for(spec, workspace, directory)
    integrity_ok = receipt["verifier_intact"] is not False and not (verifier_intact is None
                                                                    and bound)
    accepted = bool(receipt["exit_code"] == 0 and receipt["unchanged"] and receipt["policy_ok"]
                    and integrity_ok and not controller.store.get("cancel", task))
    (directory / "recovery-receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True))
    (directory / "recovery-verify.stdout").write_bytes(record.stdout)
    (directory / "recovery-verify.stderr").write_bytes(record.stderr)
    usage_path = directory / "usage.json"
    # Usage from before the interruption is preserved as-is; absence stays unknown, not zero.
    usage = (json.loads(usage_path.read_text()) if usage_path.exists()
             else {"coverage": "unknown-after-interruption"})
    result = {"structured": structured, "accepted": accepted, "receipt": receipt,
              "usage": usage, "endpoint": spec["endpoint"], "generation": generation,
              "artifact_directory": str(directory)}
    # One transaction: the result, the reconciled state and the release of exactly this
    # attempt's workspace fence and allocation, refused if the state changed meanwhile.
    controller.store.finish_attempt(task=task, resource="workspace:" + str(Path(workspace).resolve()),
                                    epoch=state["epoch"], state={**state, "status": "reconciled"},
                                    expected_state=state, result=result, owner_status="released",
                                    owner_from=("active", "uncertain"), release_allocation=True)
    return result
