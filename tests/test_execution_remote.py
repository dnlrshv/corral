from corral.execution.controller import Controller
from corral.execution import remote
from tests.test_execution_regressions import setup, spec  # noqa: F401


def test_lost_remote_ack_reconciles_same_attempt_no_local_worker(setup, tmp_path, monkeypatch):  # noqa: F811
    executor, repo = setup
    class Endpoint:
        lost_ack = False
        cancel_next = False
        def __init__(self, _):
            pass
        def call(self, action, **payload):
            if action == "submit":
                return {"task": executor.submit("owner", payload["request_id"], payload["spec"])}
            if action == "dispatch":
                executor.run("owner", payload["task_id"], execution_host="fixture")
                if Endpoint.cancel_next:
                    parent = executor.store.get("request", payload["task_id"])["logical_parent"]
                    controller.cancel("outer-owner", parent)
                    Endpoint.cancel_next = False
                if not Endpoint.lost_ack:
                    Endpoint.lost_ack = True
                    raise RuntimeError("fixture acknowledgement lost after execution")
                return {}
            if action == "status":
                return executor.status("owner", payload["task_id"])
            if action == "steer":
                executor.steer("owner", payload["task_id"], payload["amendment_id"], payload["amendment"])
                return {}
            if action == "snapshot":
                return executor.snapshot("owner", payload["task_id"], payload["paths"])
            if action == "cancel":
                return executor.cancel("owner", payload["task_id"])
            raise AssertionError(action)
    monkeypatch.setattr(remote, "Client", Endpoint)
    controller = Controller(tmp_path / "separate-controller", "outer-owner", {"remote-fixture": {
        "executor": {"declared": "fake-transport"}, "executor_host": "fixture"}}, default_host="remote-fixture")
    worker = "import json,os; assert json.load(open(os.environ['CORRAL_CONTEXT_PATH']))['objective']=='amended'; open('effects','a').write('one\\n'); open('result.json','w').write('{\"ok\":true}')"
    task = controller.submit("outer-owner", "remote", spec(repo, worker))
    controller.steer("outer-owner", task, "owner-amendment", {"objective": "amended"})
    first = controller.run("outer-owner", task, execution_host="remote-fixture")
    assert first["state"]["status"] == "uncertain"
    binding = controller.store.get("executor_binding", task)
    original_attempt = executor.status("owner", binding["task"])["state"]["attempt"]
    final = controller.run("outer-owner", task, execution_host="remote-fixture")
    assert final["result"]["accepted"]
    assert final["state"]["executor_state"]["attempt"] == original_attempt
    assert (repo / "effects").read_text() == "one\n"
    assert len(controller.store.records("remote_usage")) == 1
    controller.snapshot("outer-owner", task, ["result.json"])
    assert controller.store.ownership("remote-workspace:remote-fixture:" + str(repo))[2] == "released"
    second = controller.submit("outer-owner", "second", spec(repo))
    assert controller.run("outer-owner", second, execution_host="remote-fixture")["result"]["accepted"]
    cancelled = controller.submit("outer-owner", "cancel-race", spec(repo))
    Endpoint.cancel_next = True
    result = controller.run("outer-owner", cancelled, execution_host="remote-fixture")["result"]
    assert not result["accepted"] and result["structured"] == {"ok": True}

def test_split_continuation_proof_and_exact_mapping(setup, tmp_path, monkeypatch):  # noqa: F811
    executor, repo = setup

    # We create a fake Endpoint that intercepts the remote calls.
    # We will simulate delayed states and exact mapping.

    class Endpoint:
        def __init__(self, _):
            pass
        def call(self, action, **payload):
            if action == "submit":
                return {"task": executor.submit("owner", payload["request_id"], payload["spec"])}
            if action == "dispatch":
                executor.run("owner", payload["task_id"], execution_host="fixture")
                return {}
            if action == "status":
                return executor.status("owner", payload["task_id"])
            if action == "steer":
                executor.steer("owner", payload["task_id"], payload["amendment_id"], payload["amendment"])
                return {}
            if action == "snapshot":
                return executor.snapshot("owner", payload["task_id"], payload["paths"])
            if action == "cancel":
                return executor.cancel("owner", payload["task_id"])
            if action == "continue":
                return executor.continue_task("owner", payload["task_id"], payload["continuation_id"], payload["continuation"])
            raise AssertionError(action)

    monkeypatch.setattr(remote, "Client", Endpoint)
    controller = Controller(tmp_path / "separate-controller", "outer-owner", {"remote-fixture": {
        "executor": {"declared": "fake-transport"}, "executor_host": "fixture"}}, default_host="remote-fixture")

    # worker for gen1
    worker_gen1 = "import json,os; c=json.load(open(os.environ['CORRAL_CONTEXT_PATH'])); open('effects','a').write(str(c['generation'])+'\\n'); open('result.json','w').write('{\"ok\":true}')"
    task = controller.submit("outer-owner", "split-test", spec(repo, worker_gen1))

    # Run gen 1
    gen1 = controller.run("outer-owner", task, execution_host="remote-fixture")
    assert gen1["result"]["accepted"] is True
    assert gen1["lineage"]["current_generation"] == 1

    # Now continue gen 2
    amendment = {"objective": "do gen 2"}
    sch = controller.continue_task("outer-owner", task, "cont-2", amendment)
    assert sch["scheduled"] is True
    assert sch["generation"] == 2

    gen2 = controller.run("outer-owner", task, execution_host="remote-fixture")
    assert gen2["result"]["accepted"] is True
    assert gen2["lineage"]["current_generation"] == 2

    # verify workspace and effects
    assert (repo / "effects").read_text() == "1\n2\n"


def test_lost_ack_on_remote_continue(setup, tmp_path, monkeypatch):  # noqa: F811
    executor, repo = setup

    class Endpoint:
        lost_ack = False
        def __init__(self, _):
            pass
        def call(self, action, **payload):
            if action == "submit":
                return {"task": executor.submit("owner", payload["request_id"], payload["spec"])}
            if action == "dispatch":
                executor.run("owner", payload["task_id"], execution_host="fixture")
                return {}
            if action == "status":
                return executor.status("owner", payload["task_id"])
            if action == "steer":
                executor.steer("owner", payload["task_id"], payload["amendment_id"], payload["amendment"])
                return {}
            if action == "snapshot":
                return executor.snapshot("owner", payload["task_id"], payload["paths"])
            if action == "cancel":
                return executor.cancel("owner", payload["task_id"])
            if action == "continue":
                res = executor.continue_task("owner", payload["task_id"], payload["continuation_id"], payload["continuation"])
                if not Endpoint.lost_ack:
                    Endpoint.lost_ack = True
                    raise RuntimeError("lost continue ack")
                return res
            raise AssertionError(action)

    monkeypatch.setattr(remote, "Client", Endpoint)
    controller = Controller(tmp_path / "separate-controller", "outer-owner", {"remote-fixture": {
        "executor": {"declared": "fake-transport"}, "executor_host": "fixture"}}, default_host="remote-fixture")

    worker = "import json,os; open('result.json','w').write('{\"ok\":true}')"
    task = controller.submit("outer-owner", "ack-test", spec(repo, worker))
    controller.run("outer-owner", task, execution_host="remote-fixture")

    # Continue, which will fail with lost ack
    amendment = {"objective": "do gen 2"}
    import pytest
    with pytest.raises(RuntimeError, match="lost continue ack"):
        controller.continue_task("outer-owner", task, "cont-ack", amendment)

    # Retry the same continuation id
    sch2 = controller.continue_task("outer-owner", task, "cont-ack", amendment)
    assert sch2["scheduled"] is False
    assert sch2["generation"] == 2

    gen2 = controller.run("outer-owner", task, execution_host="remote-fixture")
    assert gen2["result"]["accepted"] is True
    assert gen2["lineage"]["current_generation"] == 2
