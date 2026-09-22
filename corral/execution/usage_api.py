"""Idempotent provider-separated usage ingest and reporting."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterable

from .store import Store, digest
from .usage import summarize

DIMENSIONS = ("provider", "model", "session_id", "role")


def _normalized(event: dict[str, Any]) -> dict[str, Any]:
    value = dict(event)
    for key in ("id", "invocation", "scope", "provider", "role"):
        if not isinstance(value.get(key), str) or not value[key].strip():
            raise ValueError(f"usage event requires non-empty {key}")
    if value.get("model") is not None and not isinstance(value["model"], str):
        raise ValueError("usage model must be a string or null")
    if value.get("session_id") is not None and not isinstance(value["session_id"], str):
        raise ValueError("usage session_id must be a string or null")
    if value.get("mode") not in ("delta", "cumulative"):
        raise ValueError("usage mode must be delta or cumulative")
    for key in ("epoch", "sequence"):
        if not isinstance(value.get(key), int) or isinstance(value[key], bool):
            raise ValueError(f"usage {key} must be an integer")
    if value["epoch"] < 0 or value["sequence"] < 1:
        raise ValueError("usage epoch/sequence out of range")
    counters = value.get("counters")
    if not isinstance(counters, dict):
        raise ValueError("usage counters must be an object")
    if any(not isinstance(v, int) or isinstance(v, bool) or v < 0 for v in counters.values()):
        raise ValueError("usage counters must be non-negative integers")
    value.setdefault("origin", "unattributed")
    value.setdefault("model", None)
    value.setdefault("session_id", None)
    return value


def ingest(store: Store, events: Iterable[dict[str, Any]]) -> dict[str, int]:
    accepted = duplicates = conflicts = 0
    for raw in events:
        event = _normalized(raw)
        key = digest([event["invocation"], event["scope"], event["id"]])
        try:
            if store.put_once("usage", key, event):
                accepted += 1
            else:
                duplicates += 1
        except ValueError:
            conflict_key = digest({"identity": key, "event": event})
            store.put_once("usage_conflict", conflict_key, event)
            conflicts += 1
    return {"accepted": accepted, "duplicates": duplicates, "conflicts": conflicts}


def ingest_jsonl(store: Store, path: str | Path) -> dict[str, int]:
    events = []
    for line_number, line in enumerate(Path(path).read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid usage JSON on line {line_number}") from exc
    return ingest(store, events)


def report(store: Store, **filters: str | None) -> dict[str, Any]:
    events = list(store.records("usage").values())
    conflicts = list(store.records("usage_conflict").values())
    for key, expected in filters.items():
        if expected is not None:
            events = [event for event in events if event.get(key) == expected]
    grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for event in events:
        grouped.setdefault(tuple(event.get(key) for key in DIMENSIONS), []).append(event)
    rows = []
    for dimensions, group in sorted(grouped.items(), key=lambda item: str(item[0])):
        summary = summarize(group)
        rows.append({**dict(zip(DIMENSIONS, dimensions)), **summary})
    incomplete_dimensions = any(row.get(key) is None for row in rows
                                for key in ("provider", "model", "session_id"))
    partial = bool(conflicts or incomplete_dimensions or
                   any(row.get("coverage") == "partial" for row in rows))
    return {"groups": rows, "event_count": len(events), "conflict_count": len(conflicts),
            "filters": filters, "coverage": "partial" if partial else "reported-events-only",
            "root_usage": None, "account_total": None,
            "notes": ["unknown fields are null, never inferred as zero",
                      "cumulative snapshots use the latest monotonic sequence",
                      "cache and reasoning counters remain separate"],
            "complete_dimensions": not incomplete_dimensions}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True, type=Path,
                        help="Existing controller state directory")
    sub = parser.add_subparsers(dest="command", required=True)
    ingest_parser = sub.add_parser("ingest")
    ingest_parser.add_argument("--jsonl", required=True, type=Path)
    report_parser = sub.add_parser("report")
    for name in DIMENSIONS:
        report_parser.add_argument("--" + name.replace("_", "-"))
    args = parser.parse_args(argv)
    store = Store(args.state / "controller.sqlite")
    if args.command == "ingest":
        result = ingest_jsonl(store, args.jsonl)
    else:
        result = report(store, **{name: getattr(args, name) for name in DIMENSIONS})
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
