"""Configured legacy publisher contracts, separate from model-written markers."""
from .store import digest


def import_review(lifecycle, publisher_token, request_key, envelope, contract):
    """Envelope is a saved/fake GitHub event; real transport auth remains required."""
    actor = lifecycle.publisher(publisher_token)
    run = envelope["workflow_run"]
    request = lifecycle.store.get("review_request", request_key)
    candidate = lifecycle.store.get("candidate", request["candidate"])
    expected = {"head_sha": candidate["head"], "base_sha": candidate["base"],
                "policy": candidate["policy"]}
    if (actor not in contract["actors"] or run["path"] != contract["workflow_path"]
            or run["conclusion"] != "success" or not run.get("id")
            or any(run.get(key) != value for key, value in expected.items())):
        raise PermissionError("legacy protected-run provenance mismatch")
    review = envelope["review"]
    if review.get("actor") != actor or review.get("run_id") != run["id"]:
        raise PermissionError("legacy review is not bound to authenticated publisher run")
    result = {"candidate": request["candidate"], "binding": request["binding"],
              "verdict": review["verdict"], "blocking": review.get("blocking", []),
              "invocation": envelope.get("invocation"), "usage": envelope.get("usage"),
              "source_receipt": digest(envelope)}
    if "body" in review:
        result["body"] = review["body"]
    if "comments" in review:
        result["comments"] = review["comments"]
    lifecycle.review(publisher_token, request_key, result)
    return result
