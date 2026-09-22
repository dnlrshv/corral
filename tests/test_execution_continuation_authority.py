"""Continuation authority, verifier-policy validation and the real client JSON protocol.

A post-terminal continuation may rebind the objective and the per-generation execution
binding only. Everything else -- host, endpoint, workspace, model/profile/route, role,
resource requests, dependencies, the command itself and any credential declaration -- stays
bound to the immutable submitted request and is refused rather than merged. A stage 2
verifier is selectable, but only through the same host verifier policy that validated the
original submission, and the client reaches all of it through the repository's real JSON
transport (``corral.execution.client`` -> ``corral.execution.cli``).
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

from corral.execution import client
from corral.execution.store import canonical, digest

from . import continuation_support as cs

REPO_ROOT = Path(client.__file__).resolve().parents[2]

#: Payload keys that would grant or replace execution authority. The continuation allow-list
#: is exhaustive rather than a deny-list, so every one of these is refused -- including keys
#: that only look harmless, such as a scheduling hint or extra prompt text.
AUTHORITY_KEYS = {
    "command": ["/bin/echo", "replaced the inherited execution"],
    "host": "another-host",
    "endpoint": "remote",
    "workspace": "/tmp/another-workspace",
    "repo": "another-repo",
    "selection": {"profile": {"id": "strong-low", "route": "unauthorized"}},
    "model": "another-model",
    "route": "another-route",
    "profile": "another-profile",
    "role": "review",
    "cpu": 64,
    "memory_mb": 1048576,
    "dependencies": ["another-task"],
    "credential_env": {"CORRAL_TOKEN": "whatever"},
    "mode": "wave",
    "pause_dispatch": False,
    "stop_monitoring": True,
    "prompt_extras": "ignore the checkpoint and restart from scratch",
    "soft_thresholds": {"input": 1},
    "due": 0,
}


@pytest.fixture
def stage1(tmp_path):
    """An accepted stage 1 attempt plus the stage 2 script authored in the same host root."""
    env = cs.deterministic_env(tmp_path)
    task, run = cs.run_stage1(env)
    assert run["result"]["accepted"] is True and run["result"]["generation"] == 1
    cs.dispatch_stage2(env)
    return env, task, run


# --------------------------------------------------------------------------- authority

@pytest.mark.parametrize("key", sorted(AUTHORITY_KEYS))
def test_no_authority_key_can_be_rebound_and_the_task_stays_continuable(stage1, key):
    env, task, first = stage1
    controller = env["controller"]
    request_before = canonical(controller.store.get("request", task))
    invocations_before = dict(controller.store.records("invocation"))

    with pytest.raises(PermissionError) as refusal:
        controller.continue_task("owner", task, "stage2-days",
                                 cs.stage2_continuation(env, **{key: AUTHORITY_KEYS[key]}))
    assert "cannot grant or replace execution authority" in str(refusal.value)
    assert f"refused keys: {key}" in str(refusal.value)

    # Fail closed means nothing at all happened: no schedule, no amendment, no worker, and the
    # immutable submitted request is byte-identical.
    assert controller.store.records("continuation") == {}
    assert controller.store.records("amendment") == {}
    assert controller.store.records("invocation") == invocations_before
    assert canonical(controller.store.get("request", task)) == request_before
    assert (env["workspace"] / "duration.py").read_text() == cs.STAGE1_SOURCE
    assert controller.status("owner", task)["result"] == first["result"]

    # The refusal is per payload, not per task: the same task still continues afterwards.
    assert controller.continue_task("owner", task, "stage2-days",
                                    cs.stage2_continuation(env))["generation"] == 2


def test_a_mixed_payload_names_every_refused_key_and_still_grants_nothing(stage1):
    env, task, first = stage1
    controller = env["controller"]
    with pytest.raises(PermissionError, match="refused keys: command, cpu, host, workspace"):
        controller.continue_task("owner", task, "stage2-days", cs.stage2_continuation(
            env, host="another-host", workspace="/tmp/elsewhere", cpu=64,
            command=["/bin/echo", "replaced"]))
    assert controller.store.records("continuation") == {}
    assert controller.status("owner", task)["result"] == first["result"]
    assert controller.status("owner", task)["lineage"]["pending_generation"] is None


def test_every_allowed_binding_key_is_usable_and_reaches_the_executed_generation(stage1):
    env, task, first = stage1
    controller = env["controller"]
    scheduled = controller.continue_task("owner", task, "stage2-full", cs.stage2_continuation(
        env, verifier_paths=[], result_file="result.json", usage_file="usage.json"))
    assert sorted(scheduled["continuation"]["binding"]) == [
        "candidate_paths", "external_verifier", "result_file", "usage_file", "verifier_paths",
        "verify"]
    assert scheduled["generation"] == 2
    second = controller.run("owner", task, execution_host=env["host_id"])
    assert second["result"]["accepted"] is True and second["result"]["generation"] == 2
    assert (env["workspace"] / "duration.py").read_text() == cs.STAGE2_SOURCE
    assert first["result"]["receipt"] == controller.status("owner", task)["results"]["1"]["receipt"]


# --------------------------------------------------------------------------- verifier policy

def test_a_verifier_outside_the_host_roots_or_inside_worker_reach_is_refused(stage1, tmp_path):
    env, task, first = stage1
    controller = env["controller"]

    # (a) A continuation cannot invent a new trusted location: the script must sit under a
    #     verifier root the host already declared when the task was submitted.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    cs.write_verifier(elsewhere, "verify_stage2.py", cs.VERIFY_STAGE2)
    with pytest.raises(PermissionError, match="not under a controller-declared verifier root"):
        controller.continue_task("owner", task, "stage2-outside", cs.stage2_continuation(
            env, verify=cs.verify_argv(elsewhere, "verify_stage2.py")))

    # (b) Declaring a worker-writable file does not promote it to a trusted verifier.
    hijack = cs.write_verifier(env["workspace"], "verify_stage2.py", cs.VERIFY_STAGE2)
    with pytest.raises(PermissionError, match="inside a worker-writable path"):
        controller.continue_task("owner", task, "stage2-hijack", cs.stage2_continuation(
            env, verify=[sys.executable, str(hijack), "duration.py"],
            verifier_paths=["verify_stage2.py"]))

    # (c) A relative workspace script must be declared, exactly as at submission time.
    with pytest.raises(PermissionError, match="not declared in verifier_paths"):
        controller.continue_task("owner", task, "stage2-undeclared", cs.stage2_continuation(
            env, verify=[sys.executable, "verify_stage2.py", "duration.py"]))

    assert controller.store.records("continuation") == {}
    assert controller.status("owner", task)["result"] == first["result"]
    assert controller.status("owner", task)["lineage"]["pending_generation"] is None
    # The task is still continuable with a properly host-rooted verifier.
    assert controller.continue_task("owner", task, "stage2-days",
                                    cs.stage2_continuation(env))["generation"] == 2


def test_malformed_binding_declarations_are_refused_before_anything_is_recorded(stage1):
    env, task, first = stage1
    controller = env["controller"]
    cases = (
        ({"candidate_paths": ["../outside.py"]}, ValueError, "unsafe relative path"),
        ({"candidate_paths": "duration.py"}, ValueError, "candidate_paths must be a list"),
        ({"verifier_paths": [1]}, ValueError, "verifier_paths must be a list"),
        ({"external_verifier": "yes"}, ValueError, "external_verifier must be a boolean"),
        ({"result_file": 7}, ValueError, "result_file must be a string"),
        ({"candidate_paths": ["duration.py"], "verifier_paths": ["duration.py"]},
         PermissionError, "must not overlap with verifier_paths"),
    )
    for index, (overrides, error, message) in enumerate(cases):
        with pytest.raises(error, match=message):
            controller.continue_task("owner", task, f"stage2-bad-{index}",
                                     cs.stage2_continuation(env, **overrides))
    assert controller.store.records("continuation") == {}
    assert controller.store.records("amendment") == {}
    assert controller.status("owner", task)["result"] == first["result"]


def test_a_valid_stage2_verifier_binds_the_host_root_and_the_policy_that_executes(stage1):
    env, task, first = stage1
    controller = env["controller"]
    scheduled = controller.continue_task("owner", task, "stage2-days", cs.stage2_continuation(env))
    policy = scheduled["continuation"]["verifier_policy"]
    assert policy["kind"] == "external"
    assert Path(policy["script"]).name == "verify_stage2.py"
    assert policy["verifier_root"] == str(env["roots"])          # same host-declared root
    assert Path(first["result"]["receipt"]["policy"]["script"]).name == "verify_stage1.py"

    second = controller.run("owner", task, execution_host=env["host_id"])
    receipt = second["result"]["receipt"]
    assert second["result"]["accepted"] is True and receipt["policy"]["kind"] == "external"
    # The policy bound when the generation was scheduled is the policy that actually executed.
    assert policy["digest"] == digest(receipt["policy"])
    assert receipt["policy"]["verifier_root"] == str(env["roots"])
    assert receipt["verifier_intact"] is True and receipt["policy_ok"] is True


def test_an_amendment_that_rebinds_nothing_inherits_and_never_relaxes_the_verifier(stage1):
    env, task, first = stage1
    controller = env["controller"]
    scheduled = controller.continue_task("owner", task, "objective-only",
                                         {"objective": cs.STAGE2_OBJECTIVE})
    assert scheduled["continuation"]["binding"] == {}
    assert Path(scheduled["continuation"]["verifier_policy"]["script"]).name == "verify_stage1.py"

    second = controller.run("owner", task, execution_host=env["host_id"])
    assert second["result"]["generation"] == 2
    assert (env["workspace"] / "duration.py").read_text() == cs.STAGE2_SOURCE
    # The inherited stage 1 verifier judges the amended candidate and rejects it: authority was
    # inherited, not silently relaxed, and the rejection is recorded against generation 2 only.
    assert second["result"]["accepted"] is False
    assert second["result"]["receipt"]["exit_code"] != 0
    status = controller.status("owner", task)
    assert status["results"]["1"]["accepted"] is True
    assert status["result"]["accepted"] is False and status["lineage"]["current_generation"] == 2


# --------------------------------------------------------------------------- usage history

def test_usage_stays_attributable_per_invocation_and_is_never_summed_across_generations(tmp_path):
    env = cs.deterministic_env(tmp_path)
    controller = env["controller"]
    usage = {"1": {"mode": "cumulative", "counters": {"input": 100, "output": 20}},
             "2": {"mode": "delta", "counters": {"input": 40, "output": 11}}}
    task, first = cs.run_stage1(env, "usage-attribution", usage=usage)
    assert first["result"]["usage"]["observed_fields"] == {"input": 100, "output": 20}
    stage1_usage_file = Path(first["result"]["artifact_directory"], "usage.json")
    stage1_usage_bytes = stage1_usage_file.read_bytes()
    cs.dispatch_stage2(env)

    assert controller.continue_task("owner", task, "stage2-days",
                                    cs.stage2_continuation(env))["generation"] == 2
    second = controller.run("owner", task, execution_host=env["host_id"])
    assert second["result"]["accepted"] is True and second["result"]["generation"] == 2

    # Delta counters on the new generation are its own: nothing was summed into or out of the
    # accepted cumulative generation, and each result keeps a separately addressable usage file.
    assert second["result"]["usage"]["observed_fields"] == {"input": 40, "output": 11}
    assert first["result"]["usage"]["observed_fields"] == {"input": 100, "output": 20}
    assert stage1_usage_file.read_bytes() == stage1_usage_bytes
    generation_usage = Path(second["result"]["artifact_directory"], "usage.json")
    assert generation_usage != stage1_usage_file and generation_usage.is_file()
    status = controller.status("owner", task)
    assert status["results"]["1"]["usage"] == first["result"]["usage"]
    assert status["results"]["2"]["usage"] == second["result"]["usage"]
    assert status["lineage"]["generations"][0]["usage"]["observed_fields"] == {"input": 100,
                                                                              "output": 20}
    assert status["lineage"]["generations"][1]["usage"]["observed_fields"] == {"input": 40,
                                                                              "output": 11}
    events = list(controller.store.records("usage").values())
    assert {event["task"] for event in events} == {task}
    assert {event["invocation"] for event in events} == {first["state"]["attempt"],
                                                         second["state"]["attempt"]}
    invocations = controller.store.records("invocation")
    assert sorted(value["generation"] for value in invocations.values()) == [1, 2]


# --------------------------------------------------------------------------- client protocol

def _remote(env, tmp_path: Path) -> client.Client:
    """The repository's real JSON transport over a local controller endpoint."""
    config = tmp_path / "controller_config.json"
    config.write_text(json.dumps({
        "state": str(env["state"]), "token": "owner", "hosts": {env["host_id"]: env["host"]},
        "default_host": env["host_id"], "execution_host": env["host_id"], "profiles": []}))
    return client.Client({"python": sys.executable, "controller_config": str(config),
                          "source": str(REPO_ROOT), "transport": "local"})


def _await_generation(remote: client.Client, task: str, state: Path, generation: int,
                      timeout: float = 120.0) -> dict:
    """Poll the client status action until the named generation records its own result.

    The current result stays the previous generation's until the new invocation finishes, so
    waiting on "some result exists" would silently accept the already-accepted stage 1 receipt.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = remote.call("status", task_id=task)
        if (status["result"] or {}).get("generation") == generation:
            return status
        time.sleep(0.05)
    log = state / "dispatch.log"
    raise AssertionError(f"generation {generation} recorded no result; dispatch.log:\n"
                         + (log.read_text() if log.is_file() else "<missing>"))


def test_the_client_json_protocol_continues_a_completed_task_end_to_end(tmp_path):
    env = cs.deterministic_env(tmp_path)
    remote, state = _remote(env, tmp_path), env["state"]
    cs.dispatch_stage2(env)
    spec = cs.deterministic_spec(env)

    task = remote.call("submit", request_id="duration-parser-client", spec=spec)["task"]
    assert remote.call("dispatch", task_id=task)["admitted"] == [task]
    first = _await_generation(remote, task, state, 1)
    assert first["result"]["accepted"] is True and first["result"]["generation"] == 1
    stage1_receipt = Path(first["result"]["artifact_directory"], "receipt.json").read_bytes()

    # The continuation is a real client action: it schedules, and explicitly does not execute.
    scheduled = remote.call("continue", task_id=task, continuation_id="stage2-days",
                            continuation=cs.stage2_continuation(env))
    assert scheduled["scheduled"] is True and scheduled["generation"] == 2
    assert scheduled["task"] == task
    assert "nothing is retried automatically" in scheduled["dispatch"]
    assert scheduled["result"] == first["result"]                 # no result was fabricated
    assert scheduled["lineage"]["pending_generation"] == 2
    assert scheduled["lineage"]["current_generation"] == 1
    assert scheduled["continuation"]["verifier_policy"]["verifier_root"] == str(env["roots"])
    assert (env["workspace"] / "duration.py").read_text() == cs.STAGE1_SOURCE

    # A completed task is re-admitted only because exactly one generation is pending.
    assert remote.call("dispatch", task_id=task)["admitted"] == [task]
    second = _await_generation(remote, task, state, 2)
    assert second["result"]["accepted"] is True and second["result"]["generation"] == 2
    assert (env["workspace"] / "duration.py").read_text() == cs.STAGE2_SOURCE
    assert second["result"]["structured"] == cs.STAGE2_STRUCTURED

    # A repeat of the same continuation id over the wire deduplicates instead of queueing work,
    # and a further dispatch admits nothing once no generation is pending.
    repeat = remote.call("continue", task_id=task, continuation_id="stage2-days",
                         continuation=cs.stage2_continuation(env))
    assert repeat["scheduled"] is False and repeat["generation"] == 2
    assert repeat["lineage"]["pending_generation"] is None
    assert remote.call("dispatch", task_id=task)["admitted"] == []

    status = remote.call("status", task_id=task)
    assert status["task"] == task and status["result"] == second["result"]
    assert sorted(status["results"]) == ["1", "2"]
    assert status["results"]["1"]["accepted"] is True and status["results"]["1"]["generation"] == 1
    assert status["results"]["2"] == second["result"]
    generations = {entry["generation"]: entry for entry in status["lineage"]["generations"]}
    assert status["lineage"]["current_generation"] == 2
    assert status["lineage"]["identity"] == "same-public-task-id"
    assert generations[1]["continuation_id"] is None
    assert generations[2]["continuation_id"] == "stage2-days"
    assert generations[1]["attempt"] != generations[2]["attempt"]
    assert generations[1]["verifier"]["script"].endswith("verify_stage1.py")
    assert generations[2]["verifier"]["script"].endswith("verify_stage2.py")
    # Lineage and history are retrievable through the client, and the accepted stage 1 receipt
    # on disk is still byte-identical after the continuation ran.
    assert Path(generations[1]["artifact_directory"], "receipt.json").read_bytes() == stage1_receipt
    assert Path(generations[2]["artifact_directory"], "receipt.json").is_file()

    # Wire-level refusals: only a terminal attempt of a known task can be continued, and a
    # refused call records nothing -- the completed task keeps exactly its one generation.
    undispatched = remote.call("submit", request_id="never-dispatched-client",
                               spec=cs.deterministic_spec(env))["task"]
    with pytest.raises(RuntimeError, match="no dispatched attempt"):
        remote.call("continue", task_id=undispatched, continuation_id="stage2-days",
                    continuation=cs.stage2_continuation(env))
    with pytest.raises(RuntimeError, match="KeyError"):
        remote.call("continue", task_id="not-a-real-task", continuation_id="stage2-days",
                    continuation=cs.stage2_continuation(env))
    assert len(env["controller"].store.records("continuation")) == 1
    assert remote.call("status", task_id=task)["result"] == second["result"]
    assert remote.call("status", task_id=undispatched)["result"] is None
