"""An in-flight continuation cannot return its predecessor as the new result."""
import pytest

from corral.execution.agent import CorralAgent
from corral.execution import continuation
from tests.test_execution_regressions import setup, spec

__all__ = ["setup"]


def test_wait_ignores_previous_result_after_new_claim(setup, monkeypatch):
    controller, repo = setup
    task = controller.submit("owner", "generation-race", spec(repo))
    controller.run("owner", task, execution_host="fixture")
    controller.continue_task("owner", task, "continue", {"objective": "amended"})
    controller.store.put_once("claim", continuation.claim_key(task, 2),
                              {"attempt": "new-attempt", "generation": 2})
    claimed = controller.status("owner", task)
    # Reproduce the actual durable window: claim is new, state/result remain old,
    # and current_generation intentionally denotes the latest recorded result.
    assert claimed["lineage"]["current_generation"] == 1
    assert claimed["lineage"]["scheduled_generation"] == 2
    assert claimed["lineage"]["pending_generation"] is None
    assert claimed["result"]["accepted"] is True
    controller.store.replace("state", task, {"status": "completed", "generation": 2})
    mixed = controller.status("owner", task)
    continuation.record_result(controller.store, task, 2, {"generation": 2, "accepted": False})
    completed = controller.status("owner", task)
    agent = object.__new__(CorralAgent)
    responses = iter([claimed, mixed, completed])
    monkeypatch.setattr(agent, "status", lambda task: next(responses))
    monkeypatch.setattr(agent, "dispatch", lambda task: (_ for _ in ()).throw(
        AssertionError("an already claimed generation must not be redispatched")))
    assert agent.wait("logical-task", poll_interval=0) == completed


@pytest.mark.parametrize("status", ["cancelled", "reconciled"])
def test_wait_returns_settled_recovery_outcome(monkeypatch, status):
    agent = object.__new__(CorralAgent)
    settled = {"state": {"status": status, "generation": 1},
               "result": {"generation": 1, "accepted": False},
               "lineage": {"current_generation": 1, "pending_generation": None}}
    monkeypatch.setattr(agent, "status", lambda task: settled)
    assert agent.wait("logical-task", poll_interval=0) == settled
