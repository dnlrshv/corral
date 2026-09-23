"""Strict bindings at the real publication boundary (independent of simulation)."""

import re
from typing import Any

from .advisory import compute_advisory_intent
from .github_support import compute_canonical_wire_hash, parse_pr_identity
from .policy import fetch_policy_snapshot
from .policy_inputs import normalize as normalize_policy_inputs


def validate_payload(
    pr: str, intent: str, payload: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    required = {"repo", "pr", "head", "base", "policy", "publisher", "event", "body"}
    if (
        not isinstance(payload, dict)
        or not required <= payload.keys()
        or payload.keys() - required - {"comments"}
    ):
        raise ValueError("incomplete or unsupported real advisory payload")
    repo, number = parse_pr_identity(pr)
    if (
        payload["repo"] != repo
        or payload["pr"] != pr
        or number <= 0
        or payload["event"] != "COMMENT"
    ):
        raise ValueError("advisory repository/PR/event binding mismatch")
    if (
        not isinstance(payload["body"], str)
        or not isinstance(payload["publisher"], str)
        or not payload["publisher"]
    ):
        raise ValueError("advisory body and publisher required")
    for field, size in (("head", 40), ("base", 40), ("policy", 64)):
        if not isinstance(payload[field], str) or not re.fullmatch(
            f"[0-9a-f]{{{size}}}", payload[field]
        ):
            raise ValueError(f"invalid advisory {field}")
    if not isinstance(intent, str) or not re.fullmatch("[0-9a-f]{64}", intent):
        raise ValueError("intent must be a canonical SHA256")
    comments = payload.get("comments", [])
    if not isinstance(comments, list):
        raise ValueError("comments must be a list")
    allowed = {"path", "body", "line", "side", "start_line", "start_side"}
    for comment in comments:
        if not isinstance(comment, dict) or comment.keys() - allowed:
            raise ValueError("unsupported comment fields")
        if (
            not isinstance(comment.get("path"), str)
            or not comment["path"]
            or not isinstance(comment.get("body"), str)
        ):
            raise ValueError("comment path/body required")
        if (
            type(comment.get("line")) is not int
            or comment["line"] <= 0
            or comment.get("side") not in ("LEFT", "RIGHT")
        ):
            raise ValueError("comment requires positive line and explicit side")
        if ("start_line" in comment) != ("start_side" in comment):
            raise ValueError("multiline comment requires start_line and start_side")
        if "start_line" in comment and (
            type(comment["start_line"]) is not int
            or comment["start_line"] <= 0
            or comment["start_side"] not in ("LEFT", "RIGHT")
        ):
            raise ValueError("invalid multiline comment")
    recomputed, normalized = compute_advisory_intent(
        repo,
        pr,
        payload["head"],
        payload["base"],
        payload["policy"],
        payload["publisher"],
        payload["body"],
        {"comments": comments} if "comments" in payload else {},
    )
    if intent != recomputed or normalized != payload:
        raise PermissionError("advisory full payload hash mismatch")
    return compute_canonical_wire_hash(payload["head"], payload["body"], comments)


def require_approval_provenance(authorized_by: Any) -> str:
    """Approval provenance must name a non-blank operator or campaign authorization."""
    if not isinstance(authorized_by, str) or not authorized_by.strip():
        raise PermissionError("explicit approval provenance required")
    return authorized_by


def verify_approval(
    transport: Any, pr: str, intent: str, payload: dict[str, Any]
) -> tuple[int, dict[str, Any]]:
    wire_hash, wire = validate_payload(pr, intent, payload)
    actor = transport._validate_bridge_identity(expected_actor=payload["publisher"])
    if actor != payload["publisher"]:
        raise PermissionError("authenticated publisher mismatch")
    ownership = transport.store.ownership("pr:" + pr)
    if not ownership or ownership[0] != "corral" or ownership[2] != "active":
        raise PermissionError("ownership drift: corral does not hold active ownership")
    approval = transport.store.get("advisory_approval", intent)
    if not approval or approval.get("authorized") is not True:
        raise PermissionError("missing explicit candidate-bound approval")
    require_approval_provenance(approval.get("authorized_by"))
    bindings = {
        k: payload[k] for k in ("repo", "pr", "head", "base", "policy", "publisher")
    }
    bindings.update(intent=intent, epoch=ownership[1], canonical_wire_hash=wire_hash)
    if any(approval.get(k) != value for k, value in bindings.items()):
        raise PermissionError("candidate-bound approval parameters do not match intent")
    if transport.store.get("advisory_payload", intent) != payload:
        raise PermissionError("stored approved payload differs from request")
    pull = transport._verify_candidate_remote(
        payload["repo"], parse_pr_identity(pr)[1], payload["head"], payload["base"]
    )
    base_ref = (pull.get("base") or {}).get("ref")
    configured_ref = normalize_policy_inputs(transport.policy_inputs)["base_ref"]
    if not configured_ref or base_ref != configured_ref:
        raise PermissionError("remote base branch differs from trusted repository policy")
    snapshot = fetch_policy_snapshot(
        payload["repo"],
        payload["base"],
        base_ref=base_ref,
        http_client=transport._request,
        policy_inputs=transport.policy_inputs,
    )
    if snapshot["enforcement_digest"] != payload["policy"]:
        raise PermissionError("live policy digest changed")
    return ownership[1], wire
