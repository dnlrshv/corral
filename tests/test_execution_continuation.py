"""Post-terminal continuation of one aggregate task identity (deterministic dispatch path).

Everything here drives the real controller dispatch path: a real worker process, a real
controller-owned external verifier subprocess and the real durable state store. No production
code is monkeypatched. The native-harness variant lives in
``test_execution_continuation_native.py``; authority and the client JSON protocol live in
``test_execution_continuation_authority.py``.
"""
from __future__ import annotations

import json
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from corral.execution.store import digest

from . import continuation_support as cs


@pytest.fixture
def stage1(tmp_path):
    """An accepted stage 1 attempt: the only kind of state a continuation may checkpoint."""
    env = cs.deterministic_env(tmp_path)
    task, run = cs.run_stage1(env)
    assert run["result"]["accepted"] is True and run["result"]["generation"] == 1
    return env, task, run


def _continue(env, task, continuation_id="stage2-days", **overrides):
    """Author the stage 2 verifier in the same host root, then select it for a new generation."""
    cs.dispatch_stage2(env)
    return env["controller"].continue_task("owner", task, continuation_id,
                                          cs.stage2_continuation(env, **overrides))


def _files(directory: Path) -> dict:
    return {str(path.relative_to(directory)): path.read_bytes()
            for path in sorted(directory.rglob("*")) if path.is_file()}


# --------------------------------------------------------------------------- the amendment

def test_terminal_task_continues_into_exactly_one_new_verified_generation(stage1):
    env, task, first = stage1
    controller, workspace = env["controller"], env["workspace"]
    assert (workspace / "duration.py").read_text() == cs.STAGE1_SOURCE

    scheduled = _continue(env, task)
    assert scheduled["scheduled"] is True and scheduled["generation"] == 2
    assert scheduled["lineage"]["pending_generation"] == 2
    assert scheduled["lineage"]["current_generation"] == 1
    # Scheduling is not execution. The accepted candidate, its receipt and the worker count are
    # untouched until an explicit operational action dispatches the single new invocation.
    assert (workspace / "duration.py").read_text() == cs.STAGE1_SOURCE
    assert set(controller.store.records("invocation")) == {first["state"]["attempt"]}

    second = controller.run("owner", task, execution_host=env["host_id"])
    assert second["task"] == task == first["task"]              # same public aggregate identity
    assert second["result"]["accepted"] is True
    assert second["result"]["generation"] == 2
    assert (workspace / "duration.py").read_text() == cs.STAGE2_SOURCE      # real amended code
    assert second["result"]["structured"] == cs.STAGE2_STRUCTURED

    receipt = second["result"]["receipt"]
    assert receipt["policy"]["kind"] == "external"
    assert Path(receipt["policy"]["script"]).name == "verify_stage2.py"     # different script ...
    assert receipt["policy"]["verifier_root"] == str(env["roots"])          # ... same host root
    assert receipt["verifier_intact"] is True and receipt["policy_ok"] is True
    assert receipt["exit_code"] == 0 and receipt["unchanged"] is True
    assert receipt["candidate_pre"] != first["result"]["receipt"]["candidate_pre"]

    record = controller.store.records("continuation")[task + ":stage2-days"]
    # The policy bound at scheduling time is the policy that actually executed.
    assert record["verifier_policy"]["digest"] == digest(receipt["policy"])
    assert record["prior"]["generation"] == 1 and record["prior"]["accepted"] is True
    assert record["prior"]["receipt_digest"] == digest(first["result"]["receipt"])
    assert record["prior"]["artifact_directory"] == first["result"]["artifact_directory"]

    assert second["state"]["attempt"] != first["state"]["attempt"]
    assert second["state"]["generation"] == 2
    assert second["result"]["artifact_directory"] != first["result"]["artifact_directory"]
    assert len(controller.store.records("claim")) == 2          # one worker claim per generation
    assert len(controller.store.records("invocation")) == 2
    assert controller.store.records("invocation")[second["state"]["attempt"]]["generation"] == 2
    owner, _epoch, status = controller.store.ownership("workspace:" + str(workspace.resolve()))
    assert (owner, status) == (task, "released")


def test_prior_receipts_artifacts_and_results_stay_immutable_and_addressable(stage1):
    env, task, first = stage1
    controller = env["controller"]
    stage1_dir = Path(first["result"]["artifact_directory"])
    before, stage1_result = _files(stage1_dir), json.loads(json.dumps(first["result"]))
    stage1_receipt = json.loads((stage1_dir / "receipt.json").read_text())

    _continue(env, task)
    second = controller.run("owner", task, execution_host=env["host_id"])
    assert second["result"]["accepted"] is True

    # 1. Every stage 1 artifact is byte-identical, including the receipt and the usage record.
    assert _files(stage1_dir) == before
    assert json.loads((stage1_dir / "receipt.json").read_text()) == stage1_receipt
    # 2. The generation 1 result record was never overwritten, and both stay retrievable.
    assert controller.store.get("result", task) == stage1_result
    status = controller.status("owner", task)
    assert status["results"]["1"] == stage1_result
    assert status["results"]["2"] == second["result"]
    assert status["result"] == second["result"]                 # current = newest generation
    # 3. The historical receipt still reports accepted even though its own verifier now rejects
    #    the amended candidate: a receipt is a record of that attempt, never re-derived live.
    assert subprocess.run(stage1_receipt["command"], cwd=str(env["workspace"]),
                          capture_output=True).returncode != 0
    assert status["results"]["1"]["accepted"] is True
    # 4. Lineage keeps both generations addressable through the client, with their own digests.
    generations = {entry["generation"]: entry for entry in status["lineage"]["generations"]}
    assert set(generations) == {1, 2}
    assert generations[1]["artifact_directory"] == str(stage1_dir)
    assert generations[2]["artifact_directory"] == second["result"]["artifact_directory"]
    assert Path(generations[1]["artifact_directory"], "receipt.json").is_file()
    assert Path(generations[2]["artifact_directory"], "receipt.json").is_file()
    assert (generations[1]["accepted"], generations[2]["accepted"]) == (True, True)
    assert generations[1]["continuation_id"] is None
    assert generations[2]["continuation_id"] == "stage2-days"
    assert generations[1]["candidate"]["pre"] != generations[2]["candidate"]["pre"]
    assert generations[2]["candidate"]["unchanged"] is True
    assert generations[2]["verifier"]["script"].endswith("verify_stage2.py")
    assert status["lineage"]["current_generation"] == 2
    assert status["lineage"]["pending_generation"] is None


# --------------------------------------------------------------------------- deduplication

def test_repeated_continuation_id_deduplicates_and_a_conflicting_one_is_refused(stage1):
    env, task, first = stage1
    controller = env["controller"]
    cs.dispatch_stage2(env)
    payload = cs.stage2_continuation(env)

    scheduled = controller.continue_task("owner", task, "stage2-days", payload)
    repeat = controller.continue_task("owner", task, "stage2-days", json.loads(json.dumps(payload)))
    assert repeat["scheduled"] is False and repeat["generation"] == scheduled["generation"] == 2
    assert len(controller.store.records("continuation")) == 1
    assert len(controller.store.records("amendment")) == 1
    # The same id must never carry a different payload, and a second id must never queue a
    # second invocation behind an undispatched one.
    with pytest.raises(ValueError, match="conflicting continuation identity"):
        controller.continue_task("owner", task, "stage2-days",
                                 cs.stage2_continuation(env, objective=cs.STAGE2_OBJECTIVE + "!"))
    with pytest.raises(PermissionError, match="not yet dispatched"):
        controller.continue_task("owner", task, "stage2-other", cs.stage2_continuation(env))
    assert len(controller.store.records("continuation")) == 1
    assert controller.status("owner", task)["lineage"]["pending_generation"] == 2

    second = controller.run("owner", task, execution_host=env["host_id"])
    assert second["result"]["generation"] == 2 and second["result"]["accepted"] is True
    assert len(controller.store.records("invocation")) == 2
    # A late retry of the same continuation id still deduplicates after the generation ran.
    late = controller.continue_task("owner", task, "stage2-days", payload)
    assert late["scheduled"] is False and late["generation"] == 2
    assert len(controller.store.records("invocation")) == 2
    assert controller.status("owner", task)["result"] == second["result"]


def test_restart_and_repeated_dispatch_run_exactly_one_new_worker(stage1):
    env, task, first = stage1
    stage1_dir = Path(first["result"]["artifact_directory"])
    controller = env["controller"]
    _continue(env, task)
    # Controller restart with the continuation scheduled and the ack lost: durable state alone
    # decides, and two racing restarted clients still start only the one scheduled invocation.
    assert cs.restart(env).status("owner", task)["lineage"]["pending_generation"] == 2
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(lambda _: cs.restart(env).run("owner", task, execution_host=env["host_id"]),
                      [1, 2]))
    status = controller.status("owner", task)
    assert status["result"]["generation"] == 2 and status["result"]["accepted"] is True
    assert len(controller.store.records("invocation")) == 2
    assert len(controller.store.records("claim")) == 2
    assert (env["workspace"] / "duration.py").read_text() == cs.STAGE2_SOURCE

    # Repeated dispatch after completion, from the original and from restarted controllers.
    for runner in (controller, cs.restart(env), cs.restart(env)):
        assert runner.run("owner", task, execution_host=env["host_id"])["result"] == status["result"]
    assert len(controller.store.records("invocation")) == 2
    generation_dir = Path(status["result"]["artifact_directory"])
    # The new generation ran in its own scratch directory: it carries its own checkpoint of the
    # terminal attempt, while the accepted stage 1 directory gained nothing at all.
    checkpoint = json.loads((generation_dir / "continuation-checkpoint.json").read_text())
    assert checkpoint["generation"] == 1 and checkpoint["accepted"] is True
    assert checkpoint["artifact_directory"] == str(stage1_dir)
    assert not (Path(first["result"]["artifact_directory"]) / "continuation-checkpoint.json").exists()
    assert json.loads((generation_dir / "context.json").read_text())["generation"] == 2
    assert controller.store.get("result", task) == first["result"]      # generation 1 untouched


# --------------------------------------------------------------------------- fencing

def test_continuation_refuses_a_task_that_was_never_dispatched(tmp_path):
    env = cs.deterministic_env(tmp_path)
    controller = env["controller"]
    cs.dispatch_stage2(env)
    task = controller.submit("owner", "never-dispatched", cs.deterministic_spec(env))
    with pytest.raises(PermissionError, match="no dispatched attempt"):
        controller.continue_task("owner", task, "stage2-days", cs.stage2_continuation(env))
    with pytest.raises(PermissionError, match="authenticated client authority required"):
        controller.continue_task("not-owner", task, "stage2-days", cs.stage2_continuation(env))
    assert controller.store.records("continuation") == {}


def test_continuation_refuses_a_genuinely_running_worker(tmp_path):
    env = cs.deterministic_env(tmp_path)
    controller = env["controller"]
    cs.dispatch_stage2(env)
    task = controller.submit("owner", "running", cs.deterministic_spec(env, sleep_seconds=2.5))
    finished = threading.Event()

    def dispatch():
        try:
            controller.run("owner", task, execution_host=env["host_id"])
        finally:
            finished.set()

    thread = threading.Thread(target=dispatch)
    thread.start()
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            state = controller.store.get("state", task)
            if state and state["status"] == "running":
                break
            time.sleep(0.02)
        else:
            raise AssertionError("the worker never reached the running state")
        with pytest.raises(PermissionError, match="cannot continue an execution whose status"):
            controller.continue_task("owner", task, "stage2-days", cs.stage2_continuation(env))
        assert controller.store.records("continuation") == {}
    finally:
        assert finished.wait(60) and not thread.join(30)
    assert controller.status("owner", task)["result"]["accepted"] is True


def test_continuation_refuses_unresolved_ownership_and_never_clears_the_lock(stage1):
    env, task, first = stage1
    controller = env["controller"]
    resource = "workspace:" + str(env["workspace"].resolve())
    owner, epoch, status = controller.store.ownership(resource)
    assert (owner, status) == (task, "released")
    # The state an interrupted attempt really leaves behind: ownership unresolved.
    controller.store.transition_owner(resource, owner, epoch, "uncertain")
    with pytest.raises(PermissionError, match="workspace ownership is 'uncertain'"):
        _continue(env, task)
    assert controller.store.ownership(resource)[2] == "uncertain"   # the lock was NOT cleared
    assert controller.store.records("continuation") == {}
    assert controller.status("owner", task)["result"] == first["result"]

    # Only the reconciled outcome reopens continuation, and then exactly one generation.
    controller.store.transition_owner(resource, owner, epoch, "released")
    assert _continue(env, task)["generation"] == 2
    assert controller.run("owner", task, execution_host=env["host_id"])["result"]["accepted"]


def test_continuation_refuses_a_recorded_cancellation(stage1):
    env, task, first = stage1
    controller = env["controller"]
    cs.dispatch_stage2(env)
    assert controller.cancel("owner", task)["ownership_released"] is False
    with pytest.raises(PermissionError, match="recorded cancellation"):
        controller.continue_task("owner", task, "stage2-days", cs.stage2_continuation(env))
    assert controller.store.records("continuation") == {}
    # The accepted stage 1 result stays the current result; nothing was amended or re-run.
    assert controller.status("owner", task)["result"] == first["result"]
    assert len(controller.store.records("invocation")) == 1
