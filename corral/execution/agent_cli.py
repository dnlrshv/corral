"""Thin developer CLI for service submission, steering and artifact return."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import time
from pathlib import Path

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
    submit.add_argument("--run", action="store_true")
    status = sub.add_parser("status")
    status.add_argument("--event-id", required=True)
    amend = sub.add_parser("amend")
    amend.add_argument("--event-id", required=True)
    amend.add_argument("--amendment-id", required=True)
    amend.add_argument("--objective", required=True)
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
                             source_snapshot=snapshot)
        if args.run:
            client.call("tick")
            while True:
                result = client.call("status", event_id=event_id)
                status_value = result["event"]["status"]
                if status_value in ("completed", "failed", "blocked", "uncertain",
                                    "refused-before-launch"):
                    break
                client.call("tick")
                time.sleep(0.05)
    elif args.command == "status":
        result = client.call("status", event_id=args.event_id)
    elif args.command == "amend":
        result = client.call("amend", event_id=args.event_id,
                             amendment_id=args.amendment_id, objective=args.objective)
    elif args.command == "tick":
        result = client.call("tick")
    else:
        fetched = client.call("fetch", event_id=args.event_id, relative_path=args.path)
        data = base64.b64decode(fetched["data"], validate=True)
        if hashlib.sha256(data).hexdigest() != fetched["digest"]:
            raise ValueError("artifact digest mismatch")
        args.destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(args.destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                         fetched.get("mode") or 0o644)
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
        except FileExistsError:
            if args.destination.read_bytes() != data:
                raise PermissionError("destination exists with newer or different content")
        result = {"path": str(args.destination)}
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
