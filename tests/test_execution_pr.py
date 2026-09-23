import pytest

from corral.execution.demo import fixture_profiles
from corral.execution.pr import FakeGitHub, PRLifecycle
from corral.execution.store import Store


@pytest.fixture
def lifecycle(tmp_path):
    policy = {"version": "v1", "routes": ["fixture"], "default_profile": "strong-low",
              "checks": ["lint", "test"], "check_actors": ["ci"], "lenses": {
                  "code": {"scope": ["code"], "actors": ["reviewer"], "head_bound": True},
                  "spec": {"scope": ["spec"], "actors": ["spec-reviewer"], "head_bound": False}}}
    return PRLifecycle(Store(tmp_path / "state"), FakeGitHub(), publishers={
        "ci": "ci-secret", "reviewer": "review-secret", "spec-reviewer": "spec-secret"},
        owner_token="owner", policy=policy, profiles=fixture_profiles())


def candidate(lifecycle, pr, head="h1", base="b1"):
    epoch = lifecycle.mutation("owner", pr, "legacy")
    return lifecycle.candidate("owner", pr, {"head": head, "base": base, "spec": "s1",
                                               "inputs": {"code": head, "spec": "s1"}}, owner="legacy", epoch=epoch)


def approve(lifecycle, pr, lens):
    key = lifecycle.request_review("owner", pr, lens)
    request = lifecycle.store.get("review_request", key)
    lifecycle.review("review-secret" if lens == "code" else "spec-secret", key,
                     {"candidate": request["candidate"], "binding": request["binding"],
                      "verdict": "approve", "usage": None})
    return key


def check_all(lifecycle, pr, current):
    for check in ("lint", "test"):
        lifecycle.check("ci-secret", pr, current, check, True)


def test_event_review_dedup_origins_and_lifetime(lifecycle):
    for origin in ("interactive", "wave", "manual"):
        event = {"repo": "fixture", "number": origin, "delivery": origin, "origin": origin}
        pr = lifecycle.ingest_event("ci-secret", event)
        assert lifecycle.ingest_event("ci-secret", event) == pr
        assert lifecycle.ingest_event("ci-secret", {**event, "delivery": origin + "scan"}) == pr
        assert lifecycle.store.get("pr_history", pr)["unobserved_implementation_usage"] is None
        candidate(lifecycle, pr)
        first = approve(lifecycle, pr, "code")
        assert lifecycle.request_review("owner", pr, "code", selection={"model": "fixture-weak"}) == first
        second = lifecycle.request_review("owner", pr, "code", opinion="fresh", reason="owner requested")
        assert first != second


def test_merge_guard_freshness_reuse_and_lost_ack(lifecycle):
    pr = "fixture#1"
    current = candidate(lifecycle, pr)
    epoch = lifecycle.mutation("owner", pr, "legacy")
    with pytest.raises(PermissionError):
        lifecycle.merge("owner", pr, "legacy", epoch, expected=current)
    code, spec = approve(lifecycle, pr, "code"), approve(lifecycle, pr, "spec")
    check_all(lifecycle, pr, current)
    new = candidate(lifecycle, pr, head="h2")
    assert lifecycle.request_review("owner", pr, "spec") == spec
    assert lifecycle.request_review("owner", pr, "code") != code
    with pytest.raises(PermissionError):
        lifecycle.merge("owner", pr, "legacy", epoch, expected=current)
    with pytest.raises(PermissionError):
        lifecycle.merge("owner", pr, "legacy", epoch, expected=new)
    approve(lifecycle, pr, "code")
    check_all(lifecycle, pr, new)
    with pytest.raises(ConnectionError):
        lifecycle.merge("owner", pr, "legacy", epoch, expected=new, lose_ack=True)
    assert lifecycle.transfer("owner", pr, "legacy", epoch, "corral", stopped=True)["state"] == "uncertain"
    assert lifecycle.store.ownership("pr:" + pr) == ("legacy", epoch, "active")
    assert lifecycle.merge("owner", pr, "legacy", epoch, expected=new)["reconciled"]
    assert len(lifecycle.github.merged) == 1
    candidate(lifecycle, pr, head="h2", base="b2")
    assert lifecycle.request_review("owner", pr, "spec") != spec


def test_authentic_publisher_blocking_and_cutover(lifecycle):
    pr = "fixture#2"
    current = candidate(lifecycle, pr)
    with pytest.raises(ValueError):
        lifecycle.check("ci-secret", pr, current, "lint", "false")
    key = lifecycle.request_review("owner", pr, "code")
    request = lifecycle.store.get("review_request", key)
    result = {"candidate": current, "binding": request["binding"], "verdict": "approve"}
    with pytest.raises(PermissionError):
        lifecycle.review("APPROVE", key, result)
    with pytest.raises(PermissionError):
        lifecycle.review("ci-secret", key, result)
    lifecycle.review("review-secret", key, {**result, "blocking": ["P1"]})
    approve(lifecycle, pr, "spec")
    check_all(lifecycle, pr, current)
    epoch = lifecycle.mutation("owner", pr, "legacy")
    with pytest.raises(PermissionError):
        lifecycle.merge("owner", pr, "legacy", epoch, expected=current)
    repair = lifecycle.repair("owner", pr, "conflict", owner="legacy", epoch=epoch)
    assert lifecycle.store.get("repair", repair)["selection"]["profile"]["id"] == "strong-low"
    assert lifecycle.transfer("owner", pr, "legacy", epoch, "corral", stopped=False)["state"] == "draining"
    with pytest.raises(PermissionError):
        lifecycle.mutation("owner", pr, "corral")
    transfer = lifecycle.transfer("owner", pr, "legacy", epoch, "corral", stopped=True)
    with pytest.raises(PermissionError):
        lifecycle.merge("owner", pr, "legacy", epoch, expected=current)
    rollback = lifecycle.transfer("owner", pr, "corral", transfer["epoch"], "legacy", stopped=True)
    assert rollback["epoch"] > transfer["epoch"]


def test_policy_and_spec_changes_invalidate_binding(lifecycle):
    pr = "fixture#policy"
    candidate(lifecycle, pr)
    before = approve(lifecycle, pr, "spec")
    lifecycle.policy["version"] = "v2"
    candidate(lifecycle, pr)
    changed_policy = lifecycle.request_review("owner", pr, "spec")
    assert changed_policy != before
    epoch = lifecycle.mutation("owner", pr, "legacy")
    lifecycle.candidate("owner", pr, {"head": "h1", "base": "b1", "spec": "s2",
        "inputs": {"code": "h1", "spec": "s2"}}, owner="legacy", epoch=epoch)
    assert lifecycle.request_review("owner", pr, "spec") != changed_policy

def test_advisory_payload_binding_and_authority_rejection(lifecycle):
    pr = "fixture#1"
    curr = candidate(lifecycle, pr)
    epoch = lifecycle.mutation("owner", pr, "legacy")
    pub_token = "review-secret"

    # Reject reserved binding keys in extra payload
    for key in ("repo", "pr", "head", "base", "policy", "publisher", "event", "body", "verdict", "checks"):
        with pytest.raises(ValueError, match="reserved binding conflict"):
            lifecycle.advisory(
                "owner", pr, "legacy", epoch, expected=curr,
                body="note", payload={key: "spoofed"}, publisher_token=pub_token
            )

    # Reject authority claims in payload values or nested structures
    for claim in ("APPROVE", "MERGE", "REQUEST_CHANGES", "approve"):
        with pytest.raises(PermissionError, match="unauthorized authority claim"):
            lifecycle.advisory(
                "owner", pr, "legacy", epoch, expected=curr,
                body="note", payload={"action": claim}, publisher_token=pub_token
            )

    with pytest.raises(PermissionError, match="unauthorized authority claim"):
        lifecycle.advisory(
            "owner", pr, "legacy", epoch, expected=curr,
            body="note", payload={"nested": {"ci": True}}, publisher_token=pub_token
        )

    # Canonical bindings enforced separately; full body/comments preserved
    comments_list = [{"path": "main.py", "line": 10, "comment": "look here"}]
    receipt = lifecycle.advisory(
        "owner", pr, "legacy", epoch, expected=curr,
        body="Advisory note", payload={"comments": comments_list}, publisher_token=pub_token
    )
    assert receipt["advisory"] is True
    assert receipt["transport"] == "synthetic_comment"
    assert receipt["repo"] == "fixture"
    assert receipt["head"] == "h1"
    assert receipt["base"] == "b1"
    assert receipt["publisher"] == "reviewer"

    stored_payload = lifecycle.store.get("advisory_payload", receipt["intent"])
    assert stored_payload["event"] == "COMMENT"
    assert stored_payload["body"] == "Advisory note"
    assert stored_payload["comments"] == comments_list
    assert stored_payload["repo"] == "fixture"
    assert stored_payload["publisher"] == "reviewer"

    # Must NOT merge, satisfy checks, or alter ownership
    assert lifecycle.github.merged == {}
    assert lifecycle.store.ownership("pr:" + pr) == ("legacy", epoch, "active")


def test_advisory_candidate_and_pr_validation(lifecycle):
    epoch = lifecycle.mutation("owner", "fixture#valid", "legacy")
    pub_token = "review-secret"

    # Explicit repository PR identity required (<repo>#<number>)
    with pytest.raises(ValueError, match="explicit repository PR identity required"):
        lifecycle.advisory("owner", "nonum", "legacy", epoch, expected="c1", publisher_token=pub_token)

    # Complete real candidate record required
    with pytest.raises(ValueError, match="complete real candidate record required"):
        lifecycle.advisory("owner", "fixture#valid", "legacy", epoch, expected="nonexistent", publisher_token=pub_token)

    # Incomplete candidate record (missing head or base)
    lifecycle.store.put_once("candidate", "incomplete", {"policy": "v1"})
    lifecycle.store.replace("pr_candidate", "fixture#valid", "incomplete")
    lifecycle.github.candidates["fixture#valid"] = "incomplete"
    with pytest.raises(ValueError, match="incomplete: missing head or base"):
        lifecycle.advisory("owner", "fixture#valid", "legacy", epoch, expected="incomplete", publisher_token=pub_token)

    # Candidate policy version mismatch invalidates prepared intent
    pr = "fixture#policy_test"
    curr = candidate(lifecycle, pr)
    pol_epoch = lifecycle.mutation("owner", pr, "legacy")
    lifecycle.policy["version"] = "v2"
    with pytest.raises(PermissionError, match="candidate policy version mismatch"):
        lifecycle.advisory("owner", pr, "legacy", pol_epoch, expected=curr, publisher_token=pub_token)


def test_advisory_publisher_attribution_and_provenance(lifecycle):
    pr = "fixture#prov"
    curr = candidate(lifecycle, pr)
    epoch = lifecycle.mutation("owner", pr, "legacy")

    # Missing publisher (owner fallback prohibited)
    with pytest.raises(PermissionError, match="owner fallback is prohibited"):
        lifecycle.advisory("owner", pr, "legacy", epoch, expected=curr, body="note")

    # Review marker without verified review request rejected (even with publisher_token)
    marker = "<!-- gemini-review-v1 -->"
    with pytest.raises(PermissionError, match="unauthenticated review provenance"):
        lifecycle.advisory("owner", pr, "legacy", epoch, expected=curr, body=f"{marker} note", publisher_token="review-secret")

    with pytest.raises(PermissionError, match="unauthenticated review provenance"):
        lifecycle.advisory(
            "owner", pr, "legacy", epoch, expected=curr,
            body="note", payload={"details": f"{marker} hidden"}, publisher_token="review-secret"
        )

    # Review request with verified provenance succeeds and allows marker
    req_key = lifecycle.request_review("owner", pr, "code")
    req = lifecycle.store.get("review_request", req_key)
    lifecycle.review("review-secret", req_key, {
        "candidate": req["candidate"],
        "binding": req["binding"],
        "verdict": "approve",
        "body": f"{marker} Verified review summary",
    })

    receipt_verified = lifecycle.advisory(
        "owner", pr, "legacy", epoch, expected=curr,
        body=f"{marker} Verified review summary", review_request=req_key, publisher_token="review-secret"
    )
    assert receipt_verified["advisory"] is True
    assert receipt_verified["publisher"] == "reviewer"

    # Review request provenance mismatch rejected
    fake_req = lifecycle.store.get("review_request", req_key)
    lifecycle.store.replace("review_request", req_key, {**fake_req, "candidate": "wrong_cand"})
    with pytest.raises(PermissionError, match="review request does not match candidate or PR"):
        lifecycle.advisory(
            "owner", pr, "legacy", epoch, expected=curr,
            review_request=req_key, publisher_token="review-secret"
        )

    # Fresh private payload bound to authenticated publisher without claiming inference provenance succeeds
    receipt_fresh = lifecycle.advisory(
        "owner", pr, "legacy", epoch, expected=curr,
        body="Fresh private note from reviewer", publisher_token="review-secret"
    )
    assert receipt_fresh["advisory"] is True
    assert receipt_fresh["publisher"] == "reviewer"


def test_advisory_transfer_blocking_and_stale_reconciliation(lifecycle):
    pr = "fixture#reconcile"
    c1 = candidate(lifecycle, pr, head="h1")
    epoch = lifecycle.mutation("owner", pr, "legacy")
    pub_token = "review-secret"

    # Advisory with lost ACK leaves unresolved advisory_intent
    body_c1 = "Lost ACK advisory comment"
    with pytest.raises(ConnectionError):
        lifecycle.advisory(
            "owner", pr, "legacy", epoch, expected=c1,
            body=body_c1, publisher_token=pub_token, lose_ack=True
        )

    # Transfer must block on unresolved advisory_intent
    uncertain = lifecycle.transfer("owner", pr, "legacy", epoch, "corral", stopped=True)
    assert uncertain["state"] == "uncertain"
    assert uncertain["reason"] == "advisory publication intent unresolved"

    # Candidate advances to c2
    candidate(lifecycle, pr, head="h2")

    # Calling advisory with expected=c1 now fails candidate freshness
    with pytest.raises(PermissionError, match="candidate freshness failure"):
        lifecycle.advisory("owner", pr, "legacy", epoch, expected=c1, body=body_c1, publisher_token=pub_token)

    # Find the unresolved intent
    with lifecycle.store.transaction() as db:
        rows = db.execute("SELECT key FROM records WHERE kind='advisory_intent'").fetchall()
    intent_c1 = rows[-1][0]

    # Reconcile validation rejections
    with pytest.raises(ValueError, match="candidate mismatch"):
        lifecycle.reconcile_advisory("owner", pr, "legacy", epoch, intent=intent_c1, candidate="wrong_cand")

    with pytest.raises(ValueError, match="body mismatch"):
        lifecycle.reconcile_advisory("owner", pr, "legacy", epoch, intent=intent_c1, candidate=c1, body="wrong body")

    with pytest.raises(PermissionError, match="publisher mismatch"):
        lifecycle.reconcile_advisory(
            "owner", pr, "legacy", epoch, intent=intent_c1,
            candidate=c1, body=body_c1, publisher_token="ci-secret"
        )

    # Stale candidate intent reconciled without submitting to transport
    initial_transport_calls = len(lifecycle.github.advisory_calls)
    receipt = lifecycle.reconcile_advisory(
        "owner", pr, "legacy", epoch, intent=intent_c1,
        candidate=c1, body=body_c1, publisher_token=pub_token
    )
    assert receipt["reconciled"] is True
    assert receipt["stale_reconciliation"] is True
    # Transport was NOT called again during reconciliation
    assert len(lifecycle.github.advisory_calls) == initial_transport_calls

    # Transfer now unblocked and proceeds
    transfer_result = lifecycle.transfer("owner", pr, "legacy", epoch, "corral", stopped=False)
    assert transfer_result["state"] == "draining"


def test_advisory_interleaved_intents_idempotence(lifecycle):
    pr = "fixture#interleave"
    curr = candidate(lifecycle, pr)
    epoch = lifecycle.mutation("owner", pr, "legacy")
    pub_token = "review-secret"

    # Intent A: submitted with lose_ack=True
    with pytest.raises(ConnectionError):
        lifecycle.advisory(
            "owner", pr, "legacy", epoch, expected=curr,
            body="Comment A", publisher_token=pub_token, lose_ack=True
        )

    # Intent B: submitted and succeeds
    receipt_b = lifecycle.advisory(
        "owner", pr, "legacy", epoch, expected=curr,
        body="Comment B", publisher_token=pub_token
    )
    assert receipt_b["advisory"] is True
    assert receipt_b["reconciled"] is False

    # Retry Intent A: recognized from FakeGitHub keyed by intent ID, not overwritten by B
    initial_transport_calls = len(lifecycle.github.advisory_calls)
    receipt_a = lifecycle.advisory(
        "owner", pr, "legacy", epoch, expected=curr,
        body="Comment A", publisher_token=pub_token
    )
    assert receipt_a["advisory"] is True
    assert receipt_a["reconciled"] is True
    # No extra transport call occurred on retry
    assert len(lifecycle.github.advisory_calls) == initial_transport_calls

    # Reuse of receipt B returns existing receipt without transport call
    receipt_b_retry = lifecycle.advisory(
        "owner", pr, "legacy", epoch, expected=curr,
        body="Comment B", publisher_token=pub_token
    )
    assert receipt_b_retry["intent"] == receipt_b["intent"]
    assert len(lifecycle.github.advisory_calls) == initial_transport_calls
