"""Real transport regressions using canonical full-policy, full-receipt fixtures."""

import pytest

from tests.advisory_http_fixture import ACTOR, HEAD, PR, environment
from corral.execution.github_support import comments_match, persist_delivery_receipt


def test_one_post_exact_wire_and_durable_dedup(tmp_path):
    store, http, transport, intent, payload = environment(tmp_path)
    receipt = transport.advisory(PR, "candidate", intent, payload)
    assert http.posts == [{"commit_id": HEAD, "event": "COMMENT", "body": "advisory"}]
    assert receipt["review_id"] == 901 and transport.has_advisory(intent)
    http.reviews.clear()
    assert transport.advisory(PR, "candidate", intent, payload) == receipt
    assert len(http.posts) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("policy", "c" * 64),
        ("body", "mutated"),
        ("publisher", "github-actions[bot]"),
        ("event", "APPROVE"),
    ],
)
def test_mutated_payload_never_posts(tmp_path, field, value):
    _, http, transport, intent, payload = environment(tmp_path)
    payload[field] = value
    with pytest.raises((PermissionError, ValueError)):
        transport.advisory(PR, "candidate", intent, payload)
    assert not http.posts


@pytest.mark.parametrize(
    "mutation", ["policy", "actor", "head", "hash", "epoch", "short-intent"]
)
def test_preflight_revalidates_all_boundaries(tmp_path, mutation):
    store, http, transport, intent, payload = environment(tmp_path)
    if mutation == "policy":
        http.rules[0]["enforcement"] = "disabled"
    elif mutation == "actor":
        http.actor = "github-actions[bot]"
    elif mutation == "head":
        http.pull["head"]["sha"] = "f" * 40
    elif mutation in ("hash", "epoch"):
        approval = store.get("advisory_approval", intent)
        approval.pop("canonical_wire_hash" if mutation == "hash" else "epoch")
        store.replace("advisory_approval", intent, approval)
    else:
        intent = "short"
    with pytest.raises((PermissionError, ValueError)):
        transport.advisory(PR, "candidate", intent, payload)
    assert not http.posts


@pytest.mark.parametrize("field", ["id", "user", "body", "commit_id", "state"])
def test_incomplete_ack_stays_ambiguous(tmp_path, field):
    store, http, transport, intent, payload = environment(tmp_path)

    def malformed(review):
        review.pop(field)
        return review

    http.after_post = malformed
    with pytest.raises(ValueError):
        transport.advisory(PR, "candidate", intent, payload)
    assert store.get("advisory_pending", intent)["status"] == "ambiguous"
    assert not transport.has_advisory(intent)
    assert len(http.posts) == 1


@pytest.mark.parametrize("kind", ["head", "policy"])
def test_postflight_drift_retains_evidence_and_historical_reconciliation(
    tmp_path, kind
):
    store, http, transport, intent, payload = environment(tmp_path)

    def drift(review):
        if kind == "head":
            http.pull["head"]["sha"] = "f" * 40
        else:
            http.rules[0]["enforcement"] = "disabled"
        return review

    http.after_post = drift
    with pytest.raises(PermissionError):
        transport.advisory(PR, "candidate", intent, payload)
    assert store.get("advisory_pending", intent)["observed_review"]["id"] == 901
    assert not transport.has_advisory(intent)
    receipt = transport.reconcile(PR, intent, payload, is_stale=True)
    assert receipt["delivered"] and not receipt["current_candidate_verified"]
    assert len(http.posts) == 1


def test_lost_ack_and_mutated_reconciliation(tmp_path):
    _, http, transport, intent, payload = environment(tmp_path)
    with pytest.raises(ConnectionError):
        transport.advisory(PR, "candidate", intent, payload, lose_ack=True)
    bad = {**payload, "policy": "c" * 64}
    with pytest.raises(PermissionError):
        transport.reconcile(PR, intent, bad)
    http.reviews.clear()
    assert transport.reconcile(PR, intent, payload)["delivered"] == "unknown"
    with pytest.raises(PermissionError):
        transport.advisory(PR, "candidate", intent, payload)
    assert len(http.posts) == 1


def test_remote_without_provenance_blocks_post(tmp_path):
    _, http, transport, intent, payload = environment(tmp_path)
    http.reviews = [
        {
            "id": 1,
            "user": {"login": ACTOR},
            "commit_id": HEAD,
            "state": "COMMENTED",
            "body": payload["body"],
        }
    ]
    http.comments[1] = []
    with pytest.raises(PermissionError, match="without local"):
        transport.advisory(PR, "candidate", intent, payload)
    assert not http.posts


def test_comment_api_metadata_and_position_not_conflated():
    modern = {"path": "README.md", "body": "nit", "line": 7, "side": "RIGHT"}
    assert comments_match(
        [{**modern, "position": 2, "subject_type": "line", "id": 9}], [modern]
    )
    assert not comments_match(
        [{"path": "README.md", "body": "nit", "position": 7}], [modern]
    )
    assert not comments_match([{**modern, "side": "LEFT"}], [modern])


def test_paginated_extra_comments_prevent_acceptance(tmp_path):
    comments = [
        {"path": "README.md", "line": i + 1, "side": "RIGHT", "body": "nit"}
        for i in range(100)
    ]
    store, http, transport, intent, payload = environment(tmp_path, comments=comments)

    def extra(review):
        http.comments[901].append({**comments[0], "line": 101})
        return review

    http.after_post = extra
    with pytest.raises(ValueError, match="comments differ"):
        transport.advisory(PR, "candidate", intent, payload)
    assert not store.get("advisory_receipt", intent)


def test_reconciliation_does_not_complete_newer_lease_or_change_receipt(tmp_path):
    store, _, transport, intent, payload = environment(tmp_path)
    receipt = transport.advisory(PR, "candidate", intent, payload)
    assert store.acquire_lease("pr:" + PR, "corral", HEAD, 123, "new-attempt")[0]
    persist_delivery_receipt(store, intent, "pr:" + PR, receipt)
    with store.transaction() as db:
        assert db.execute("SELECT attempt_id,status FROM leases").fetchone() == (
            "new-attempt",
            "in_flight",
        )
    for tampered in ({**receipt, "review_id": 902}, {**receipt, "policy": "x"}):
        with pytest.raises(PermissionError):
            persist_delivery_receipt(store, intent, "pr:" + PR, tampered)
    assert store.get("advisory_receipt", intent) == receipt


def test_null_body_reviews_from_others_do_not_block_publication(tmp_path):
    store, http, transport, intent, payload = environment(tmp_path)
    # GitHub reports ``"body": null`` for reviews that only carry line comments.
    http.reviews = [{"id": 7, "user": {"login": "someone"}, "commit_id": HEAD,
                     "state": "COMMENTED", "body": None}]
    receipt = transport.advisory(PR, "candidate", intent, payload)
    assert receipt["review_id"] == 901 and len(http.posts) == 1
    assert transport.reconcile(PR, intent, payload)["review_id"] == 901
