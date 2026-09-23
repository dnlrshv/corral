"""Durable native events. Missing or conflicting counters remain unknown.

Counter provenance is never merged blindly: ``native-measured`` events are counters a real
harness reported, ``synthetic-fixture`` events come from an explicitly synthetic offline
harness, and ``estimated`` events are Corral's own character/token estimates. Estimates are
kept in their own bucket and are never summed into measured counters, because an estimate
that undercounts would otherwise silently rewrite a provider-reported session total.
"""
from .store import Store, digest

#: Recognized provenance classes for a usage event. Anything else is rejected as unknown.
MEASURED_ORIGIN = "native-measured"
SYNTHETIC_ORIGIN = "synthetic-fixture"
ESTIMATED_ORIGIN = "estimated"
UNATTRIBUTED_ORIGIN = "unattributed"
RECOGNIZED_ORIGINS = (MEASURED_ORIGIN, SYNTHETIC_ORIGIN, ESTIMATED_ORIGIN, UNATTRIBUTED_ORIGIN)


class Spool:
    def __init__(self, path):
        self.store = Store(path)

    def append(self, event):
        # Immutable duplicates are harmless. Conflicting payloads are preserved.
        key = digest([event["invocation"], event.get("scope"), event["id"]])
        try:
            self.store.put_once("event", key, event)
        except ValueError:
            self.store.put_once("conflict", digest(event), event)

    def flush(self, target):
        for key, value in self.store.records("event").items():
            try:
                target.put_once("usage", key, value)
            except ValueError:
                target.put_once("usage_conflict", digest(value), value)
        for key, value in self.store.records("conflict").items():
            target.put_once("usage_conflict", key, value)


def event_origin(event) -> str:
    """Return the declared provenance of one event, without inferring it from success."""
    origin = event.get("origin")
    return UNATTRIBUTED_ORIGIN if origin is None else str(origin)


def summarize(events, conflicts=(), registered=()):
    groups, unknown = {}, []
    buckets = {name: {} for name in RECOGNIZED_ORIGINS}
    for event in events:
        invocation = event["invocation"]
        if (not isinstance(event.get("scope"), str) or not isinstance(event.get("epoch"), int)
                or isinstance(event.get("epoch"), bool) or event["epoch"] < 0
                or not isinstance(event.get("sequence"), int) or isinstance(event.get("sequence"), bool)
                or event["sequence"] < 1 or not isinstance(event.get("counters", {}), dict)):
            unknown.append({"invocation": invocation, "reason": "invalid native counter metadata"})
            continue
        key = (invocation, event.get("scope"), event.get("epoch"))
        groups.setdefault(key, []).append(event)
    for (invocation, scope, epoch), rows in groups.items():
        if scope is None or epoch is None:
            unknown.append({"invocation": invocation, "reason": "missing counter scope/epoch"})
            continue
        by_sequence = {}
        collision = False
        for row in rows:
            sequence = row.get("sequence")
            body = {k: v for k, v in row.items() if k != "id"}
            if sequence in by_sequence and by_sequence[sequence] != body:
                collision = True
            by_sequence[sequence] = body
        if collision or None in by_sequence:
            unknown.append({"invocation": invocation, "reason": "conflicting/missing sequence"})
            continue
        rows = list(by_sequence.values())
        modes = {r.get("mode") for r in rows}
        if len(modes) != 1 or not modes <= {"delta", "cumulative"}:
            unknown.append({"invocation": invocation, "reason": "ambiguous counter mode"})
            continue
        origins = {event_origin(row) for row in rows}
        if len(origins) != 1:
            unknown.append({"invocation": invocation, "reason": "mixed usage origins in one counter group",
                            "origins": sorted(origins)})
            continue
        origin = origins.pop()
        if origin not in RECOGNIZED_ORIGINS:
            unknown.append({"invocation": invocation, "reason": "unrecognized usage origin",
                            "origin": origin})
            continue
        if "delta" in modes and (min(by_sequence) != 1 or len(by_sequence) != max(by_sequence)):
            unknown.append({"invocation": invocation, "reason": "delta sequence gap"})
        if not any(row.get("counters") for row in rows):
            unknown.append({"invocation": invocation, "reason": "missing native counters"})
        for metric in {k for r in rows for k in r.get("counters", {})}:
            samples = [(r.get("sequence"), r.get("counters", {}).get(metric)) for r in rows]
            values = [v for _, v in samples]
            if any(not isinstance(v, int) or isinstance(v, bool) or v < 0 for v in values):
                unknown.append({"invocation": invocation, "metric": metric, "reason": "missing/invalid field"})
                continue
            if "cumulative" in modes:
                if any(s is None for s, _ in samples) or len({s for s, _ in samples}) != len(samples):
                    unknown.append({"invocation": invocation, "metric": metric, "reason": "ambiguous sequence"})
                    continue
                ordered = [v for _, v in sorted(samples)]
                if ordered != sorted(ordered):
                    unknown.append({"invocation": invocation, "metric": metric, "reason": "unmarked counter reset"})
                    continue
                value = ordered[-1]
            else:
                value = sum(values)
            bucket = buckets[origin]
            bucket[metric] = bucket.get(metric, 0) + value
    totals = dict(buckets[MEASURED_ORIGIN])
    for name in (SYNTHETIC_ORIGIN, UNATTRIBUTED_ORIGIN):
        for metric, value in buckets[name].items():
            totals[metric] = totals.get(metric, 0) + value
    seen = {e["invocation"] for e in events}
    for invocation in set(registered) - seen:
        unknown.append({"invocation": invocation, "reason": "no native events"})
    if conflicts:
        unknown.append({"reason": "conflicting event identities", "count": len(conflicts)})
    # Fields remain separate: cache/reasoning may be subsets of input/output, and an
    # estimate is never added to a measured counter.
    return {"observed_fields": totals,
            "measured_fields": dict(buckets[MEASURED_ORIGIN]),
            "synthetic_fields": dict(buckets[SYNTHETIC_ORIGIN]),
            "estimated_fields": dict(buckets[ESTIMATED_ORIGIN]),
            "unattributed_fields": dict(buckets[UNATTRIBUTED_ORIGIN]),
            "origin_breakdown": {name: sum(1 for event in events if event_origin(event) == name)
                                 for name in RECOGNIZED_ORIGINS},
            "unknown": unknown,
            "coverage": "partial" if unknown else "reported-events-only",
            "account_total": None, "desktop_root": "uncovered",
            "combined_tokens": None}
