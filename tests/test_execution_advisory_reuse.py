import pytest

from tests.test_execution_pr import candidate, lifecycle  # noqa: F401


def test_advisory_review_reuse_regressions(lifecycle):  # noqa: F811
    pr = "fixture#reuse"
    curr = candidate(lifecycle, pr)
    epoch = lifecycle.mutation("owner", pr, "legacy")
    marker = "<!-- gemini-review-v1 -->"

    # 1. Unavailable original payload in review record
    req_no_content = lifecycle.request_review("owner", pr, "code")
    req_nc = lifecycle.store.get("review_request", req_no_content)
    lifecycle.review("review-secret", req_no_content, {
        "candidate": req_nc["candidate"],
        "binding": req_nc["binding"],
        "verdict": "approve",
        # Missing "body"
    })
    with pytest.raises(ValueError, match="original review record lacks full content"):
        lifecycle.advisory(
            "owner", pr, "legacy", epoch, expected=curr,
            review_request=req_no_content, publisher_token="review-secret"
        )

    # 2. Missing or mismatched publisher auth on reuse
    req_full = lifecycle.request_review("owner", pr, "code", opinion="fresh", reason="full review")
    req_f = lifecycle.store.get("review_request", req_full)
    review_comments = [{"path": "src/app.py", "line": 42, "body": "nit: check bounds"}]
    lifecycle.review("review-secret", req_full, {
        "candidate": req_f["candidate"],
        "binding": req_f["binding"],
        "verdict": "approve",
        "body": f"{marker} Verified code review",
        "comments": review_comments,
    })

    # Missing publisher_token
    with pytest.raises(PermissionError, match="explicit authenticated publisher token required for review reuse"):
        lifecycle.advisory(
            "owner", pr, "legacy", epoch, expected=curr,
            review_request=req_full
        )

    # Publisher token for different actor
    with pytest.raises(PermissionError, match="publisher token 'spec-reviewer' does not match review actor 'reviewer'"):
        lifecycle.advisory(
            "owner", pr, "legacy", epoch, expected=curr,
            review_request=req_full, publisher_token="spec-secret"
        )

    # 3. Changed body vs stored review
    with pytest.raises(PermissionError, match="caller body differs from stored authenticated review body"):
        lifecycle.advisory(
            "owner", pr, "legacy", epoch, expected=curr,
            body=f"{marker} Tampered review body", review_request=req_full, publisher_token="review-secret"
        )

    # 4. Changed inline comments / extra payload vs stored review
    tampered_comments = [{"path": "src/app.py", "line": 42, "body": "TAMPERED"}]
    with pytest.raises(PermissionError, match="caller extra payload differs from stored authenticated review payload"):
        lifecycle.advisory(
            "owner", pr, "legacy", epoch, expected=curr,
            payload={"comments": tampered_comments}, review_request=req_full, publisher_token="review-secret"
        )

    # 5. Exact successful reuse and dedup
    initial_transport_calls = len(lifecycle.github.advisory_calls)
    receipt1 = lifecycle.advisory(
        "owner", pr, "legacy", epoch, expected=curr,
        review_request=req_full, publisher_token="review-secret"
    )
    assert receipt1["advisory"] is True
    assert receipt1["publisher"] == "reviewer"
    assert receipt1["repo"] == "fixture"
    assert len(lifecycle.github.advisory_calls) == initial_transport_calls + 1

    stored_payload = lifecycle.store.get("advisory_payload", receipt1["intent"])
    assert stored_payload["body"] == f"{marker} Verified code review"
    assert stored_payload["comments"] == review_comments
    assert stored_payload["publisher"] == "reviewer"
    assert stored_payload["event"] == "COMMENT"

    # Dedup: subsequent identical publication returns existing receipt without transport call
    receipt2 = lifecycle.advisory(
        "owner", pr, "legacy", epoch, expected=curr,
        body=f"{marker} Verified code review", payload={"comments": review_comments},
        review_request=req_full, publisher_token="review-secret"
    )
    assert receipt2["intent"] == receipt1["intent"]
    assert len(lifecycle.github.advisory_calls) == initial_transport_calls + 1
