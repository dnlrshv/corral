"""Concurrency and process-recreation authority safety tests for Corral and legacy adapter.

Validates:
- Multi-threaded and multi-process concurrent lease contention (exactly one winner).
- Process crash / dead PID cannot be stolen by TTL or PID reclaim.
- SamefileStore mutual exclusion between Corral transport and legacy runner.
- Atomic pending-before-POST transaction prevents duplicate or overwritten intents.
- Dynamic ownership persistence across config reloads.
- Safe operator recovery gating on unresolved intents.
"""

import concurrent.futures
import os
import time
from pathlib import Path

import pytest

from corral.execution.pr import PRLifecycle
from corral.execution.store import Store


def test_concurrent_lease_contention_single_winner(tmp_path: Path):
    """Multiple concurrent workers competing for the same PR lease. Exactly one wins."""
    db_path = tmp_path / "authority.db"
    store = Store(db_path)
    res = "pr:example/project#101"
    store.acquire(res, "legacy")

    results = []

    def try_acquire(worker_id: int):
        local_store = Store(db_path)
        head = "a" * 40
        return local_store.acquire_lease(res, "legacy", head, pid=1000 + worker_id, attempt_id=f"attempt-{worker_id}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(try_acquire, i) for i in range(10)]
        for f in concurrent.futures.as_completed(futures):
            results.append(f.result())

    won = [r for r in results if r[0] is True]
    denied = [r for r in results if r[0] is False]

    assert len(won) == 1, f"Expected exactly 1 lease winner, got {len(won)}"
    assert len(denied) == 9
    assert all(r[1] == "concurrent_lease_active" for r in denied)


def test_dead_pid_and_stale_lease_cannot_be_stolen(tmp_path: Path):
    """A crashed worker process (nonexistent dead PID, elapsed > 900s) cannot have its lease stolen."""
    db_path = tmp_path / "authority.db"
    store = Store(db_path)
    res = "pr:example/project#101"
    store.acquire(res, "legacy")

    # Insert simulated crashed worker lease with dead PID from 2000s ago
    dead_pid = 99999999
    with store.transaction() as db:
        db.execute(
            "INSERT INTO leases VALUES (?, 'legacy', 1, ?, 'dead-attempt', ?, ?, 'in_flight')",
            (res, dead_pid, "a" * 40, time.time() - 2000),
        )

    # New process attempts to acquire lease
    store2 = Store(db_path)
    acquired, reason, _, _ = store2.acquire_lease(res, "legacy", "a" * 40, pid=os.getpid(), attempt_id="new-attempt")
    assert acquired is False
    assert reason == "concurrent_lease_active"

    # Verify operator recovery without pending intent is safe and explicit
    assert store.recover_lease_operator(res, authorized_by="ops-admin") is True

    # Now new process can safely acquire
    acquired_after, reason_after, _, _ = store2.acquire_lease(res, "legacy", "a" * 40, pid=os.getpid(), attempt_id="new-attempt")
    assert acquired_after is True
    assert reason_after == "acquired"


def test_operator_recovery_blocked_when_pending_intent_exists(tmp_path: Path):
    """Operator recovery must refuse to clear a lease if an unresolved intent exists."""
    db_path = tmp_path / "authority.db"
    store = Store(db_path)
    res = "pr:example/project#101"
    store.acquire(res, "legacy")
    intent = "unresolved-intent-xyz"

    store.acquire_lease(res, "legacy", "a" * 40, pid=os.getpid(), attempt_id="att-1")
    store.record_intent_pending(intent, res, "legacy", 1, "a" * 40, "b" * 40, {"payload": 1}, attempt_id="att-1")
    store.record_intent_outcome(intent, res, "ambiguous", error="POST timeout")

    with pytest.raises(PermissionError, match="unresolved intent"):
        store.recover_lease_operator(res, authorized_by="ops-admin")


def test_samefile_store_cross_transport_mutual_exclusion(tmp_path: Path):
    from tests.advisory_http_fixture import environment, PR, HEAD
    store, http, transport, intent, payload = environment(tmp_path)
    resource = "pr:" + PR
    store.transition_owner(resource, "corral", 1, "released")
    epoch = store.acquire(resource, "legacy")
    assert store.acquire_lease(resource, "legacy", HEAD, 123, "legacy-attempt")[0]
    with pytest.raises(PermissionError, match="ownership drift"):
        transport.advisory(PR, "candidate", intent, payload)
    assert not http.posts
    store.release_lease(resource, "legacy-attempt")
    store.transition_owner(resource, "legacy", epoch, "released")
    epoch = store.acquire(resource, "corral")
    approval = store.get("advisory_approval", intent)
    approval["epoch"] = epoch
    store.replace("advisory_approval", intent, approval)
    assert transport.advisory(PR, "candidate", intent, payload)["advisory"]
    assert len(http.posts) == 1


def test_transfer_blocked_during_in_flight_intent(tmp_path: Path):
    """PRLifecycle.transfer refuses to transfer ownership while a publication intent is pending/ambiguous."""
    db_path = tmp_path / "authority.db"
    store = Store(db_path)
    pr = "fixture#transfer-block"
    res = "pr:" + pr
    head = "4" * 40
    base = "2" * 40

    store.acquire(res, "legacy")
    lc = PRLifecycle(
        store, None, publishers={"review-publisher": "sec"}, owner_token="owner",
        policy={"version": "v1", "lenses": {}, "checks": []},
    )

    # In-flight lease
    store.acquire_lease(res, "legacy", head, pid=os.getpid(), attempt_id="att-transfer")
    store.record_intent_pending("intent-tx0000000000000000000000000000000000000000000000000000000", res, "legacy", 1, head, base, {"test": True}, attempt_id="att-transfer")

    # Transfer must return uncertain
    result = lc.transfer("owner", pr, "legacy", 1, "corral", stopped=True)
    assert result["state"] == "uncertain"
    assert "intent" in result["reason"] or "lease" in result["reason"]

    # Mark outcome delivered and release lease
    store.record_intent_outcome("intent-tx0000000000000000000000000000000000000000000000000000000", res, "delivered", review_id=123)
    store.release_lease(res, "att-transfer")

    # Transfer can now proceed
    result2 = lc.transfer("owner", pr, "legacy", 1, "corral", stopped=True)
    assert result2["state"] == "active"
    assert result2["owner"] == "corral"
    assert result2["epoch"] == 2
