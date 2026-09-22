"""Continuation of a completed native task: fresh session, checkpoint, own usage attribution.

This is the same offline full path as ``test_execution_end_to_end_native.py``: a real
executable synthetic harness named by a host-owned route, the production adapter, a real
ephemeral Seatbelt worker boundary and a real controller-owned external verifier. The harness
is explicitly synthetic, so every model/usage artifact here is fixture evidence, never a live
provider response.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from corral.execution import containment, native

from . import continuation_support as cs
from . import native_support as ns

requires_sandbox = pytest.mark.skipif(containment.sandbox_exec() is None,
                                      reason="real worker containment requires macOS sandbox-exec")


def stage1_ops() -> list[str]:
    return [
        f"write duration.py {ns.b64(cs.STAGE1_SOURCE)}",
        f"result {ns.b64(json.dumps(cs.STAGE1_STRUCTURED))}",
        "narrative Implemented h/m parsing and re-read the contract before finishing.",
        f"usage {json.dumps({'input_tokens': 150, 'output_tokens': 20, 'total_tokens': 170})}",
        "usage-mode cumulative",
    ]


def stage2_ops() -> list[str]:
    # Delta counters on purpose: cumulative/delta semantics are per invocation, and a new
    # generation must never be summed into the accepted prior one.
    return [
        f"write duration.py {ns.b64(cs.STAGE2_SOURCE)}",
        f"result {ns.b64(json.dumps(cs.STAGE2_STRUCTURED))}",
        "narrative Added days with strict descending order and duplicate rejection.",
        f"usage {json.dumps({'input_tokens': 40, 'output_tokens': 11, 'total_tokens': 51})}",
        "usage-mode delta",
    ]


def native_env(tmp_path: Path) -> dict:
    env = ns.native_env(tmp_path)
    cs.write_verifier(env["verifiers"], "verify_stage1.py", cs.VERIFY_STAGE1)
    return env


def stage1_spec(env: dict) -> dict:
    return ns.native_spec(env, ops=stage1_ops(), objective=cs.STAGE1_OBJECTIVE,
                          candidate_paths=("duration.py",),
                          verify=cs.verify_argv(env["verifiers"], "verify_stage1.py"))


def stage2_payload(env: dict) -> dict:
    cs.write_verifier(env["verifiers"], "verify_stage2.py", cs.VERIFY_STAGE2)
    return {"objective": cs.STAGE2_OBJECTIVE + "\n" + ns.write_ops(*stage2_ops()),
            "verify": cs.verify_argv(env["verifiers"], "verify_stage2.py"),
            "external_verifier": True, "candidate_paths": ["duration.py"]}


@requires_sandbox
def test_native_continuation_is_a_checkpointed_fresh_session_with_its_own_usage(tmp_path):
    env = native_env(tmp_path)
    controller, workspace, state = env["controller"], env["workspace"], env["state"]
    task = controller.submit("owner", "duration-parser-native", stage1_spec(env))
    first = controller.run("owner", task, execution_host=ns.FAKE_HOST)
    assert first["result"]["accepted"] is True
    assert (workspace / "duration.py").read_text() == cs.STAGE1_SOURCE
    stage1_dir = Path(first["result"]["artifact_directory"])
    stage1_attempts, stage1_prompt = ns.attempts(first), (state / "scratch" / task / "prompt.md")
    stage1_prompt_text = stage1_prompt.read_text()

    scheduled = controller.continue_task("owner", task, "stage2-days", stage2_payload(env))
    assert scheduled["scheduled"] is True and scheduled["generation"] == 2
    second = controller.run("owner", task, execution_host=ns.FAKE_HOST)
    result = second["result"]

    # 1. Same public task identity, real amended code, judged by the new controller-owned script.
    assert second["task"] == task and result["accepted"] is True and result["generation"] == 2
    assert (workspace / "duration.py").read_text() == cs.STAGE2_SOURCE
    assert result["structured"] == cs.STAGE2_STRUCTURED
    receipt = result["receipt"]
    assert receipt["policy"]["kind"] == "external"
    assert Path(receipt["policy"]["script"]).name == "verify_stage2.py"
    assert receipt["policy"]["verifier_root"] == str(env["verifiers"])
    assert receipt["verifier_intact"] is True and receipt["unchanged"] is True
    assert receipt["native"]["route"] == ns.FAKE_ROUTE
    assert result["identity_mismatch"] == []
    assert result["observed"]["model"] == ns.FAKE_MODEL

    # 2. A fresh native session/scratch with a compact checkpoint, not an in-session retune.
    stage2_scratch = state / "scratch" / f"{task}-g2"
    stage2_prompt_text = (stage2_scratch / "prompt.md").read_text()
    assert stage2_scratch != state / "scratch" / task
    assert "Continuation of the SAME aggregate task identity" in stage2_prompt_text
    assert "generation 2 follows terminal generation 1" in stage2_prompt_text
    assert "not an in-session retune" in stage2_prompt_text
    assert "Prior amendments for this same task identity" in stage2_prompt_text
    assert "stage2-days" in stage2_prompt_text          # same aggregate task history
    assert cs.STAGE2_OBJECTIVE.splitlines()[0] in stage2_prompt_text
    # The accepted stage 1 session is untouched: no checkpoint leaked into its prompt.
    assert stage1_prompt.read_text() == stage1_prompt_text
    assert "Continuation of the SAME aggregate task identity" not in stage1_prompt_text
    checkpoint = json.loads((Path(result["artifact_directory"]) / "continuation-checkpoint.json")
                            .read_text())
    assert checkpoint["generation"] == 1 and checkpoint["accepted"] is True
    assert checkpoint["artifact_directory"] == str(stage1_dir)
    assert checkpoint["usage"]["synthetic_fields"]["input_tokens"] == 150

    # 3. Per-invocation usage: each generation reports its own counters, kept synthetic, and
    #    the delta-mode generation is not summed into the cumulative one.
    assert result["usage"]["synthetic_fields"] == {"input_tokens": 40, "output_tokens": 11,
                                                   "total_tokens": 51}
    assert first["result"]["usage"]["synthetic_fields"] == {"input_tokens": 150,
                                                            "output_tokens": 20,
                                                            "total_tokens": 170}
    for usage in (first["result"]["usage"], result["usage"]):
        assert usage["measured_fields"] == {} and usage["estimated_fields"] == {}
        assert usage["origin_breakdown"]["synthetic-fixture"] == 1
        assert usage["account_total"] is None and usage["combined_tokens"] is None
    events = list(controller.store.records("usage").values())
    assert {event["task"] for event in events} == {task}
    assert {event["invocation"] for event in events} == {first["state"]["attempt"],
                                                         second["state"]["attempt"]}

    # 4. Containment was demonstrated again for the new generation, in its own task directory.
    proven = receipt["containment"]
    assert proven["passed"] is True and proven["not_contained"] and proven["isolation_claim"]
    assert all(check["errno"] == "EPERM"
               for check in proven["checks"] if check["expected"] == "denied")
    boundary = json.loads(
        (Path(result["artifact_directory"]) / "boundary.json").read_text())["boundary"]
    # The boundary was rebuilt around this generation's own scratch identity, so its denied
    # sibling sentinel is the generation-2 one, while generation 1's sentinels are untouched.
    sibling = state / "scratch" / f"{task}-g2-sibling{native.SENTINEL_NAME}"
    assert sibling.is_file() and str(sibling.resolve()) in boundary["sentinels"]
    assert not any(f"{task}-sibling" in item for item in boundary["sentinels"])
    assert (state / "scratch" / f"{task}-sibling{native.SENTINEL_NAME}").is_file()
    assert (stage1_dir / native.SENTINEL_NAME).is_file()

    # 5. One attempt per generation, and the stage 1 artifacts/receipts are unchanged.
    assert len(ns.attempts(second)) == 1 and ns.attempts(first) == stage1_attempts
    assert ns.adapter_result(second)["status"] == "completed"
    assert ns.adapter_result(second)["structured"] == cs.STAGE2_STRUCTURED
    assert len(controller.store.records("claim")) == 2
    assert len(controller.store.records("invocation")) == 2
    status = controller.status("owner", task)
    assert status["results"]["1"] == first["result"] and status["result"] == result
    assert status["lineage"]["current_generation"] == 2
    assert [entry["attempt"] for entry in status["lineage"]["generations"]] == [
        first["state"]["attempt"], second["state"]["attempt"]]
    owner, _epoch, ownership = controller.store.ownership("workspace:" + str(workspace.resolve()))
    assert (owner, ownership) == (task, "released")


@requires_sandbox
def test_native_continuation_repeats_and_restarts_without_a_second_worker(tmp_path):
    env = native_env(tmp_path)
    controller, workspace, state = env["controller"], env["workspace"], env["state"]
    task = controller.submit("owner", "duration-parser-native-dedupe", stage1_spec(env))
    first = controller.run("owner", task, execution_host=ns.FAKE_HOST)
    assert first["result"]["accepted"] is True
    payload = stage2_payload(env)

    assert controller.continue_task("owner", task, "stage2-days", payload)["scheduled"] is True
    assert controller.continue_task("owner", task, "stage2-days",
                                    json.loads(json.dumps(payload)))["scheduled"] is False
    assert len(controller.store.records("continuation")) == 1
    assert controller.store.records("invocation").keys() == {first["state"]["attempt"]}

    # Restart before dispatch: the durable schedule alone authorizes exactly one new invocation.
    restarted = ns.Controller(state, "owner", {ns.FAKE_HOST: env["host"]},
                              default_host=ns.FAKE_HOST, profiles=[env["profile"]])
    assert restarted.status("owner", task)["lineage"]["pending_generation"] == 2
    second = restarted.run("owner", task, execution_host=ns.FAKE_HOST)
    assert second["result"]["accepted"] is True and second["result"]["generation"] == 2
    assert (workspace / "duration.py").read_text() == cs.STAGE2_SOURCE
    generation_dir = Path(second["result"]["artifact_directory"])
    assert len(ns.attempts(second)) == 1
    assert len(controller.store.records("invocation")) == 2

    # Repeat dispatch after completion, from the original and a restarted controller: no worker.
    for runner in (controller, restarted):
        assert runner.run("owner", task, execution_host=ns.FAKE_HOST)["result"] == second["result"]
    assert len(controller.store.records("invocation")) == 2
    assert len(controller.store.records("claim")) == 2
    assert len(ns.attempts(second)) == 1
    # The trusted adapter logged exactly one attempt for generation 2 -- every repeat above
    # started no worker at all -- and generation 1 keeps its own separate attempt log.
    assert [line["attempt"] for line in ns.attempts(second)] == [second["state"]["attempt"]]
    assert [line["attempt"] for line in ns.attempts(first)] == [first["state"]["attempt"]]
    assert (generation_dir / "attempts.jsonl").stat().st_size > 0
