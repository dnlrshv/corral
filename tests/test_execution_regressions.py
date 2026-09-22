import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from corral.execution.controller import Controller
from corral.execution.demo import fixture_profiles
from corral.execution.process import Process
from corral.execution.profiles import resolve
from corral.execution.recovery import reconcile
from corral.execution.reconciliation_api import reconcile_local
from corral.execution.usage import Spool, summarize


@pytest.fixture
def setup(tmp_path):
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=f@example.invalid",
                    "commit", "--allow-empty", "-qm", "fixture"], cwd=repo, check=True)
    controller = Controller(tmp_path / "state", "owner", {"fixture": {"routes": ["fixture"],
        "harnesses": ["synthetic"], "cpu": 4, "memory_mb": 1024,
        "reconciliation_delivery_reader": "controller-records-only"}}, default_host="fixture", profiles=fixture_profiles())
    return controller, repo


def spec(repo, worker=None):
    return {"repo": "test", "workspace": str(repo), "candidate_paths": ["result.json"],
            "command": [sys.executable, "-c", worker or "open('result.json','w').write('{\"ok\":true}')"],
            "verify": [sys.executable, "-c", "import json; assert json.load(open('result.json')) == {'ok':True}"]}


def bind_interrupted_attempt(controller, task, state):
    generation, attempt = state.get("generation", 1), state["attempt"]
    controller.store.put_once("claim", task if generation <= 1 else f"{task}:g{generation}",
                              {"attempt": attempt, "generation": generation})
    controller.store.put_once("invocation", attempt, {"task": task, "generation": generation})
    directory = controller.artifacts / task if generation <= 1 else controller.artifacts / f"{task}-g{generation}"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "context.json").write_text(json.dumps({"task": task, "attempt": attempt, "generation": generation}))
    (directory / "adapter-result.json").write_text(json.dumps({"task": task, "attempt": attempt,
                                                                 "status": "completed"}))


def test_cancel_before_spawn_pause_resume_and_bad_profile(setup):
    controller, repo = setup
    task = controller.submit("owner", "cancel", spec(repo))
    controller.cancel("owner", task)
    assert controller.run("owner", task, execution_host="fixture")["dispatch"] == "cancelled-before-start"
    assert not (repo / "result.json").exists()
    task = controller.submit("owner", "pause", spec(repo))
    controller.steer("owner", task, "1", {"pause_dispatch": True})
    with pytest.raises(PermissionError):
        controller.run("owner", task, execution_host="fixture")
    controller.steer("owner", task, "2", {"pause_dispatch": False, "stop_monitoring": True})
    assert controller.run("owner", task, execution_host="fixture")["result"]["accepted"]
    selected = resolve(fixture_profiles(), role="implementation", routes=["fixture"], default="strong-low")
    selected["profile"]["route"] = "unauthorized"
    with pytest.raises(PermissionError):
        controller.submit("owner", "bad-profile", {**spec(repo), "selection": selected})


def test_two_concurrent_clients_start_one_worker(setup):
    controller, repo = setup
    worker = "import time; open('effects','a').write('one\n'); time.sleep(.2); open('result.json','w').write('{\"ok\":true}')"
    # Literal newline in the worker code must be escaped, preserving a real effect.
    worker = worker.replace("one\n", "one\\n")
    task = controller.submit("owner", "race", spec(repo, worker))
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(lambda _: controller.run("owner", task, execution_host="fixture"), [1, 2]))
    assert (repo / "effects").read_text() == "one\n"
    assert controller.status("owner", task)["result"]["accepted"]


def test_native_event_identity_attempt_isolation_and_malformed(setup):
    controller, repo = setup
    (repo / "usage.json").write_text('[{"id":"stale","counters":{"input":999}}]')
    worker = """import json,os,pathlib
pathlib.Path('result.json').write_text('{"ok":true}')
pathlib.Path(os.environ['CORRAL_USAGE_PATH']).write_text(json.dumps([{'id':'local-id','scope':'session','epoch':0,'sequence':1,'mode':'delta','counters':{'input':7}}]))
"""
    for request in ("a", "b"):
        task = controller.submit("owner", request, spec(repo, worker))
        result = controller.run("owner", task, execution_host="fixture")["result"]
        assert result["usage"]["observed_fields"] == {"input": 7}
    assert len(controller.store.records("usage")) == 2
    bad = "import os; open('result.json','w').write('{\"ok\":true}'); open(os.environ['CORRAL_USAGE_PATH'],'w').write('broken')"
    task = controller.submit("owner", "bad-usage", spec(repo, bad))
    assert controller.run("owner", task, execution_host="fixture")["result"]["accepted"]


def test_spool_sequence_collisions_and_sparse_values(tmp_path):
    event = {"invocation": "a", "id": "one", "scope": "session", "epoch": 0,
             "sequence": 1, "mode": "delta", "counters": {"input": 3, "output": None}}
    report = summarize([event, {**event, "id": "alias"}])
    assert report["observed_fields"] == {"input": 3}
    assert report["unknown"]
    assert summarize([{**event, "sequence": 10**20}])["unknown"]
    assert summarize([{**event, "sequence": "broken"}])["unknown"]
    spool = Spool(tmp_path / "spool")
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(spool.append, [event, {**event, "counters": {"input": 4}}]))
    assert len(spool.store.records("conflict")) == 1


def test_parent_exit_term_resistant_child_and_sentinel(tmp_path):
    sentinel = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    code = """import os,pathlib,signal,time
pid=os.fork()
if pid:
    while not pathlib.Path('ready').exists(): time.sleep(.01)
    os._exit(0)
signal.signal(signal.SIGTERM,signal.SIG_IGN)
pathlib.Path('ready').write_text(str(os.getpid()))
time.sleep(30)
"""
    try:
        with (tmp_path / "out").open("wb") as out:
            process = Process([sys.executable, "-c", code], tmp_path, out, out)
            assert process.wait() == 0
            assert process.running_group()
            receipt = process.cancel()
        child = (tmp_path / "ready").read_text()
        status = subprocess.run(["ps", "-p", child, "-o", "stat="], capture_output=True, text=True).stdout.strip()
        assert not status or status.startswith("Z")
        assert receipt["detached_descendants"] == "unknown"
        assert sentinel.poll() is None
        assert (tmp_path / "ready").exists()
    finally:
        sentinel.terminate()
        sentinel.wait()


def test_simulated_crash_reconciliation_no_duplicate_effect(setup):
    controller, repo = setup
    task = controller.submit("owner", "crash", spec(repo))
    epoch = controller.store.acquire("workspace:" + str(repo.resolve()), task)
    state = {"status": "uncertain", "attempt": "saved", "epoch": epoch, "generation": 1}
    bind_interrupted_attempt(controller, task, state)
    controller.store.replace("state", task, state)
    (repo / "result.json").write_text('{"ok":true}')
    (repo / "effects").write_text("one")
    assert reconcile(controller, "owner", task, lambda *_: {"execution_stopped": False})["status"] == "uncertain"
    result = reconcile(controller, "owner", task, lambda *_: {
        "execution_stopped": True, "effects_reconciled": True, "proof": "declared simulated boundary"})
    assert result["accepted"] and not result["receipt"]["worker_restarted"]
    assert (repo / "effects").read_text() == "one"


def test_cancelled_recovery_cannot_accept(setup):
    controller, repo = setup
    task = controller.submit("owner", "cancelled-recovery", spec(repo))
    epoch = controller.store.acquire("workspace:" + str(repo.resolve()), task)
    controller.store.transition_owner("workspace:" + str(repo.resolve()), task, epoch, "uncertain")
    state = {"status": "uncertain", "attempt": "saved", "epoch": epoch, "generation": 1}
    bind_interrupted_attempt(controller, task, state)
    (repo / "result.json").write_text('{"ok":true}')
    controller.cancel("owner", task)
    state.update(pid=99999999, pgid=99999999)
    controller.store.replace("state", task, state)
    result = reconcile_local(controller, "owner", task)
    assert not result["accepted"] and result["terminal_status"] == "cancelled"
    assert result["receipt"]["verifier_executed"] is False
    assert controller.store.get("state", task)["status"] == "cancelled"
    assert controller.store.ownership("workspace:" + str(repo.resolve()))[2] == "released"
    assert controller.store.get("allocation", task) is None or not controller.store.get("allocation", task)["active"]
    assert reconcile_local(controller, "owner", task) == result


def test_cancelled_missing_git_metadata_preserves_history_without_verifier(setup, tmp_path):
    controller, repo = setup
    task = controller.submit("owner", "cancelled-export", spec(repo))
    epoch = controller.store.acquire("workspace:" + str(repo.resolve()), task)
    controller.store.transition_owner("workspace:" + str(repo.resolve()), task, epoch, "uncertain")
    state = {"status": "uncertain", "attempt": "post-adapter", "epoch": epoch, "generation": 1}
    bind_interrupted_attempt(controller, task, state)
    controller.store.put_once("usage", "post-adapter", {"input": "unknown"})
    # Match #3518's exported snapshot contract: no Git metadata exists at reconciliation.
    import shutil
    shutil.rmtree(repo / ".git")
    controller.cancel("owner", task)
    state.update(pid=99999998, pgid=99999998)
    controller.store.replace("state", task, state)
    result = reconcile_local(controller, "owner", task)
    assert result["terminal_status"] == "cancelled"
    assert controller.store.get("invocation", "post-adapter")["task"] == task
    assert controller.store.get("usage", "post-adapter") == {"input": "unknown"}
    assert controller.store.records("reconciliation")[task + ":g1"]["event"] == "cancelled-reconciled"


def test_cancelled_reconciliation_requires_concrete_effect_receipts(setup):
    controller, repo = setup
    task = controller.submit("owner", "unsettled", spec(repo))
    epoch = controller.store.acquire("workspace:" + str(repo.resolve()), task)
    controller.store.transition_owner("workspace:" + str(repo.resolve()), task, epoch, "uncertain")
    state = {"status": "uncertain", "attempt": "saved", "epoch": epoch, "generation": 1}
    bind_interrupted_attempt(controller, task, state)
    controller.store.replace("state", task, state)
    controller.cancel("owner", task)
    with pytest.raises(PermissionError, match="recorded process identity"):
        reconcile_local(controller, "owner", task)
    assert controller.store.ownership("workspace:" + str(repo.resolve()))[2] == "uncertain"
