"""A POST that never reached GitHub is settled by authenticated readback, not by deadlock.

Every scenario drives the real advisory transport, store and publication records against
the fake GitHub HTTP fixture; no network is contacted.
"""

import os
import subprocess
import sys

import pytest

from tests.advisory_http_fixture import ACTOR, HEAD, PR, environment
from corral.execution.publication_store import record_absence

RESOURCE = "pr:" + PR


def _lose_post(http):
    """An HTTP client whose review POST fails before GitHub receives it."""
    def client(method, path, **kwargs):
        if method == "POST":
            raise ConnectionRefusedError("connection refused before the request was sent")
        return http(method, path, **kwargs)
    return client


def _intent_status(store, intent):
    with store.transaction() as db:
        return db.execute("SELECT status FROM publication_intents WHERE intent=?",
                          (intent,)).fetchone()[0]


def _lease(store):
    with store.transaction() as db:
        return db.execute("SELECT attempt_id,status,holder_pid FROM leases WHERE resource=?",
                          (RESOURCE,)).fetchone()


def _dead_pid():
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


def test_lost_post_is_proven_absent_then_the_retry_publishes_exactly_once(tmp_path):
    store, http, transport, intent, payload = environment(tmp_path)
    transport.http_client = _lose_post(http)
    with pytest.raises(ConnectionRefusedError):
        transport.advisory(PR, "candidate", intent, payload)
    assert _intent_status(store, intent) == "ambiguous" and not http.posts
    assert store.acquire_lease(RESOURCE, "corral", HEAD, os.getpid(), "other")[0] is False
    with pytest.raises(PermissionError, match="unresolved intent"):
        store.recover_lease_operator(RESOURCE, authorized_by="operator")

    transport.http_client = http
    waiting = transport.reconcile(PR, intent, payload)
    assert waiting["status"] == "ambiguous_pending"
    assert waiting["absence_unproven"] == "quiet-period"
    assert _intent_status(store, intent) == "ambiguous"

    transport.absence_quiet_seconds = 0
    settled = transport.reconcile(PR, intent, payload)
    assert settled["status"] == "absent" and settled["reconciled"] is True
    assert settled["delivered"] is False and settled["absence"]["consistent_reads"] == 3
    assert _intent_status(store, intent) == "absent" and _lease(store) is None
    assert transport.reconcile(PR, intent, payload)["absence"] == settled["absence"]
    assert store.acquire_lease(RESOURCE, "corral", HEAD, os.getpid(), "probe")[:2] == (
        True, "acquired")
    store.release_lease(RESOURCE, "probe")

    receipt = transport.advisory(PR, "candidate", intent, payload)
    assert receipt["review_id"] == 901 and transport.has_advisory(intent)
    assert transport.advisory(PR, "candidate", intent, payload) == receipt
    assert transport.reconcile(PR, intent, payload) == receipt
    assert len(http.posts) == 1


def test_lost_acknowledgement_is_delivered_and_never_absent(tmp_path):
    store, http, transport, intent, payload = environment(tmp_path)
    transport.absence_quiet_seconds = 0
    with pytest.raises(ConnectionError):
        transport.advisory(PR, "candidate", intent, payload, lose_ack=True)
    receipt = transport.reconcile(PR, intent, payload)
    assert receipt["review_id"] == 901 and _intent_status(store, intent) == "delivered"
    assert store.records("advisory_absence") == {}
    assert len(http.posts) == 1


def test_review_landing_between_readbacks_is_delivered(tmp_path):
    store, http, transport, intent, payload = environment(tmp_path)
    transport.http_client = _lose_post(http)
    with pytest.raises(ConnectionRefusedError):
        transport.advisory(PR, "candidate", intent, payload)
    listings = []

    def late_landing(method, path, **kwargs):
        if method == "GET" and path.split("?")[0].endswith("/reviews"):
            listings.append(path)
            if len(listings) == 2:  # the lost request lands after the first readback
                http.reviews.append({"id": 902, "state": "COMMENTED", "user": {"login": ACTOR},
                                     "commit_id": HEAD, "body": payload["body"]})
                http.comments[902] = []
        return http(method, path, **kwargs)

    transport.http_client = late_landing
    transport.absence_quiet_seconds = 0
    receipt = transport.reconcile(PR, intent, payload)
    assert receipt["review_id"] == 902 and _intent_status(store, intent) == "delivered"
    assert store.records("advisory_absence") == {}


def test_moved_candidate_keeps_the_attempt_unresolved(tmp_path):
    store, http, transport, intent, payload = environment(tmp_path)
    transport.http_client = _lose_post(http)
    with pytest.raises(ConnectionRefusedError):
        transport.advisory(PR, "candidate", intent, payload)
    transport.http_client = http
    transport.absence_quiet_seconds = 0
    http.pull["head"]["sha"] = "f" * 40
    result = transport.reconcile(PR, intent, payload)
    assert result["status"] == "ambiguous_pending"
    assert result["absence_unproven"] == "candidate-changed"
    assert _intent_status(store, intent) == "ambiguous"


def test_pending_attempt_settles_only_after_its_holder_is_gone(tmp_path):
    store, http, transport, intent, payload = environment(tmp_path)
    transport.absence_quiet_seconds = 0
    # A publisher that recorded its attempt and then crashed before any outcome.
    assert store.acquire_lease(RESOURCE, "corral", HEAD, os.getpid(), "crashed")[0]
    store.record_intent_pending(intent, RESOURCE, "corral", 1, HEAD, payload["base"], payload,
                                attempt_id="crashed")
    alive = transport.reconcile(PR, intent, payload)
    assert alive["absence_unproven"] == "attempt-in-flight"
    with store.transaction() as db:
        db.execute("UPDATE leases SET holder_pid=? WHERE resource=?", (_dead_pid(), RESOURCE))
    assert transport.reconcile(PR, intent, payload)["status"] == "absent"
    assert _lease(store) is None
    transport.advisory(PR, "candidate", intent, payload)
    assert len(http.posts) == 1


def test_absence_settlement_refuses_a_changed_attempt_or_a_receipt(tmp_path):
    store, http, transport, intent, payload = environment(tmp_path)
    transport.http_client = _lose_post(http)
    with pytest.raises(ConnectionRefusedError):
        transport.advisory(PR, "candidate", intent, payload)
    with store.transaction() as db:
        attempt, updated_at = db.execute(
            "SELECT attempt_id,updated_at FROM publication_intents WHERE intent=?",
            (intent,)).fetchone()
    stale = dict(attempt_id=attempt, prior_status="ambiguous", prior_updated_at=updated_at - 1,
                 evidence={})
    with pytest.raises(PermissionError, match="changed"):
        record_absence(store, intent, RESOURCE, **stale)
    store.replace("advisory_receipt", intent, {"intent": intent})
    with pytest.raises(PermissionError, match="receipt"):
        record_absence(store, intent, RESOURCE, **{**stale, "prior_updated_at": updated_at})
    assert _intent_status(store, intent) == "ambiguous"


def test_operator_recovers_a_dead_holder_lease_but_never_a_live_one(tmp_path):
    store, _http, _transport, _intent, _payload = environment(tmp_path)
    assert store.acquire_lease(RESOURCE, "corral", HEAD, os.getpid(), "live")[0]
    with pytest.raises(PermissionError, match="is alive"):
        store.recover_lease_operator(RESOURCE, authorized_by="operator")
    with store.transaction() as db:
        db.execute("UPDATE leases SET holder_pid=? WHERE resource=?", (_dead_pid(), RESOURCE))
    assert store.recover_lease_operator(RESOURCE, authorized_by="operator") is True
    assert store.acquire_lease(RESOURCE, "corral", HEAD, os.getpid(), "next")[0]


def test_migrated_ambiguous_intent_without_attempt_identity_can_settle(tmp_path):
    store, http, transport, intent, payload = environment(tmp_path)
    transport.absence_quiet_seconds = 0
    # A row written before attempt identities existed, and the lease it left behind.
    assert store.acquire_lease(RESOURCE, "corral", HEAD, _dead_pid(), "unbound")[0]
    store.record_intent_pending(intent, RESOURCE, "corral", 1, HEAD, payload["base"], payload,
                                attempt_id="unbound")
    store.record_intent_outcome(intent, RESOURCE, "ambiguous", error="lost before migration")
    with store.transaction() as db:
        db.execute("UPDATE publication_intents SET attempt_id='' WHERE intent=?", (intent,))
    assert transport.reconcile(PR, intent, payload)["status"] == "absent"
    assert store.acquire_lease(RESOURCE, "corral", HEAD, os.getpid(), "next")[1] == (
        "ambiguous_lease_blocking")
    assert store.recover_lease_operator(RESOURCE, authorized_by="operator") is True
    transport.advisory(PR, "candidate", intent, payload)
    assert len(http.posts) == 1
