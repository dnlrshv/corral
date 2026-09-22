"""Comprehensive regression and contract verification tests for M5 transport, policy, and cohort boundaries."""

import json
import os
from pathlib import Path
from typing import Any
import pytest

from corral.execution.advisory import compute_advisory_intent
from corral.execution.advisory_cli import main as cli_main
from corral.execution.github_advisory import GitHubAdvisoryTransport, _NoRedirectHandler
from corral.execution.legacy_adapter import (
    bootstrap_cohort_store,
    check_cohort_admission,
    pr_lease,
)
from corral.execution.store import Store
from corral.execution.github_support import compute_canonical_wire_hash

REPO = "example/project"
PR101 = "example/project#101"
PR102 = "example/project#102"
HEAD = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
BASE = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
REAL_POLICY = "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"


class MockOpenerHTTP:
    def __init__(self, pull_head: str = HEAD, pull_base: str = BASE, reviews: list | None = None):
        self.pull_head = pull_head
        self.pull_base = pull_base
        self.reviews = list(reviews or [])
        self.post_count = 0
        self.posted_bodies: list[dict[str, Any]] = []

    def __call__(self, method: str, path: str, *, headers=None, json=None):
        if method == "GET" and path == "/user":
            return {"login": "github-actions[bot]", "id": 41898282}
        if method == "GET" and "/pulls/" in path and "/reviews" not in path:
            return {"number": 101, "state": "open", "head": {"sha": self.pull_head}, "base": {"sha": self.pull_base}}
        if method == "GET" and "/reviews" in path and "/comments" not in path:
            return list(self.reviews)
        if method == "GET" and "/comments" in path:
            return []
        if method == "POST" and "/reviews" in path:
            self.post_count += 1
            self.posted_bodies.append(json)
            rev = {"id": 1000 + self.post_count, "state": "COMMENTED", "commit_id": self.pull_head, "body": json.get("body")}
            self.reviews.append(rev)
            return rev
        return {}


def test_prepare_proposal_only_no_autoapproval_or_owner_mutation(tmp_path: Path):
    store_file = tmp_path / "store.sqlite"
    intent_file = tmp_path / "intent.txt"
    proposal_file = tmp_path / "proposal.json"

    rc = cli_main([
        "prepare", "--repo", REPO, "--pr", PR101, "--head", HEAD, "--base", BASE,
        "--publisher", "review-publisher", "--body", "Proposal only review body",
        "--policy", REAL_POLICY,
        "--store", str(store_file), "--output-intent", str(intent_file),
        "--output-proposal", str(proposal_file),
    ])
    assert rc == 0
    intent = intent_file.read_text().strip()
    assert len(intent) == 64

    store = Store(store_file)
    assert store.get("advisory_payload", intent) is not None
    assert store.get("advisory_approval", intent) is None
    assert store.ownership("pr:" + PR101) is None

    proposal = json.loads(proposal_file.read_text())
    assert proposal["authorized"] is False
    assert proposal["status"] == "prepared"
    assert proposal["intent"] == intent


def test_import_approved_artifact_and_epoch_fence(tmp_path: Path):
    store_file = tmp_path / "store.sqlite"
    store = Store(store_file)
    store.acquire("pr:" + PR101, "corral")

    intent, full_payload = compute_advisory_intent(
        repo=REPO, pr=PR101, head=HEAD, base=BASE,
        policy_version=REAL_POLICY, publisher="review-publisher",
        body="Exact approved body", validated_extra={},
    )
    artifact_path = tmp_path / "approved_artifact.json"
    artifact_path.write_text(json.dumps({
        "repo": REPO, "pr": PR101, "head": HEAD, "base": BASE,
        "policy": REAL_POLICY, "publisher": "review-publisher",
        "intent": intent, "epoch": 1, "authorized": True,
        "canonical_wire_hash": compute_canonical_wire_hash(HEAD, "Exact approved body", [])[0],
        "authorized_by": "fixture operator",
        "payload": full_payload,
    }))

    rc = cli_main(["import-approved", "--artifact", str(artifact_path), "--store", str(store_file)])
    assert rc == 0

    approval = store.get("advisory_approval", intent)
    assert approval is not None
    assert approval["authorized"] is True


def test_selected_cohort_boundary_allows_unrelated_pr102(tmp_path: Path):
    db_path = tmp_path / "authority.db"
    cfg_file = tmp_path / "cohort_control.json"
    cfg_file.write_text(json.dumps({
        "authority_db": str(db_path),
        "cohorts": {PR101: {"owner": "legacy", "epoch": 1, "status": "active"}},
    }))

    # 1. Missing DB fails closed for selected PR101
    admitted_101, reason_101, _ = check_cohort_admission(repo=REPO, pr=101, cohort_control_path=cfg_file)
    assert admitted_101 is False
    assert reason_101 == "missing_authority_db"

    # 2. Definitively unmanaged PR102 is permitted without DB side effects
    admitted_102, reason_102, rec_102 = check_cohort_admission(repo=REPO, pr=102, cohort_control_path=cfg_file)
    assert admitted_102 is True
    assert reason_102 == "legacy_unmanaged"
    assert rec_102["status"] == "unmanaged"

    # 3. pr_lease allows PR102 with unmanaged dummy lease
    with pr_lease(tmp_path, REPO, 102, "a" * 40, cohort_control_path=cfg_file) as lease_102:
        assert lease_102.acquired is True
        assert lease_102.can_publish() is True
        assert lease_102.store is None

    # 4. Bootstrap and verify PR101 managed admission
    bootstrap_cohort_store(cfg_file, db_path)
    admitted_101_after, reason_101_after, _ = check_cohort_admission(repo=REPO, pr=101, cohort_control_path=cfg_file)
    assert admitted_101_after is True
    assert reason_101_after == "active_legacy"


def test_store_record_intent_pending_requires_attempt_id_and_validates_lease(tmp_path: Path):
    store = Store(tmp_path / "store.sqlite")
    res = "pr:" + PR101
    store.acquire(res, "corral")

    intent, payload = compute_advisory_intent(
        repo=REPO, pr=PR101, head=HEAD, base=BASE,
        policy_version=REAL_POLICY, publisher="review-publisher",
        body="Advisory note", validated_extra={},
    )

    # Missing attempt_id must raise ValueError/TypeError
    with pytest.raises((ValueError, TypeError)):
        store.record_intent_pending(intent, res, "corral", 1, HEAD, BASE, payload, attempt_id="")

    # Missing lease must fail
    with pytest.raises(PermissionError, match="missing lease"):
        store.record_intent_pending(intent, res, "corral", 1, HEAD, BASE, payload, attempt_id="attempt-1")

    # Acquired lease with matching attempt succeeds
    acquired, _, _, epoch = store.acquire_lease(res, "corral", HEAD, os.getpid(), "attempt-1")
    assert acquired is True
    store.record_intent_pending(intent, res, "corral", epoch, HEAD, BASE, payload, attempt_id="attempt-1")

    # Repeat pending must fail
    with pytest.raises(PermissionError, match="already recorded with status 'pending'"):
        store.record_intent_pending(intent, res, "corral", epoch, HEAD, BASE, payload, attempt_id="attempt-1")


def test_operator_recovery_refuses_when_pending_intent_exists(tmp_path: Path):
    store = Store(tmp_path / "store.sqlite")
    res = "pr:" + PR101
    store.acquire(res, "corral")
    store.acquire_lease(res, "corral", HEAD, os.getpid(), "att-recovery")
    store.record_intent_pending("intent-recov", res, "corral", 1, HEAD, BASE, {"k": "v"}, attempt_id="att-recovery")

    # Unresolved pending intent forbids recovery
    with pytest.raises(PermissionError, match="unresolved intent 'intent-recov' exists"):
        store.recover_lease_operator(res, authorized_by="root-admin")

    # Missing authorizer raises
    with pytest.raises(PermissionError, match="explicit operator authorization required"):
        store.recover_lease_operator(res, authorized_by="")

    # Once resolved, operator recovery succeeds
    store.record_intent_outcome("intent-recov", res, "delivered", review_id=123)
    recovered = store.recover_lease_operator(res, authorized_by="root-admin")
    assert recovered is True


def test_reconcile_rejects_malformed_payload_without_changing_intent(tmp_path: Path):
    store = Store(tmp_path / "store.sqlite")
    res = "pr:" + PR101
    store.acquire(res, "corral")
    store.acquire_lease(res, "corral", HEAD, os.getpid(), "att-rec-stale")
    intent = "intent-ambig"
    store.record_intent_pending(intent, res, "corral", 1, HEAD, BASE, {"head": HEAD, "base": BASE, "body": "text"}, attempt_id="att-rec-stale")
    store.record_intent_outcome(intent, res, "ambiguous", error="Network dropped ACK")

    transport = GitHubAdvisoryTransport(store=store, http_client=MockOpenerHTTP(reviews=[]), allow_network=False)
    with pytest.raises(ValueError, match="incomplete or unsupported real advisory payload"):
        transport.reconcile_shared_authority(PR101, intent, {"head": HEAD, "base": BASE, "body": "text"}, is_stale=True)

    # Pending record in publication_intents remains ambiguous
    with store.transaction() as db:
        row = db.execute("SELECT status FROM publication_intents WHERE intent=?", (intent,)).fetchone()
        assert row[0] == "ambiguous"


def test_reconcile_valid_unresolved_intent_remains_ambiguous(tmp_path: Path):
    store = Store(tmp_path / "store.sqlite")
    resource = "pr:" + PR101
    store.acquire(resource, "corral")
    store.acquire_lease(resource, "corral", HEAD, os.getpid(), "lost-ack")
    intent, payload = compute_advisory_intent(
        REPO, PR101, HEAD, BASE, REAL_POLICY, "github-actions[bot]", "advisory", {})
    store.record_intent_pending(intent, resource, "corral", 1, HEAD, BASE, payload,
                                attempt_id="lost-ack")
    store.record_intent_outcome(intent, resource, "ambiguous", error="Lost acknowledgement")
    http = MockOpenerHTTP(reviews=[])
    transport = GitHubAdvisoryTransport(store=store, http_client=http, allow_network=False)
    receipt = transport.reconcile_shared_authority(PR101, intent, payload, is_stale=True)
    assert receipt["reconciled"] is False
    assert receipt["delivered"] == "unknown"
    assert http.post_count == 0
    with store.transaction() as db:
        assert db.execute("SELECT status FROM publication_intents WHERE intent=?",
                          (intent,)).fetchone()[0] == "ambiguous"


def test_post_receipt_validation_and_no_redirect_handler(tmp_path: Path):
    store = Store(tmp_path / "store.sqlite")
    res = "pr:" + PR101
    store.acquire(res, "corral")

    handler = _NoRedirectHandler()
    with pytest.raises(PermissionError, match="HTTP redirect prohibited"):
        handler.redirect_request(None, None, 301, "Moved", {}, "https://evil.redirect.com")
