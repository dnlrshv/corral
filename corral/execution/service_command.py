"""Administrative command interface for the maintained Corral service."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .service import Service


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    submit = sub.add_parser("submit")
    for flag in ("event-id", "repository", "objective"):
        submit.add_argument("--" + flag, required=True)
    submit.add_argument("--host")
    submit.add_argument("--mode", default="interactive")
    submit.add_argument("--role")
    submit.add_argument("--profile")
    submit.add_argument("--candidate", action="append", default=[])
    submit.add_argument("--inspection-path", action="append", default=[])
    submit.add_argument("--diff-path")
    submit.add_argument("--workspace-kind", default="checkout")
    submit.add_argument("--snapshot", type=Path)
    review = sub.add_parser("review-pr")
    review.add_argument("--repository", required=True)
    review.add_argument("--pr", required=True, type=int)
    review.add_argument("--policy", required=True)
    review.add_argument("--expected-head")
    review.add_argument("--expected-base")
    review.add_argument("--host")
    for name in ("status", "return"):
        item = sub.add_parser(name)
        item.add_argument("--event-id", required=True)
        if name == "return":
            item.add_argument("--path", required=True)
            item.add_argument("--destination", required=True)
    tick = sub.add_parser("tick")
    tick.add_argument("--now", type=float)
    args = parser.parse_args(argv)
    service = Service(args.config)
    if args.command == "submit":
        snapshot = json.loads(args.snapshot.read_text()) if args.snapshot else None
        result = service.submit(args.event_id, args.repository, args.objective, host=args.host,
                                mode=args.mode, role=args.role, profile_id=args.profile,
                                candidate_paths=args.candidate or None, source_snapshot=snapshot,
                                inspection_paths=args.inspection_path, diff_path=args.diff_path,
                                workspace_kind=args.workspace_kind)
    elif args.command == "review-pr":
        result = service.submit_pr_review(
            args.repository, args.pr, args.policy, expected_head=args.expected_head,
            expected_base=args.expected_base, host=args.host)
    elif args.command == "status":
        result = service.status(args.event_id)
    elif args.command == "tick":
        result = service.tick(args.now)
    else:
        result = {"path": str(service.return_artifact(
            args.event_id, args.path, args.destination))}
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
