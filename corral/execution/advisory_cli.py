"""Executable CLI entrypoint for Corral advisory preparation, reconciliation, and publication."""

from __future__ import annotations

import argparse
import re
import json
import sys
from pathlib import Path
from typing import Any

from .advisory import compute_advisory_intent, validate_advisory_payload
from .github_advisory import GitHubAdvisoryTransport
from .github_support import compute_canonical_wire_hash, parse_pr_identity
from .publication_validation import validate_payload
from .policy import _get_auth_token, load_policy_snapshot
from .store import Store, record_advisory_approval
from .policy_inputs import load as load_policy_inputs


def _load_payload(payload_file: Path | None, body: str | None, expected_head: str | None = None) -> tuple[str, dict[str, Any]]:
    extra: dict[str, Any] = {}
    body_text = body or ""
    if payload_file:
        p = Path(payload_file).resolve()
        if not p.exists():
            raise FileNotFoundError(f"Advisory payload file not found: {p}")
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("Advisory payload file must contain a JSON dictionary")
        forbidden = set(raw) - {"body", "comments", "commit_id", "event"}
        if forbidden:
            raise ValueError("payload file contains unsupported binding fields")
        if "event" in raw and raw["event"] != "COMMENT":
            raise ValueError("payload file event must be COMMENT")
        if "commit_id" in raw and raw["commit_id"] != expected_head:
            raise ValueError("commit_id mismatch in payload file")
        if body is not None and "body" in raw and raw["body"] != body:
            raise ValueError("conflicting explicit body and payload body")
        body_text = raw.get("body", body_text)
        extra = {"comments": raw["comments"]} if "comments" in raw else {}

    return body_text, validate_advisory_payload(extra)


def _resolve_policy(args: argparse.Namespace) -> str:
    """Resolve policy snapshot digest without arbitrary v1 defaults."""
    explicit = getattr(args, "policy", None)
    if getattr(args, "policy_snapshot", None):
        snap = load_policy_snapshot(args.policy_snapshot)
        if snap["repo"] != args.repo or snap["base_sha"] != args.base:
            raise ValueError("policy snapshot repository/base mismatch")
        if explicit is not None and explicit != snap["enforcement_digest"]:
            raise ValueError("explicit policy differs from snapshot")
        explicit = snap["enforcement_digest"]
    if not isinstance(explicit, str) or not re.fullmatch("[0-9a-f]{64}", explicit):
        raise ValueError("explicit policy digest or validated policy snapshot required")
    return explicit


def _check_repo(args: argparse.Namespace) -> None:
    if parse_pr_identity(args.pr)[0] != args.repo:
        raise ValueError("--repo and --pr repository mismatch")


def cmd_prepare(args: argparse.Namespace) -> int:
    store = Store(args.store)
    body, extra = _load_payload(args.payload_file, args.body, expected_head=args.head)
    policy_digest = _resolve_policy(args)
    intent, full_payload = compute_advisory_intent(
        repo=args.repo,
        pr=args.pr,
        head=args.head,
        base=args.base,
        policy_version=policy_digest,
        publisher=args.publisher,
        body=body,
        validated_extra=extra,
    )
    _check_repo(args)
    validate_payload(args.pr, intent, full_payload)
    store.put_once("advisory_payload", intent, full_payload)

    if args.output_intent:
        Path(args.output_intent).write_text(intent, encoding="utf-8")

    owner = getattr(args, "target_owner", None) or getattr(args, "owner", None)
    epoch = getattr(args, "epoch", None)
    if not owner or epoch is None:
        ownership = store.ownership("pr:" + args.pr)
        if ownership:
            owner = owner or ownership[0]
            epoch = epoch if epoch is not None else ownership[1]
        else:
            owner = owner or "corral"
            epoch = epoch if epoch is not None else 1

    canonical_wire_hash, wire_payload = compute_canonical_wire_hash(
        args.head, body, full_payload.get("comments")
    )

    proposal = {
        "status": "prepared",
        "authorized": False,
        "repo": args.repo,
        "pr": args.pr,
        "head": args.head,
        "base": args.base,
        "policy": policy_digest,
        "publisher": args.publisher,
        "owner": owner,
        "epoch": epoch,
        "intent": intent,
        "canonical_wire_hash": canonical_wire_hash,
        "wire_payload": wire_payload,
        "payload": full_payload,
    }
    if getattr(args, "expected_prior_owner", None):
        proposal["expected_prior_owner"] = args.expected_prior_owner
    if getattr(args, "expected_prior_epoch", None) is not None:
        proposal["expected_prior_epoch"] = args.expected_prior_epoch

    if getattr(args, "output_proposal", None):
        Path(args.output_proposal).write_text(json.dumps(proposal, indent=2), encoding="utf-8")

    print(json.dumps(proposal, indent=2))
    return 0


def cmd_import_approved(args: argparse.Namespace) -> int:
    store = Store(args.store)
    p = Path(args.artifact).resolve()
    if not p.exists():
        raise FileNotFoundError(f"Approved artifact not found: {p}")
    artifact = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(artifact, dict):
        raise ValueError("Approved artifact must be a JSON dictionary")

    if artifact.get("authorized") is not True:
        raise PermissionError("Artifact is not authorized (authorized: true required)")

    for field in ("repo", "pr", "head", "base", "policy", "publisher", "intent", "epoch", "canonical_wire_hash", "authorized_by"):
        if field not in artifact or artifact[field] is None:
            raise ValueError(f"Approved artifact missing required field: '{field}'")

    epoch = artifact["epoch"]
    if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch <= 0:
        raise ValueError(f"Epoch must be a positive integer, got: {epoch!r}")

    expected_owner = artifact.get("owner") or "corral"
    ownership = store.ownership("pr:" + artifact["pr"])
    if not ownership or ownership[0] != expected_owner or ownership[1] != epoch or ownership[2] != "active":
        raise PermissionError(
            f"Ownership fence mismatch for '{artifact['pr']}': "
            f"expected ('{expected_owner}', {epoch}, 'active'), got {ownership}"
        )

    payload = artifact.get("payload") or artifact.get("full_payload") or {}
    computed_wire_hash, expected_wire = validate_payload(artifact["pr"], artifact["intent"], payload)
    for field in ("repo", "pr", "head", "base", "policy", "publisher"):
        if artifact[field] != payload[field]:
            raise PermissionError("approved artifact top-level and full payload differ")
    if not isinstance(artifact["authorized_by"], str) or not artifact["authorized_by"].strip():
        raise PermissionError("explicit approval provenance required")
    if artifact["canonical_wire_hash"] != computed_wire_hash:
        raise PermissionError("canonical wire hash mismatch")
    if "wire_payload" in artifact and artifact["wire_payload"] != expected_wire:
        raise PermissionError("wire payload mismatch")

    store.put_once("advisory_payload", artifact["intent"], payload)
    approval = record_advisory_approval(
        store,
        repo=artifact["repo"],
        pr=artifact["pr"],
        head=artifact["head"],
        base=artifact["base"],
        policy=artifact["policy"],
        intent=artifact["intent"],
        epoch=epoch,
        canonical_wire_hash=computed_wire_hash,
        authorized_by=artifact["authorized_by"],
        publisher=artifact["publisher"],
    )
    print(json.dumps({"status": "imported", "intent": artifact["intent"], "approval": approval}, indent=2))
    return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    _check_repo(args)
    store = Store(args.store)
    payload = store.get("advisory_payload", args.intent)
    if not payload:
        print(json.dumps({"status": "error", "reason": f"missing stored payload for intent {args.intent}"}), file=sys.stderr)
        return 1
    token = _get_auth_token()
    transport = GitHubAdvisoryTransport(
        store=store,
        bridge_actor=args.bridge_actor,
        bridge_token=token,
        authorized_bridge_actors=frozenset({args.bridge_actor}),
        allow_network=args.allow_network,
        policy_inputs=load_policy_inputs(getattr(args, "policy_inputs", None)),
    )
    receipt = transport.reconcile(args.pr, args.intent, payload, is_stale=args.is_stale)
    print(json.dumps(receipt, indent=2))
    return 0 if receipt.get("reconciled") else 2


def cmd_publish(args: argparse.Namespace) -> int:
    _check_repo(args)
    store = Store(args.store)
    payload = store.get("advisory_payload", args.intent)
    if not payload:
        print(json.dumps({"status": "error", "reason": f"missing stored payload for intent {args.intent}"}), file=sys.stderr)
        return 1

    approval = store.get("advisory_approval", args.intent)
    ownership = store.ownership("pr:" + args.pr)
    ready = bool(
        approval and approval.get("authorized")
        and ownership and ownership[0] == "corral" and ownership[2] == "active"
    )

    if not args.allow_network:
        report = {
            "status": "local_offline_verification",
            "ready_for_publication": False,
            "local_approval_and_owner_present": ready,
            "pr": args.pr,
            "intent": args.intent,
            "ownership": ownership,
            "approval_present": bool(approval),
            "network_prohibited": True,
            "notice": "live GitHub POST prohibited in local sandbox; authorized deployment required",
        }
        print(json.dumps(report, indent=2))
        return 0 if ready else 1

    if not ready:
        print(json.dumps({"status": "error", "reason": "unauthorized or unowned PR"}, indent=2), file=sys.stderr)
        return 1

    token = _get_auth_token()
    transport = GitHubAdvisoryTransport(
        store=store,
        bridge_actor=args.bridge_actor,
        bridge_token=token,
        authorized_bridge_actors=frozenset({args.bridge_actor}),
        allow_network=True,
        policy_inputs=load_policy_inputs(getattr(args, "policy_inputs", None)),
    )
    receipt = transport.advisory(args.pr, args.candidate, args.intent, payload)
    print(json.dumps(receipt, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Corral GitHub Advisory CLI")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # prepare
    p_prep = subparsers.add_parser("prepare", help="Prepare advisory intent and proposal artifact")
    p_prep.add_argument("--repo", required=True)
    p_prep.add_argument("--pr", required=True)
    p_prep.add_argument("--head", required=True)
    p_prep.add_argument("--base", required=True)
    p_prep.add_argument("--policy", default=None, help="Policy version or snapshot digest")
    p_prep.add_argument("--policy-snapshot", type=Path, help="Policy snapshot file")
    p_prep.add_argument("--publisher", required=True)
    p_prep.add_argument("--body", help="Inline review body")
    p_prep.add_argument("--payload-file", type=Path, help="JSON file containing prepared review payload")
    p_prep.add_argument("--store", type=Path, required=True, help="Corral SQLite store path")
    p_prep.add_argument("--output-intent", type=Path, help="Path to write intent ID")
    p_prep.add_argument("--output-proposal", type=Path, help="Path to write proposed approval JSON")
    p_prep.add_argument("--owner", default=None, help="Target owner (e.g. corral)")
    p_prep.add_argument("--target-owner", dest="target_owner", default=None, help="Target owner alias")
    p_prep.add_argument("--epoch", type=int, default=None, help="Active or target epoch")
    p_prep.add_argument("--expected-prior-owner", default=None, help="Expected prior owner")
    p_prep.add_argument("--expected-prior-epoch", type=int, default=None, help="Expected prior epoch")
    p_prep.set_defaults(func=cmd_prepare)

    # import-approved
    p_imp = subparsers.add_parser("import-approved", help="Import explicit user-approved advisory artifact")
    p_imp.add_argument("--artifact", type=Path, required=True, help="User-approved artifact JSON")
    p_imp.add_argument("--store", type=Path, required=True, help="Corral SQLite store path")
    p_imp.set_defaults(func=cmd_import_approved)

    # reconcile
    p_rec = subparsers.add_parser("reconcile", help="Reconcile uncertain or ambiguous advisory delivery")
    p_rec.add_argument("--repo", required=True)
    p_rec.add_argument("--pr", required=True)
    p_rec.add_argument("--intent", required=True)
    p_rec.add_argument("--store", type=Path, required=True)
    p_rec.add_argument("--bridge-actor", default="github-actions[bot]")
    p_rec.add_argument("--allow-network", action="store_true", default=False)
    p_rec.add_argument("--is-stale", action="store_true", default=False)
    p_rec.add_argument("--policy-inputs", type=Path, help="Repository policy source and runner declarations")
    p_rec.set_defaults(func=cmd_reconcile)

    # publish
    p_pub = subparsers.add_parser("publish", help="Publish advisory review under active Corral ownership")
    p_pub.add_argument("--repo", required=True)
    p_pub.add_argument("--pr", required=True)
    p_pub.add_argument("--intent", required=True)
    p_pub.add_argument("--candidate", required=True)
    p_pub.add_argument("--store", type=Path, required=True)
    p_pub.add_argument("--bridge-actor", default="github-actions[bot]")
    p_pub.add_argument("--allow-network", action="store_true", default=False)
    p_pub.add_argument("--policy-inputs", type=Path, help="Repository policy source and runner declarations")
    p_pub.set_defaults(func=cmd_publish)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
