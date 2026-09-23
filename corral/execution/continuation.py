"""Post-terminal continuation: one aggregate task identity, exactly one new invocation.

A submitted request is immutable, so ``steer`` can only refine an objective before the first
dispatch and ``run`` returns the recorded state forever afterwards. This module adds the
explicitly authorized alternative for a task that already reached a terminal attempt: the
authenticated client checkpoints that attempt and schedules exactly one subsequent generation
under the SAME public task id, with its own objective, verifier policy, candidate declaration,
artifact directory, native session and usage attribution.

Continuation grants no authority. Host, endpoint, workspace, profile/model/route, role,
resource requests, dependencies and every credential declaration stay bound to the immutable
submitted request; a payload that names any of them is refused instead of merged, so a stage 2
verifier and a new objective are selectable while execution authority is inherited. A fresh
native session carrying a compact checkpoint of the prior terminal attempt is not in-session
retuning and is never reported as such.

Nothing here retries, caps or resumes on its own: each generation is one explicit operational
action, a repeated continuation id deduplicates, a restart re-reads durable state, and a lock
is never cleared for active, unresolved or cancel-unknown work.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import prelaunch_refusal, routes, verifier, workspace_contract
from .store import canonical, digest
from .workspace import safe_path

#: The only keys a continuation may bind for its own generation.
BINDING_KEYS = ("verify", "verifier_paths", "external_verifier", "candidate_paths",
                "result_file", "usage_file")
TERMINAL_STATUSES = ("completed", "reconciled", "refused-before-launch")
KIND = "continuation"
RESULT_KIND = "result"
GENERATION_RESULT_KIND = "generation_result"


# --------------------------------------------------------------------------- identity

def generation_of(state) -> int:
    """Generation of a state record; records written before continuation exist default to 1."""
    try:
        return max(1, int((state or {}).get("generation") or 1))
    except (TypeError, ValueError):
        return 1


def artifact_dir(artifacts, task_id: str, generation: int) -> Path:
    """Generation 1 keeps its original immutable path; later generations get a fresh one.

    A unique directory per generation is what stops a previous generation's
    ``adapter-result.json``, ``structured.json`` or ``usage.json`` from masquerading as the
    result of the new invocation, and keeps the accepted stage 1 artifact addressable.
    """
    root = Path(artifacts)
    return root / task_id if generation <= 1 else root / f"{task_id}-g{generation}"


def scratch_id(task_id: str, generation: int) -> str:
    """Per-generation native scratch/session identity; the public task id is unchanged."""
    return task_id if generation <= 1 else f"{task_id}-g{generation}"


def claim_key(task_id: str, generation: int) -> str:
    """One worker claim per generation, so a repeat dispatch cannot start a second worker."""
    return task_id if generation <= 1 else f"{task_id}:g{generation}"


# --------------------------------------------------------------------------- records

def results(store, task_id: str) -> dict:
    """generation -> immutable result record. Generation 1 keeps the original record kind."""
    found = {}
    first = store.get(RESULT_KIND, task_id)
    if first is not None:
        found[1] = first
    prefix = task_id + ":"
    for key, value in store.records(GENERATION_RESULT_KIND).items():
        if key.startswith(prefix) and key[len(prefix):].isdigit():
            found[int(key[len(prefix):])] = value
    return dict(sorted(found.items()))


def current_result(store, task_id: str):
    """The task's current result: the latest generation that has one, else None."""
    found = results(store, task_id)
    return found[max(found)] if found else None


def record_result(store, task_id: str, generation: int, result: dict) -> bool:
    """Persist one generation's result without ever overwriting a historical receipt."""
    if generation <= 1:
        return store.put_once(RESULT_KIND, task_id, result)
    return store.put_once(GENERATION_RESULT_KIND, f"{task_id}:{generation}", result)


def _records(store, kind: str, db=None) -> dict:
    """All records of one kind, read inside the caller's open transaction when given."""
    if db is None:
        return store.records(kind)
    return {key: json.loads(value) for key, value in db.execute(
        "SELECT key,value FROM records WHERE kind=? ORDER BY key", (kind,))}


def schedules(store, task_id: str, *, db=None) -> dict:
    """generation -> scheduled continuation record, ordered by generation."""
    found = {}
    for key, value in _records(store, KIND, db).items():
        if value.get("task") == task_id and key.startswith(task_id + ":"):
            found[int(value["generation"])] = value
    return dict(sorted(found.items()))


def current_generation(store, task_id: str) -> int:
    scheduled = schedules(store, task_id)
    return max(scheduled) if scheduled else 1


def pending_generation(store, task_id: str, *, db=None):
    """The one scheduled generation that no worker has claimed yet, else None."""
    claims = _records(store, "claim", db)
    for generation in schedules(store, task_id, db=db):
        if generation > 1 and claim_key(task_id, generation) not in claims:
            return generation
    return None


def attempts_by_generation(store, task_id: str) -> dict:
    """generation -> attempt id, from the per-invocation records (absent generation means 1)."""
    found = {}
    for attempt, value in store.records("invocation").items():
        if value.get("task") == task_id:
            found.setdefault(generation_of(value), attempt)
    return dict(sorted(found.items()))


def effective_spec(controller, task_id: str, generation: int | None = None) -> dict:
    """Immutable request + amendment history + every binding declared up to this generation."""
    spec = controller.context(task_id)
    for entry, record in schedules(controller.store, task_id).items():
        if generation is not None and entry > generation:
            continue
        if record.get("binding"):
            spec.update(record["binding"])
    return spec


# --------------------------------------------------------------------------- validation

def _string_list(value, label: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{label} must be a list of strings")
    return list(value)


def _validated_binding(amendment: dict) -> dict:
    """Fail closed on anything that is not a per-generation execution binding.

    This is an allow-list, not a deny-list: an unlisted key is refused whether or not it looks
    like authority, so nothing can be smuggled in under a name this module never considered.
    """
    unknown = sorted(set(amendment) - set(BINDING_KEYS) - {"objective"})
    if unknown:
        raise PermissionError(
            "continuation cannot grant or replace execution authority; refused keys: "
            f"{', '.join(unknown)}. Host, endpoint, workspace, profile/model/route, role, "
            "resources, dependencies and credentials stay bound to the immutable submitted "
            f"request; only objective and {', '.join(BINDING_KEYS)} are rebound per generation.")
    binding = {key: json.loads(json.dumps(amendment[key])) for key in BINDING_KEYS if key in amendment}
    for key in ("candidate_paths", "verifier_paths"):
        if key in binding:
            _string_list(binding[key], key)
    if "external_verifier" in binding and not isinstance(binding["external_verifier"], bool):
        raise ValueError("external_verifier must be a boolean")
    for key in ("result_file", "usage_file"):
        if key in binding and not isinstance(binding[key], str):
            raise ValueError(f"{key} must be a string")
    return binding


def _validate_policy(controller, task_id: str, binding: dict) -> dict:
    """Bind the generation's verifier with the same host policy as the original submission."""
    spec = {**effective_spec(controller, task_id), **binding}
    workspace = spec["workspace"]
    host = controller.hosts.get(spec["host"]) or {}
    selected = (spec.get("selection") or {}).get("profile") or {}
    profile = next((item for item in controller.profiles
                    if item.id == selected.get("id")), None)
    route = routes.declared_routes(host).get(profile.route) if profile else None
    if route and route.inspection_only:
        if binding:
            raise PermissionError(
                "inspection continuation inherits its controller-bound packet policy; only "
                "the objective may be amended")
        provenance = workspace_contract.preflight(spec, workspace, store=controller.store)
        core = {"kind": "controller-inspection-report", "script": None,
                "verifier_root": None, "verifier_paths": [],
                "trusted_export_id": spec.get("trusted_export_id"),
                "profile_id": profile.id, "route": route.id,
                "provenance_digest": provenance["digest"]}
        return {**core, "digest": digest(core)}
    candidates = list(spec.get("candidate_paths") or ())
    declared = list(spec.get("verifier_paths") or ())
    if set(candidates) & set(declared):
        raise PermissionError("candidate_paths must not overlap with verifier_paths")
    for name in [*candidates, *declared, spec.get("result_file", "result.json"),
                 spec.get("usage_file", "usage.json")]:
        safe_path(workspace, name)
    policy = verifier.policy(spec, workspace, host=host, worker_writable=(workspace,))
    return {"kind": policy.kind, "script": policy.script, "verifier_root": policy.verifier_root,
            "verifier_paths": list(policy.verifier_paths), "digest": digest(policy.as_dict())}


def _ownership_refusal(controller, task_id: str, request: dict, state: dict) -> None:
    """A lock is never cleared here: active, uncertain or cancelled work must reconcile first."""
    if controller.store.get("cancel", task_id):
        raise PermissionError("task has a recorded cancellation and Corral has no un-cancel; its "
                              "outcome stays cancel-unknown until reconciled, so no new "
                              "generation may be scheduled")
    if state.get("status") not in TERMINAL_STATUSES:
        raise PermissionError(
            f"cannot continue an execution whose status is {state.get('status')!r}; only a "
            f"terminal attempt ({', '.join(TERMINAL_STATUSES)}) may be amended")
    host = controller.hosts.get(request["host"]) or {}
    if host.get("executor"):
        resource = "remote-workspace:" + request["host"] + ":" + request["workspace"]
    else:
        resource = "workspace:" + str(Path(request["workspace"]).resolve())
    row = controller.store.ownership(resource)
    if row is not None and row[2] != "released":
        raise PermissionError(
            f"workspace ownership is {row[2]!r} for owner {row[0]!r}; unresolved or "
            "cancel-unknown work must be reconciled before continuation, and the lock is "
            "never cleared to make room for a new generation")


# --------------------------------------------------------------------------- checkpoint

def _checkpoint(task_id: str, generation: int, state: dict, result: dict) -> dict:
    """Compact, digest-shaped record of the terminal attempt a new generation follows."""
    receipt = result.get("receipt") or {}
    policy = receipt.get("policy") or {}
    usage = result.get("usage") or {}
    return {"task": task_id, "generation": generation, "attempt": state.get("attempt"),
            "status": state.get("status"), "accepted": result.get("accepted"),
            "artifact_directory": result.get("artifact_directory"),
            "receipt_digest": digest(receipt),
            "candidate": {"pre": receipt.get("candidate_pre"), "post": receipt.get("candidate_post"),
                          "unchanged": receipt.get("unchanged")},
            "verifier": {**{key: policy.get(key) for key in ("kind", "script")},
                         **{key: receipt.get(key) for key in ("exit_code", "verifier_intact")},
                         "bundle_digest": digest(receipt.get("verifier_bundle") or {})},
            "structured_digest": digest(result.get("structured")),
            "observed": result.get("observed"),
            "usage": {key: usage.get(key) for key in ("observed_fields", "measured_fields",
                                                      "synthetic_fields", "coverage",
                                                      "publication")}}


def checkpoint_text(checkpoint: dict) -> str:
    """The compact checkpoint a fresh native session is given about the same aggregate task."""
    if not checkpoint:
        return ""
    if checkpoint.get("schema") == "corral-prelaunch-refusal-checkpoint-v1":
        return "\n".join([
            f"Continuation of the SAME aggregate task identity: generation "
            f"{checkpoint['generation'] + 1} follows generation {checkpoint['generation']}, "
            "which was refused before any worker or provider process launched.",
            "The refused attempt, claim, controller invocation record and released ownership "
            "remain immutable. It produced no candidate result, model output or usage.",
            "This is a fresh session after an operator-corrected preflight condition; do not "
            "describe the refused generation as completed, accepted, resumed or retried.",
            f"Prelaunch evidence digest: {checkpoint['digest']}"])
    prior = {key: checkpoint.get(key) for key in ("attempt", "verifier", "candidate", "usage")}
    return "\n".join([
        f"Continuation of the SAME aggregate task identity: generation "
        f"{checkpoint['generation'] + 1} follows terminal generation {checkpoint['generation']} "
        f"(accepted={checkpoint['accepted']}, status={checkpoint['status']}).",
        "This is a fresh session with a compact checkpoint, not an in-session retune: the prior "
        "attempt is finished, its receipts are immutable and are not re-executed here.",
        f"Prior attempt/verifier/candidate/usage: {json.dumps(prior, sort_keys=True, default=str)}",
        f"Prior structured completion digest: {checkpoint['structured_digest']}",
        "Build on the accepted prior result; do not restart it or rewrite its artifacts."])


def checkpoint_for(store, task_id: str, generation: int):
    """The checkpoint recorded when this generation was scheduled (None for generation 1)."""
    record = schedules(store, task_id).get(generation)
    return (record or {}).get("prior")


# --------------------------------------------------------------------------- scheduling

def _schedule_atomically(store, task_id: str, continuation_id: str, record: dict,
                         objective: str) -> bool:
    """Write the generation and its objective amendment in one transaction, or deduplicate.

    The generation slot is recomputed inside the write fence, so two different continuation
    ids racing for the same task cannot both claim one generation, and a second continuation
    cannot queue behind one that is scheduled but not yet dispatched.
    """
    key = f"{task_id}:{continuation_id}"
    with store.transaction() as db:
        row = db.execute("SELECT value FROM records WHERE kind=? AND key=?", (KIND, key)).fetchone()
        if row:
            if json.loads(row[0]) != record:
                raise ValueError("conflicting continuation identity")
            return False
        prior = record.get("prior") or {}
        if prior.get("schema") == "corral-prelaunch-refusal-checkpoint-v1":
            resource = "workspace:" + str(Path(
                json.loads(db.execute(
                    "SELECT value FROM records WHERE kind='request' AND key=?", (task_id,)
                ).fetchone()[0])["workspace"]).resolve())
            if prelaunch_refusal.prove_db(
                    db, task_id, int(prior["generation"]), resource) != prior:
                raise PermissionError("prelaunch refusal evidence changed before continuation")
        claims = {claimed for (claimed,) in db.execute("SELECT key FROM records WHERE kind='claim'")}
        taken = set()
        for (raw,) in db.execute("SELECT value FROM records WHERE kind=?", (KIND,)):
            value = json.loads(raw)
            if value.get("task") == task_id:
                taken.add(int(value["generation"]))
        undispatched = sorted(item for item in taken
                              if item > 1 and claim_key(task_id, item) not in claims)
        if undispatched:
            raise PermissionError(
                f"generation {undispatched[0]} is already scheduled and not yet dispatched; "
                "dispatch or reconcile it first, so exactly one invocation follows one action")
        if max([1, *taken]) + 1 != record["generation"]:
            raise PermissionError(
                "continuation state changed while this request was prepared; re-read task "
                "status and issue the continuation again")
        db.execute("INSERT INTO records VALUES(?,?,?)", (KIND, key, canonical(record)))
        old = db.execute("SELECT value FROM records WHERE kind='amendment' AND key=?", (key,)).fetchone()
        if old:
            if json.loads(old[0])["value"] != {"objective": objective}:
                raise ValueError("conflicting amendment identity")
        else:
            sequence = db.execute("SELECT COUNT(*) FROM records WHERE kind='amendment'").fetchone()[0] + 1
            db.execute("INSERT INTO records VALUES(?,?,?)",
                       ("amendment", key,
                        canonical({"sequence": sequence, "value": {"objective": objective}})))
        return True


def _scheduled_by_id(store, task_id: str, continuation_id: str):
    """The generation already scheduled under this continuation id, if any."""
    return store.get(KIND, f"{task_id}:{continuation_id}")


def schedule(controller, token, task_id: str, continuation_id: str, amendment: dict) -> dict:
    """Authenticated continuation: checkpoint the terminal attempt, schedule one generation."""
    controller.authorize(token)
    request = controller.store.get("request", task_id)
    if request is None:
        raise KeyError(task_id)
    if not isinstance(continuation_id, str) or not continuation_id.strip():
        raise ValueError("continuation_id must be a non-empty string")
    if not isinstance(amendment, dict):
        raise ValueError("continuation must be an object")
    objective = amendment.get("objective")
    if not isinstance(objective, str) or not objective.strip():
        raise ValueError("continuation requires an explicit non-empty objective; nothing is inferred")
    if amendment.get("native_resume") or amendment.get("resume") or amendment.get("session_resume"):
        raise PermissionError("native session resume is unsupported; fresh checkpoint-based generations are supported")
    binding = _validated_binding(amendment)
    # A repeated request deduplicates on its own id whatever happened afterwards: a lost ack
    # must never become a second worker, and a reused id must never carry a different payload.
    already = _scheduled_by_id(controller.store, task_id, continuation_id)
    if already is not None:
        if ((already["objective_digest"], already["binding_digest"])
                != (digest(objective), digest(binding))):
            raise ValueError("conflicting continuation identity")
        # Ensure we still send the remote intent if we are continuing via remote executor,
        # in case the previous request lost the ACK or dropped before reaching the executor.
        host = controller.hosts.get(request["host"]) or {}
        if host.get("executor"):
            from .remote import Client, binding_for
            host_cfg = controller.hosts[request["host"]]
            binding_info = binding_for(controller, task_id, host_cfg, 0)
            Client(host_cfg["executor"]).call("continue", task_id=binding_info["task"],
                                              continuation_id=continuation_id,
                                              continuation=amendment)
        return {"scheduled": False, "generation": already["generation"], "continuation": already,
                "dispatch": "already scheduled by this continuation id; no new invocation",
                **controller.status(token, task_id)}

    state = controller.store.get("state", task_id)
    if state is None:
        raise PermissionError(
            "task has no dispatched attempt to continue; steer it before the first dispatch")
    _ownership_refusal(controller, task_id, request, state)
    if pending_generation(controller.store, task_id) is not None:
        raise PermissionError(
            "a continuation generation is already scheduled and not yet dispatched; dispatch or "
            "reconcile it before scheduling another, so exactly one invocation follows one action")
    prior = current_generation(controller.store, task_id)
    recorded = results(controller.store, task_id).get(prior)
    if recorded is None:
        checkpoint = prelaunch_refusal.prove(controller, task_id, prior, request)
    else:
        checkpoint = _checkpoint(task_id, prior, state, recorded)

    host = controller.hosts.get(request["host"]) or {}
    if host.get("executor"):
        from .remote import Client, binding_for

        host_cfg = controller.hosts[request["host"]]
        binding_info = binding_for(controller, task_id, host_cfg, 0)
        record = {"task": task_id, "continuation_id": continuation_id, "generation": prior + 1,
                  "objective_digest": digest(objective), "binding": binding,
                  "binding_digest": digest(binding), "verifier_policy": {}, "prior": checkpoint,
                  "executor_task": binding_info["task"]}

        # Durable fenced controller continuation intent before remote side effects
        created = _schedule_atomically(controller.store, task_id, continuation_id, record, objective)

        # Lost ack replay safe: remote continue is idempotent on continuation_id
        remote_client = Client(host_cfg["executor"])
        remote_resp = remote_client.call("continue", task_id=binding_info["task"],
                                         continuation_id=continuation_id,
                                         continuation=amendment)

        # We can update the local record with the remote policy now if we want,
        # but the remote executor enforces it anyway.
        if created:
            policy = remote_resp.get("continuation", {}).get("verifier_policy") or {}
            if policy:
                record["verifier_policy"] = policy
                controller.store.replace(KIND, f"{task_id}:{continuation_id}", record)

        return {"scheduled": created, "generation": record["generation"], "continuation": record,
                "dispatch": "explicit operational action required; nothing is retried automatically",
                **controller.status(token, task_id)}

    policy = _validate_policy(controller, task_id, binding)
    record = {"task": task_id, "continuation_id": continuation_id, "generation": prior + 1,
              "objective_digest": digest(objective), "binding": binding,
              "binding_digest": digest(binding), "verifier_policy": policy, "prior": checkpoint}
    created = _schedule_atomically(controller.store, task_id, continuation_id, record, objective)
    return {"scheduled": created, "generation": record["generation"], "continuation": record,
            "dispatch": "explicit operational action required; nothing is retried automatically",
            **controller.status(token, task_id)}


# --------------------------------------------------------------------------- lineage

def _compact_usage(usage: dict | None) -> dict:
    usage = usage or {}
    return {key: usage.get(key) for key in ("observed_fields", "measured_fields",
                                            "synthetic_fields", "estimated_fields",
                                            "unattributed_fields", "coverage", "publication",
                                            "mode")}


def lineage(controller, task_id: str) -> dict:
    """Current and historical generations, retrievable through the client status action."""
    store = controller.store
    scheduled, recorded = schedules(store, task_id), results(store, task_id)
    attempts = attempts_by_generation(store, task_id)
    generations = []
    for generation in sorted({1, *scheduled, *recorded}):
        record, result = scheduled.get(generation), recorded.get(generation)
        receipt = (result or {}).get("receipt") or {}
        policy = receipt.get("policy") or {}
        generations.append({
            "generation": generation, "attempt": attempts.get(generation),
            "dispatched": generation in attempts, "result_recorded": result is not None,
            **{key: (record or {}).get(key) for key in
               ("continuation_id", "objective_digest", "binding_digest")},
            **{key: (result or {}).get(key) for key in ("accepted", "endpoint", "observed")},
            "artifact_directory": str((result or {}).get("artifact_directory")
                                      or artifact_dir(controller.artifacts, task_id, generation)),
            "verifier": {**{key: policy.get(key) for key in ("kind", "script")},
                         **{key: receipt.get(key) for key in
                            ("exit_code", "verifier_intact", "policy_ok")}},
            "candidate": {"pre": receipt.get("candidate_pre"), "post": receipt.get("candidate_post"),
                          "unchanged": receipt.get("unchanged")},
            "usage": _compact_usage((result or {}).get("usage")),
            "checkpoint_of_prior_attempt": (record or {}).get("prior")})
    return {"task": task_id, "identity": "same-public-task-id",
            "current_generation": max([entry["generation"] for entry in generations
                                       if entry["result_recorded"]] or [1]),
            "scheduled_generation": max(scheduled) if scheduled else None,
            "pending_generation": pending_generation(store, task_id),
            "generations": generations}
