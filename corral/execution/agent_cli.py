"""Thin developer CLI for service submission, steering and artifact return."""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from . import artifact_return
from .service_client import from_path
from .workspace import manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    submit = sub.add_parser("submit")
    submit.add_argument("--repository", required=True)
    submit.add_argument("--objective", required=True)
    submit.add_argument("--event-id")
    submit.add_argument("--source-root", type=Path)
    submit.add_argument("--input", action="append", default=[])
    submit.add_argument("--candidate", action="append", default=[])
    submit.add_argument("--host")
    submit.add_argument("--role")
    submit.add_argument("--profile")
    submit.add_argument("--inspection-path", action="append", default=[])
    submit.add_argument("--diff-path")
    submit.add_argument("--workspace-kind", choices=("checkout", "immutable_snapshot"),
                        default="checkout")
    submit.add_argument("--run", action="store_true")
    submit.add_argument("--poll-interval", type=float, default=2.0)
    review = sub.add_parser("review-pr")
    review.add_argument("--repository", required=True)
    review.add_argument("--pr", required=True, type=int)
    review.add_argument("--policy", required=True)
    review.add_argument("--expected-head")
    review.add_argument("--expected-base")
    review.add_argument("--host")
    review.add_argument("--replacement-of-task")
    status = sub.add_parser("status")
    status.add_argument("--event-id", required=True)
    amend = sub.add_parser("amend")
    amend.add_argument("--event-id", required=True)
    amend.add_argument("--amendment-id", required=True)
    amend.add_argument("--objective", required=True)
    pause = sub.add_parser("pause")
    pause.add_argument("--event-id", required=True)
    pause.add_argument("--amendment-id", required=True)
    pause.add_argument("--resume", action="store_true")
    cancel = sub.add_parser("cancel")
    cancel.add_argument("--event-id", required=True)
    reconcile = sub.add_parser("reconcile")
    reconcile.add_argument("--event-id", required=True)
    continued = sub.add_parser("continue")
    continued.add_argument("--event-id", required=True)
    continued.add_argument("--continuation-id", required=True)
    continued.add_argument("--objective", required=True)
    wave = sub.add_parser("wave")
    wave.add_argument("--wave-id", required=True)
    wave.add_argument("--spec", required=True, type=Path)
    sub.add_parser("tick")
    returned = sub.add_parser("return")
    returned.add_argument("--event-id", required=True)
    returned.add_argument("--path", required=True)
    returned.add_argument("--destination", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    client = from_path(args.config)
    if args.command == "submit":
        if args.input and args.source_root is None:
            raise SystemExit("--source-root is required with --input")
        snapshot = manifest(args.source_root.resolve(), args.input) if args.input else None
        event_id = args.event_id or f"interactive:{args.repository}:{time.time_ns()}"
        result = client.call("submit", event_id=event_id, repository=args.repository,
                             objective=args.objective, host=args.host, role=args.role,
                             profile_id=args.profile, candidate_paths=args.candidate or None,
                             source_snapshot=snapshot, inspection_paths=args.inspection_path,
                             diff_path=args.diff_path, workspace_kind=args.workspace_kind)
        if args.run:
            client.call("tick")
            while True:
                result = client.call("status", event_id=event_id)
                status_value = result["event"]["status"]
                if status_value in ("completed", "failed", "blocked", "uncertain",
                                    "refused-before-launch", "cancelled"):
                    break
                client.call("tick")
                if args.poll_interval <= 0:
                    raise ValueError("poll interval must be positive")
                time.sleep(args.poll_interval)
    elif args.command == "review-pr":
        result = client.call("submit-pr-review", repository=args.repository,
                             pr_number=args.pr, policy_id=args.policy,
                             expected_head=args.expected_head, expected_base=args.expected_base,
                             host=args.host, replacement_of_task=args.replacement_of_task)
    elif args.command == "status":
        result = client.call("status", event_id=args.event_id)
    elif args.command == "amend":
        result = client.call("amend", event_id=args.event_id,
                             amendment_id=args.amendment_id, objective=args.objective)
    elif args.command == "pause":
        result = client.call("pause", event_id=args.event_id,
                             amendment_id=args.amendment_id, paused=not args.resume)
    elif args.command == "cancel":
        result = client.call("cancel", event_id=args.event_id)
    elif args.command == "reconcile":
        result = client.call("reconcile", event_id=args.event_id)
    elif args.command == "continue":
        result = client.call("continue", event_id=args.event_id,
                             continuation_id=args.continuation_id, objective=args.objective)
    elif args.command == "wave":
        spec = json.loads(args.spec.read_text())
        result = client.call("wave", wave_id=args.wave_id, tasks=spec["tasks"],
                             handoffs=spec.get("handoffs"))
    elif args.command == "tick":
        result = client.call("tick")
    else:
        fetched = client.call("fetch", event_id=args.event_id, relative_path=args.path)
        artifact_return.write(fetched, args.destination)
        result = {"path": str(args.destination)}
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
