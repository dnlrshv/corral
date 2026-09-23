from corral.execution.controller import Controller
from corral.execution import remote
from tests.test_execution_regressions import spec, setup

__all__ = ['setup']
import pytest

# Helper to create a remote executor mock
class MockTransport:
    def __init__(self, executor):
        self.executor = executor
        self.fail_on_continue_before = False
        self.fail_on_continue_after = False
        self.delay_gen1 = False
        self.snapshot_override = None

    def call(self, action, **payload):
        if action == "submit":
            return {"task": self.executor.submit("owner", payload["request_id"], payload["spec"])}
        if action == "dispatch":
            self.executor.run("owner", payload["task_id"], execution_host="fixture")
            return {}
        if action == "status":
            if self.snapshot_override:
                return self.snapshot_override()
            return self.executor.status("owner", payload["task_id"])
        if action == "steer":
            self.executor.steer("owner", payload["task_id"], payload["amendment_id"], payload["amendment"])
            return {}
        if action == "snapshot":
            return self.executor.snapshot("owner", payload["task_id"], payload["paths"])
        if action == "fetch-artifact":
            return self.executor.fetch_artifact("owner", payload["task_id"], payload["path"], payload.get("generation"))
        if action == "cancel":
            return self.executor.cancel("owner", payload["task_id"])
        if action == "continue":
            if self.fail_on_continue_before:
                self.fail_on_continue_before = False
                raise RuntimeError("transport failed before reaching executor")
            res = self.executor.continue_task("owner", payload["task_id"], payload["continuation_id"], payload["continuation"])
            if self.fail_on_continue_after:
                self.fail_on_continue_after = False
                raise RuntimeError("transport failed after reaching executor")
            return res
        raise AssertionError(action)

def make_worker():
    return "import json,os; c=json.load(open(os.environ['CORRAL_CONTEXT_PATH'])); open('effects','a').write(str(c['generation'])+'\\n'); open('result.json','w').write('{\"ok\":true}')"

def test_a_durable_intent_transport_failure_before_scheduling(setup, tmp_path, monkeypatch):
    executor, repo = setup
    transport = MockTransport(executor)
    monkeypatch.setattr(remote, "Client", lambda _: transport)
    controller = Controller(tmp_path / "controller", "outer-owner", {"remote-fixture": {
        "executor": {"declared": "fake-transport"}, "executor_host": "fixture"}}, default_host="remote-fixture")

    task = controller.submit("outer-owner", "task-a", spec(repo, make_worker()))
    controller.run("outer-owner", task, execution_host="remote-fixture")

    transport.fail_on_continue_before = True
    amendment = {"objective": "do gen 2"}
    with pytest.raises(RuntimeError, match="transport failed before"):
        controller.continue_task("outer-owner", task, "cont-a", amendment)

    # Retry same ID. Executor should get it exactly once.
    sch = controller.continue_task("outer-owner", task, "cont-a", amendment)
    assert sch["scheduled"] is False  # wait, it was scheduled locally, but not remotely. Wait, local says "False" because it was scheduled locally, but remote wasn't? Actually, `already is not None` returns scheduled=False!

    controller.run("outer-owner", task, execution_host="remote-fixture")
    assert executor.status("owner", controller.store.get("executor_binding", task)["task"])["lineage"]["current_generation"] == 2
    assert (repo / "effects").read_text() == "1\n2\n"


def test_b_delayed_old_result_not_satisfy_gen2(setup, tmp_path, monkeypatch):
    executor, repo = setup
    transport = MockTransport(executor)
    monkeypatch.setattr(remote, "Client", lambda _: transport)
    controller = Controller(tmp_path / "controller", "outer-owner", {"remote-fixture": {
        "executor": {"declared": "fake-transport"}, "executor_host": "fixture"}}, default_host="remote-fixture")

    task = controller.submit("outer-owner", "task-b", spec(repo, make_worker()))
    gen1 = controller.run("outer-owner", task, execution_host="remote-fixture")

    controller.continue_task("outer-owner", task, "cont-b", {"objective": "do gen 2"})

    # We will simulate the `run_remote` for gen 2 seeing a stale gen 1 result first
    # and wait until gen2 result actually appears.

    real_status_calls = []

    def delayed_status():
        real = executor.status("owner", controller.store.get("executor_binding", task)["task"])
        if len(real_status_calls) == 0:
            # First status call: return gen 1 result as if gen 2 is dispatching but result is still gen 1.
            # But we actually want to simulate it returned gen 1 even though gen 2 is running
            # wait, `real` might already have gen2 if we let it run natively.
            # Let's override it completely to simulate the race.
            fake = dict(real)
            fake["results"] = {"1": gen1["result"]}
            fake["result"] = gen1["result"]
            real_status_calls.append(1)
            return fake
        return real

    transport.snapshot_override = delayed_status
    gen2 = controller.run("outer-owner", task, execution_host="remote-fixture")

    assert gen2["result"]["accepted"] is True
    assert gen2["lineage"]["current_generation"] == 2

    results = controller.status("outer-owner", task)["results"]
    assert results["1"]["receipt"]["attempt"] == gen1["result"]["receipt"]["attempt"]
    assert results["2"]["receipt"]["attempt"] == gen2["result"]["receipt"]["attempt"]
    assert results["1"]["receipt"]["attempt"] != results["2"]["receipt"]["attempt"]
    assert len(controller.store.records("remote_usage")) == 2


def test_c_reconnect_during_gen2_and_repeated_completed_run(setup, tmp_path, monkeypatch):
    executor, repo = setup
    transport = MockTransport(executor)
    monkeypatch.setattr(remote, "Client", lambda _: transport)
    controller = Controller(tmp_path / "controller", "outer-owner", {"remote-fixture": {
        "executor": {"declared": "fake-transport"}, "executor_host": "fixture"}}, default_host="remote-fixture")

    task = controller.submit("outer-owner", "task-c", spec(repo, make_worker()))
    controller.run("outer-owner", task, execution_host="remote-fixture")

    controller.continue_task("outer-owner", task, "cont-c", {"objective": "do gen 2"})

    # We simulate reconnect controller instance while gen2 is running on the executor.
    # We do this by throwing a ConnectionError during the first 'status' poll.
    # This causes run() to return early with an 'uncertain' status due to transport error.
    executor_task = controller.store.get("executor_binding", task)["task"]

    original_status = transport.call

    def status_override(action, **payload):
        if action == "status" and not getattr(status_override, "failed", False):
            # First status call: fail to simulate crash
            status_override.failed = True
            raise ConnectionError("simulated crash")
        return original_status(action, **payload)

    transport.call = status_override
    gen2_first_try = controller.run("outer-owner", task, execution_host="remote-fixture")
    assert gen2_first_try["state"]["status"] == "uncertain"
    assert gen2_first_try["state"]["transport_error"] == "ConnectionError"

    # Now it's "uncertain". Reconnect!
    reconnected = Controller(tmp_path / "controller", "outer-owner", {"remote-fixture": {
        "executor": {"declared": "fake-transport"}, "executor_host": "fixture"}}, default_host="remote-fixture")

    # Restore transport dispatch so the next run completes it, or we just complete it manually.
    transport.call = original_status

    # Run repeated completed on the reconnected instance
    gen2_repeated = reconnected.run("outer-owner", task, execution_host="remote-fixture")
    assert gen2_repeated["state"]["status"] == "completed"

    invocations = executor.store.records("invocation")
    task_invocations = [v for k, v in invocations.items() if v["task"] == executor_task]
    assert len(task_invocations) == 2  # exactly 2 worker effects/invocations


def test_d_executor_auto_advances_preserves_lineage(setup, tmp_path, monkeypatch):
    executor, repo = setup
    transport = MockTransport(executor)
    monkeypatch.setattr(remote, "Client", lambda _: transport)
    controller = Controller(tmp_path / "controller", "outer-owner", {"remote-fixture": {
        "executor": {"declared": "fake-transport"}, "executor_host": "fixture"}}, default_host="remote-fixture")

    task = controller.submit("outer-owner", "task-d", spec(repo, make_worker()))

    # Steer first
    controller.steer("outer-owner", task, "amend-d", {"objective": "amended"})

    original_status = executor.status

    def fake_status(owner, ex_task):
        stat = original_status(owner, ex_task)
        if stat["lineage"]["current_generation"] == 1 and stat["state"]["status"] == "completed" and not getattr(fake_status, "called", False):
            fake_status.called = True
            executor.continue_task("owner", ex_task, "auto-amend-d", {"objective": "amended"})
            executor.run("owner", ex_task, execution_host="fixture")
            return original_status(owner, ex_task)
        return stat

    executor.status = fake_status

    controller.run("outer-owner", task, execution_host="remote-fixture")

    lineage = controller.status("outer-owner", task)["lineage"]
    assert lineage["current_generation"] == 2
    assert lineage["pending_generation"] is None
    results = controller.status("outer-owner", task)["results"]
    assert "1" in results
    assert "2" in results

def test_e_fetch_artifact_remote_forwarding(setup, tmp_path, monkeypatch):
    """Ensure fetch-artifact bridges distinct controller and executor stores."""
    executor, repo = setup

    # Actually let's just dispatch a fast command and wait for it on the executor
    # But since we have a split topology, we can just use the controller -> remote bridge
    transport = MockTransport(executor)
    monkeypatch.setattr(remote, "Client", lambda _: transport)

    controller = Controller(tmp_path / "controller", "outer-owner", {"remote-fixture": {
        "executor": {"declared": "fake-transport"}, "executor_host": "fixture"}}, default_host="remote-fixture")

    (repo / "out.txt").write_text("initial")

    import sys
    task_spec = spec(repo, "import json,os,base64; c=json.load(open(os.environ['CORRAL_CONTEXT_PATH'])); open('result.json','w').write(json.dumps({'ok':True,'obj':c.get('objective', 'initial')}))")
    task_spec["candidate_paths"] = ["result.json"]
    task_spec["verify"] = [sys.executable, "-c", "import json; assert json.load(open('result.json')) == {'ok':True,'obj':'initial'}"]
    task = controller.submit("outer-owner", "task-remote", task_spec)

    monkeypatch.setattr(remote, "Client", lambda _: MockTransport(executor))

    controller.run("outer-owner", task, execution_host="remote-fixture")

    import time
    time.sleep(0.5)

    # 2. Fetch latest (gen 1)
    result = controller.fetch_artifact("outer-owner", task, "result.json")
    import base64
    data = base64.b64decode(result["data"]).decode("utf-8")
    assert "initial" in data

    # Verify the local controller actually does not have the artifact file natively,
    # proving it was fetched over the bridge.
    from corral.execution.continuation import artifact_dir
    local_art = artifact_dir(controller.artifacts, task, 1)
    assert not (local_art / "candidate_manifest.json").exists()

    # 3. Schedule generation 2
    controller.continue_task("outer-owner", task, "cont-1", {
        "objective": "generation 2 objective",
        "verify": [sys.executable, "-c", "import json; assert json.load(open('result.json')) == {'ok':True,'obj':'generation 2 objective'}"]
    })
    controller.run("outer-owner", task, execution_host="remote-fixture")
    time.sleep(0.5)

    # 4. Fetch gen 1 explicitly
    result1 = controller.fetch_artifact("outer-owner", task, "result.json", generation=1)
    data1 = base64.b64decode(result1["data"]).decode("utf-8")
    assert "initial" in data1

    # 5. Fetch gen 2 (default latest)
    result2 = controller.fetch_artifact("outer-owner", task, "result.json")
    data2 = base64.b64decode(result2["data"]).decode("utf-8")
    assert "generation 2 objective" in data2

    local_art2 = artifact_dir(controller.artifacts, task, 2)
    assert not (local_art2 / "candidate_manifest.json").exists()
