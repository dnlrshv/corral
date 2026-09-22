"""Advisory review lifecycle contracts, canonical bindings, provenance, and reconciliation."""

from __future__ import annotations

from typing import Any

from .store import digest

RESERVED_PAYLOAD_KEYS = frozenset({
    "repo", "pr", "head", "base", "policy", "publisher", "event", "body",
    "verdict", "merged", "checks", "check", "approved", "state", "epoch",
    "commit_id", "repository", "pull_number", "sha", "target"
})

SUPPORTED_COMMENT_KEYS = frozenset({
    "path", "line", "side", "start_line", "startline", "start_side", "startside", "body", "comment"
})

FORBIDDEN_AUTHORITY_CLAIMS = frozenset({
    "APPROVE", "MERGE", "REQUEST_CHANGES", "approve", "merge", "request-changes"
})

EXCLUDED_REVIEW_METADATA_KEYS = frozenset({
    "candidate", "binding", "verdict", "actor", "usage", "invocation", "blocking", "body"
})


def _scan_for_authority_claims(val: Any) -> None:
    """Recursively scan payload values for unauthorized authority claims."""
    if isinstance(val, str):
        if val in FORBIDDEN_AUTHORITY_CLAIMS or val.upper() in {"APPROVE", "MERGE", "REQUEST_CHANGES"}:
            raise PermissionError(f"unauthorized authority claim: '{val}' is forbidden in advisory payload")
    elif isinstance(val, dict):
        for k, v in val.items():
            if k in {"checks", "check", "verdict", "approved", "merged", "ci"}:
                raise PermissionError(f"unauthorized authority claim: key '{k}' cannot claim review or check authority")
            _scan_for_authority_claims(v)
    elif isinstance(val, (list, tuple, set)):
        for item in val:
            _scan_for_authority_claims(item)


def validate_advisory_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    """Validate that extra payload does not spoof canonical bindings or claim authority."""
    if not payload:
        return {}
    if not isinstance(payload, dict):
        raise ValueError("advisory payload must be a mapping")

    for key in payload:
        if key in RESERVED_PAYLOAD_KEYS:
            raise ValueError(f"reserved binding conflict: '{key}' cannot be specified in payload")

    if "comments" in payload:
        comments = payload["comments"]
        if not isinstance(comments, list):
            raise ValueError("comments must be a list")
        for comment in comments:
            if not isinstance(comment, dict):
                raise ValueError("each comment must be a dictionary")
            for ck in comment:
                if ck not in SUPPORTED_COMMENT_KEYS:
                    raise ValueError(f"unsupported payload comment field: '{ck}'")

    _scan_for_authority_claims(payload)
    return dict(payload)


def check_marker_in_content(body: str | None, payload: dict[str, Any] | None) -> bool:
    """Detect review provenance marker in body or any payload string."""
    if body and "<!-- gemini-" in body:
        return True
    if payload:
        def _scan(obj: Any) -> bool:
            if isinstance(obj, str) and "<!-- gemini-" in obj:
                return True
            if isinstance(obj, dict):
                return any(_scan(v) for v in obj.values())
            if isinstance(obj, (list, tuple, set)):
                return any(_scan(item) for item in obj)
            return False
        if _scan(payload):
            return True
    return False


def validate_candidate_and_pr(
    store: Any, policy: dict[str, Any], pr: str, current: str
) -> tuple[str, str, str]:
    """Validate explicit PR repository identity and complete real candidate record."""
    if not isinstance(pr, str) or "#" not in pr:
        raise ValueError("explicit repository PR identity required, format: <repo>#<number>")
    parts = pr.split("#", 1)
    if not parts[0] or not parts[1]:
        raise ValueError("explicit repository PR identity required, format: <repo>#<number>")
    repo = parts[0]

    cand_record = store.get("candidate", current)
    if not cand_record:
        raise ValueError(f"complete real candidate record required; none found for '{current}'")

    if not cand_record.get("head") or not cand_record.get("base"):
        raise ValueError(f"candidate record '{current}' is incomplete: missing head or base")

    if cand_record.get("policy") != policy["version"]:
        raise PermissionError(
            f"candidate policy version mismatch: stored '{cand_record.get('policy')}' "
            f"vs current '{policy['version']}'; policy change invalidates prepared intent"
        )

    return repo, cand_record["head"], cand_record["base"]


def authenticate_advisory_publisher(
    lifecycle: Any,
    pr: str,
    candidate: str,
    body: str | None,
    payload: dict[str, Any] | None,
    *,
    publisher_token: str | None = None,
    review_request: str | None = None,
) -> tuple[str, dict[str, Any], str | None, dict[str, Any]]:
    """Authenticate publisher identity and review provenance.

    Requires explicit authenticated authorized publisher. For review reuse, requires
    authenticated current publisher token matching the review actor, and preserves exact
    stored full review body and extra payload without inventing transformations.
    Reject differing caller body/extras. Reject review markers in body/payload without
    verified review request provenance. Allow fresh private payloads bound to
    authenticated publishers without claiming inference provenance.
    """
    has_marker = check_marker_in_content(body, payload)

    if review_request:
        req = lifecycle.store.get("review_request", review_request)
        rev = lifecycle.store.get("review", review_request)
        if not req or not rev:
            raise PermissionError("unauthenticated review request provenance")
        if req.get("pr") != pr or req.get("candidate") != candidate:
            raise PermissionError("review request does not match candidate or PR")
        lens = req["lens"]
        if lens not in lifecycle.policy["lenses"]:
            raise PermissionError("review lens not registered in current policy")
        actor = rev["actor"]
        if actor not in lifecycle.policy["lenses"][lens]["actors"]:
            raise PermissionError("review actor not authorized in current policy for lens")
        if req["binding"] != lifecycle.binding(candidate, lens):
            raise PermissionError("review binding mismatch with current candidate/policy")

        # Explicit authenticated current publisher token is required for reuse
        if not publisher_token:
            raise PermissionError("explicit authenticated publisher token required for review reuse")
        pub_actor = lifecycle.publisher(publisher_token)
        if pub_actor != actor:
            raise PermissionError(f"publisher token '{pub_actor}' does not match review actor '{actor}'")

        # Original review record must have full content; refuse reuse if missing
        stored_body = rev.get("body")
        if stored_body is None:
            raise ValueError("original review record lacks full content needed to authenticate reuse")

        if body is not None and body != stored_body:
            raise PermissionError("caller body differs from stored authenticated review body")

        # Preserve exact stored review extra payload; reject differing caller extras
        stored_extra = {k: v for k, v in rev.items() if k not in EXCLUDED_REVIEW_METADATA_KEYS}
        _scan_for_authority_claims(stored_extra)

        if payload is not None:
            validated_caller_extra = validate_advisory_payload(payload)
            if validated_caller_extra != stored_extra:
                raise PermissionError("caller extra payload differs from stored authenticated review payload")

        effective_body = stored_body
        effective_extra = dict(stored_extra)

        provenance = {
            "review_request": review_request,
            "lens": lens,
            "actor": actor,
            "verdict": rev.get("verdict"),
        }
        return actor, provenance, effective_body, effective_extra

    if publisher_token:
        actor = lifecycle.publisher(publisher_token)
        if has_marker:
            raise PermissionError(
                "unauthenticated review provenance; marker alone insufficient without verified review request"
            )
        validated_extra = validate_advisory_payload(payload)
        provenance = {"actor": actor, "auth": "publisher_token"}
        return actor, provenance, body, validated_extra

    if has_marker:
        raise PermissionError(
            "unauthenticated review provenance; marker alone insufficient without verified review request"
        )

    raise PermissionError("explicit authenticated authorized publisher required; owner fallback is prohibited")


def compute_advisory_intent(
    repo: str,
    pr: str,
    head: str,
    base: str,
    policy_version: str,
    publisher: str,
    body: str | None,
    validated_extra: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    """Build canonical full payload and compute stable publication intent hash."""
    canonical_bindings = {
        "repo": repo,
        "pr": pr,
        "head": head,
        "base": base,
        "policy": policy_version,
        "publisher": publisher,
        "event": "COMMENT",
        "body": body,
    }
    full_payload = {
        **validated_extra,
        **canonical_bindings,
    }
    intent = digest({
        "repo": repo,
        "pr": pr,
        "head": head,
        "base": base,
        "policy": policy_version,
        "publisher": publisher,
        "event": "COMMENT",
        "payload": digest(full_payload),
    })
    return intent, full_payload
