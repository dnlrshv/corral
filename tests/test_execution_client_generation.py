"""An in-flight continuation cannot return its predecessor as the new result."""
import pytest

from corral.execution.agent import CorralAgent


def test_wait_ignores_previous_result_after_new_claim(monkeypatch):
    agent = object.__new__(CorralAgent)
    old = {"generation": 1, "accepted": True}
    new = {"generation": 2, "accepted": False}
    # The durable new claim exists before its worker updates the prior state.
    claimed = {"state": {"status": "completed", "generation": 1}, "result": old,
               "lineage": {"current_generation": 2, "pending_generation": None}}
    # A mixed status read must also wait if its result belongs to the old attempt.
    mixed = {**claimed, "state": {"status": "completed", "generation": 2}}
    completed = {**mixed, "result": new}
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
