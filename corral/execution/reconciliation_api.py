"""Controller-installed collectors for evidence-bound task reconciliation."""
from __future__ import annotations

import hashlib
import json
import os

from . import continuation
from .github_advisory import GitHubAdvisoryTransport
from .github_support import read_pages, verify_remote_candidate
from .policy import _get_auth_token
from .recovery import reconcile


def _local_process(state: dict) -> dict:
    identity = {name: state.get(name) for name in ("pid", "pgid", "attempt") if state.get(name) is not None}
    if not all(name in identity for name in ("pid", "pgid", "attempt")):
        raise PermissionError("reconciliation lacks a recorded process identity")
    try:
        os.kill(int(identity["pid"]), 0)
    except ProcessLookupError:
        return _absent_group(identity)
    except PermissionError as error:
        raise PermissionError("cannot inspect recorded worker process") from error
    try:
        if os.getpgid(int(identity["pid"])) != int(identity["pgid"]):
            raise PermissionError("recorded PID was reused by a different process group")
    except ProcessLookupError:
        return _absent_group(identity)
    raise PermissionError("recorded worker process remains live")


def _absent_group(identity: dict) -> dict:
    try:
        os.killpg(int(identity["pgid"]), 0)
    except ProcessLookupError:
        return {"identity": identity, "status": "absent",
                "limits": ["detached descendants outside the recorded process group are unobservable"]}
    raise PermissionError("recorded process group remains live after leader exit")


def _recorded_binding(controller, task_id: str, state: dict) -> int:
    """Fail before remote reads unless the state still names its original invocation."""
    generation = continuation.generation_of(state)
    attempt = state.get("attempt")
    if not isinstance(attempt, str) or not attempt:
        raise PermissionError("reconciliation lacks a recorded attempt identity")
    claim = controller.store.get("claim", continuation.claim_key(task_id, generation))
    invocation = controller.store.get("invocation", attempt)
    if (not isinstance(claim, dict) or claim.get("attempt") != attempt
            or claim.get("generation") != generation
            or not isinstance(invocation, dict) or invocation.get("task") != task_id
            or invocation.get("generation") != generation):
        raise PermissionError("reconciliation claim or invocation does not bind the current attempt")
    return generation


def _github_readback(reader: dict, task_id: str, state: dict) -> dict:
    required = ("task", "attempt", "generation", "epoch", "repo", "pr", "head", "base", "actor")
    if (reader.get("kind") != "github-pr-readback-v1"
            or any(not isinstance(reader.get(key), str) or not reader[key] for key in required)
            or not reader["pr"].isdigit()):
        raise PermissionError("GitHub delivery reader declaration is invalid")
    binding = {"task": task_id, "attempt": str(state.get("attempt") or ""),
               "generation": str(state.get("generation") or 1), "epoch": str(state.get("epoch") or "")}
    if any(reader[key] != value for key, value in binding.items()):
        raise PermissionError("GitHub delivery reader is not bound to the current task attempt")
    token_name = str(reader.get("token_env") or "GITHUB_TOKEN")
    token = os.environ.get(token_name) or _get_auth_token()
    if not token:
        raise PermissionError("GitHub delivery reader credential is unavailable")
    transport = GitHubAdvisoryTransport(bridge_token=token, bridge_actor=reader["actor"], store=None,
                                        allow_network=True, authorized_bridge_actors=frozenset({reader["actor"]}))
    if transport.validated_bridge_actor != reader["actor"]:
        raise PermissionError("authenticated GitHub actor does not match the reader declaration")
    verify_remote_candidate(transport._request, reader["repo"], int(reader["pr"]), reader["head"], reader["base"])
    reviews = read_pages(transport._request, f"/repos/{reader['repo']}/pulls/{reader['pr']}/reviews")
    matching = [item["id"] for item in reviews if item.get("commit_id") == reader["head"]
                and isinstance(item.get("user"), dict) and item["user"].get("login") == reader["actor"]]
    if matching:
        raise PermissionError("authenticated GitHub readback found a matching remote review")
    return {"searched": True, "status": "absent", "reader": "github-pr-readback-v1",
            "authenticated": True, "task": task_id, "attempt": state.get("attempt"),
            "generation": state.get("generation", 1), "epoch": state.get("epoch"),
            "repo": reader["repo"], "pr": int(reader["pr"]), "head": reader["head"],
            "base": reader["base"], "actor": transport.validated_bridge_actor,
            "review_count": len(reviews), "matching_review_ids": matching}


def _artifact_observation(controller, task_id: str, state: dict) -> dict:
    generation = int(state.get("generation") or 1)
    directory = continuation.artifact_dir(controller.artifacts, task_id, generation)
    paths = {"context": directory / "context.json", "adapter": directory / "adapter-result.json"}
    if not all(path.is_file() for path in paths.values()):
        raise PermissionError("reconciliation requires preserved context and adapter result artifacts")
    try:
        context, adapter = (json.loads(path.read_text()) for path in paths.values())
    except (OSError, ValueError) as error:
        raise PermissionError("reconciliation artifacts are not valid JSON") from error
    if (context.get("task") != task_id or context.get("attempt") != state.get("attempt")
            or context.get("generation") != generation or adapter.get("task") != task_id
            or adapter.get("attempt") != state.get("attempt")
            or adapter.get("status") != "completed"):
        raise PermissionError("reconciliation artifacts do not bind the interrupted attempt")
    return {"status": "preserved", "directory": str(directory), "attempt": state.get("attempt"),
            "generation": generation, "context_sha256": hashlib.sha256(paths["context"].read_bytes()).hexdigest(),
            "adapter_sha256": hashlib.sha256(paths["adapter"].read_bytes()).hexdigest()}


def controller_only_observation(controller, task_id: str, state: dict) -> dict:
    """Collect local facts without accepting mutable caller-provided settlement claims."""
    generation = _recorded_binding(controller, task_id, state)
    spec = controller.context(task_id)
    host = controller.hosts.get(spec["host"], {})
    reader = host.get("reconciliation_delivery_reader")
    if reader != "controller-records-only" and not isinstance(reader, dict):
        raise PermissionError("host lacks a trusted delivery-readback collector")
    with controller.store.transaction() as db:
        intents = db.execute("SELECT intent,status FROM publication_intents WHERE attempt_id=?",
                             (str(state.get("attempt") or ""),)).fetchall()
    if intents:
        raise PermissionError("attempt has publication intents; authenticated remote reconciliation is required")
    delivery = (_github_readback(reader, task_id, state) if isinstance(reader, dict) else
                 {"searched": True, "status": "absent", "reader": "controller-records-only",
                 "task": task_id, "attempt": state.get("attempt"), "generation": generation})
    return {"process": _local_process(state),
            "artifact": _artifact_observation(controller, task_id, state),
            "delivery": delivery}


def reconcile_local(controller, token: str, task_id: str) -> dict:
    """Use the installed collector; CLI callers cannot smuggle a settlement observation."""
    return reconcile(controller, token, task_id,
                     lambda current_task, state: controller_only_observation(controller, current_task, state))
