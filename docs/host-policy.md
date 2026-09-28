# Host policy

These fields are optional. The maintained service reads `blackout_windows`,
`max_concurrent_seats`, and `github_post_mode` from its service JSON. Each controller
host may set `process_priority` and override `max_concurrent_seats`. A service
repository may override `github_post_mode` and explicitly allow branch pushes.

```json
{
  "blackout_windows": [
    {"days": ["mon", "tue", "wed", "thu", "fri"], "start": "22:00",
     "end": "06:00", "timezone": "Europe/Zurich", "policy": "finish"}
  ],
  "max_concurrent_seats": 2,
  "github_post_mode": "dry-run",
  "repositories": {
    "example": {"github_post_mode": "comment", "allow_repair_push": false}
  }
}
```

`days` names the local day on which a window starts. End before start spans
midnight. Windows are interpreted in their IANA timezone, including skipped and
repeated DST hours. During a window, service ticks may admit schedules and
reconcile running work, but they do not launch prepared service or wave tasks.
`finish` lets running work continue. `hold` is reserved and validation rejects it.
The tick and event status include `blackout.active`, `blackout.window`, and the
UTC ISO `blackout.until` (null while inactive or continuously covered). `--now` on `tick` sets the clock
for blackout and dispatch decisions.

The seat cap counts active Store reservations across repositories and providers
on one host. It is checked in the same transaction as provider concurrency and
capacity. A task waits prepared when the cap is full. A host override replaces
the service cap for that host.

A controller host may declare:

```json
{"process_priority": {"nice": 5, "low_priority_io": true}}
```

`nice` is an integer from 0 to 19. The service wraps launched commands with the
host's `nice` executable. On macOS, low priority I/O wraps them with
`taskpolicy -b`; on Linux it uses `ionice -c3`. This applies to the detached
launcher, seat, and verifier (including reconciliation verification). The
wrapper stays outside the Seatbelt command. `corral-service validate` refuses a
requested priority tool that is absent on the validating host. Run validation
on the execution host before use.

## GitHub writes

`github_post_mode` defaults to `dry-run`. Dry-run saves a `github_dry_run`
Store record keyed by intent with the HTTP method or Git push action, endpoint,
and exact body/ref update, and makes no GitHub write. `comment` permits the
existing advisory `COMMENT` review POST under its candidate, ownership,
approval, and authenticated publisher checks. It does not permit approval or
request-changes reviews. Service repository `allow_repair_push: true` additionally
permits its guarded same-repository repair branch push. It defaults to false.
For coordinator and merge CLI configs, `allow_merge: true` additionally permits
the existing guarded merge PUT after its policy and candidate checks; it
defaults to false. The CLI configs also default to dry-run and accept
`github_post_mode`. The standalone advisory CLI uses `--post-mode comment`
and `--allow-network` together for a live COMMENT review; its default records
a local dry-run artifact. Reconciliation commands remain read-only.

Changing a repository from dry-run to comment does not mark earlier dry-run
records as delivered. They remain reviewable records; a later live invocation
still must pass the existing candidate and approval checks.
