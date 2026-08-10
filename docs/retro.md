# Weekly retrospective loop

The retrospective turns repository evidence into reviewable gotchas and, when enabled, instruction-file proposals. Its sequence is intentionally bounded:

```text
telemetry → evidence groups → mining floors/caps → drafting
          → provider-distinct verification → gotcha registry + summary → human PR
```

Zero candidates is a successful result when no repeated pattern clears the evidence bar. The loop does not loosen a floor to manufacture output. Configure model invocation through [`seats.yaml`](seats.md), and apply instruction proposals under the [governance contract](governance.md).

## 1. Telemetry becomes evidence

The Claude Code `Stop` hook can invoke `corral-telemetry-capture`, which writes a fail-soft session record to the configured spool. The weekly rollup command collects session artifacts and writes `rollup_<week>.parquet` beneath `telemetry.rollup_output_dir`:

```bash
corral telemetry rollup
```

`corral telemetry ci-outcome --pr <number>` reconstructs first-push and final-push required-check outcomes when GitHub exposes them. Missing checks remain unknown rather than being converted to failures.

For defect evidence, the retrospective prefers a committed `fixup_<week>.parquet` matching `retro.fixup_glob` (or the default under the rollup directory). If none is present, it queries merged pull requests for the requested week and derives fix-up pairs. It may also read session learning notes from rollups and explicitly configured file-backed evidence roots under `retro.bridge`. Those roots default to empty; opt in only to understood, sanitized sources.

Self-referential housekeeping is excluded. Configured title patterns prevent the weekly retrospective and rollup PRs from mining themselves, and configured path globs exclude pairs whose only common files are instruction, telemetry, or registry housekeeping.

## 2. Mining applies floors and caps

Evidence is grouped by the repeated mistake pattern and counted by **distinct root incident**, not by raw mentions of the same incident. `retro.evidence.min_root_incidents` is the minimum before a group can reach a model seat. Qualified groups are sorted by evidence count and stable key; only the first `retro.evidence.max_candidates` are drafted.

The cap applies before verification. A refuted candidate does not free a slot for a lower-ranked group, which prevents repeated calls until something passes. Existing gotcha source PRs, file-backed source references, and matching open review issues are deduplicated before drafting.

## 3. A drafter produces a constrained candidate

The configured `retro.drafter_seat` receives compact evidence: paired pull-request excerpts, session learning notes, and sanitized bridge evidence. The output parser requires the gotcha rule, rationale, severity, applicability dimensions, confidence, and source references. Candidates below `retro.confidence_threshold` are recorded as skipped.

`retro.allowed_severities` defines the vocabulary the drafter may use. `retro.severe_severities` is the subset eligible for immediate review-issue handling through `retro.issue_sink`. Keep the subset empty until maintainers agree on the operational meaning of each severity.

A transient drafting failure is retried within the bounded retry policy. Exhausting retries opens a pass-level capacity circuit: later groups are reported as deferred rather than called blindly.

## 4. An independent seat challenges the draft

Each drafted gotcha is sent to verifier seats in configured order. With `require_distinct_provider: true`, a seat carrying the drafter's provider label is ineligible. The verifier must return `CONFIRM` or `REFUTE` with evidence-based reasoning and may sharpen the wording. A malformed response receives one constrained retry.

Only an explicit `REFUTE` rejects a gotcha under the default `proceed-unverified` availability policy. When every verifier is unavailable, the candidate may continue marked `UNVERIFIED`, including the reason and provenance gap. Set `gotcha_unavailable_policy: fail-closed` if that degradation is inappropriate for your repository.

Instruction-file proposals are stricter: `instruction_unavailable_policy` defaults to `fail-closed`. During the port, every one of 10 cross-provider adversarial audits found real bugs, 9 classified High; that is the evidence for including provider-distinct challenge in this design. It is not a measured claim about downstream quality.

## 5. The registry and summary are prepared

Confirmed and policy-permitted unverified gotchas receive sequential IDs and are validated against the bundled gotcha schema before write. `corral retro run` is the single writer of `retro.gotchas_path`. Its optional `--base-ref` plus `--expected-base` pair prevents a write when the shared base moved during the run.

The Markdown summary records:

- requested date window and dry-run/live mode;
- verifier availability;
- total, qualified, deduplicated, skipped, accepted, refuted, and issue-handling outcomes;
- each rule, evidence reference, rationale, severity, and verification provenance;
- every dropped/deferred reason;
- a separate instruction-file proposal section when that pass is enabled.

Gotchas are registry data used by future preflight briefs. Doc/skill proposal edits are never applied by the retrospective: accepted proposals, prospective file text, registry text, and the gate-compatible proposal block are rendered for human review. The weekly workflow opens one PR containing the permitted run artifacts; maintainers decide what merges.

## Configuration walkthrough

Start from this explicit configuration and adjust it to repository policy:

```yaml
seats_file: seats.yaml

telemetry:
  rollup_output_dir: agent_telemetry
  lookback_days: 7
  required_ci_contexts: [lint, test]

retro:
  repository: owner/repository
  drafter_seat: retro-drafter
  verifier_seats: [retro-verifier, local-verifier]
  require_distinct_provider: true
  verification_timeout_s: 300
  gotcha_unavailable_policy: proceed-unverified
  instruction_unavailable_policy: fail-closed

  issue_sink: stdout
  fixup_glob: null
  gotchas_path: agent_memory/gotchas.json
  refinements_path: agent_memory/refinements.jsonl
  confidence_threshold: 0.70
  max_tokens: 900
  drafting_timeout_s: 300
  allowed_severities: [info, P2, P1, P0]
  severe_severities: []

  github:
    assignee: null
    gotcha_label: agent-gotcha
    timeout_s: 30

  evidence:
    min_root_incidents: 2
    max_candidates: 3
    ignored_title_patterns:
      - weekly gotcha retrospective
      - weekly agent rollup
    ignored_path_globs:
      - AGENTS.md
      - README.md
      - agent_telemetry/*
      - agent_memory/gotchas.json

  bridge:
    memory_roots: []
    run_artifact_roots: []

  proposals:
    enabled: false
    max: 3
    min_incidents: 2
    target_globs: []
```

Important controls:

- `repository` has no default. The retrospective stays repository-less until you opt in with `owner/name`.
- `issue_sink` is `stdout`, `github`, or `off`. GitHub writes are additionally suppressed by `--dry-run`.
- `fixup_glob: null` selects the default `fixup_*.parquet` location beneath the telemetry output directory.
- `confidence_threshold`, evidence floors, and caps are selection policy. Do not tune them merely to increase proposal count.
- `proposals.enabled` defaults false. Before enabling it, declare `target_globs`, a governance reviewer, registry paths, manifest/replay inputs, and proposal reviewers. The hard cap for doc/skill proposals is three, on top of the gotcha cap.
- Skill proposals require a stable trigger and repeated completed-task evidence spanning more than one week. A single weekly window cannot establish that history.

See [`corral.example.yaml`](../corral.example.yaml) for every option and [`seats.example.yaml`](../seats.example.yaml) for adapter configuration.

## Dry-run first

Dry-run suppresses registry writes and issue filing, but it deliberately performs real evidence reads, drafting, and verification. Provider credentials and GitHub read access must therefore be available.

```bash
corral retro seats check
corral retro run --dry-run --week 2026-W32
```

Review the printed summary for grouping quality, distinct incident labels, exclusions, verifier provenance, unverified behavior, and proposed applicability. A safe rollout is:

1. Run with proposals disabled, `issue_sink: stdout`, and `--dry-run`.
2. Enable live gotcha-registry writes with the single-writer base guard.
3. Add the scheduled workflow from [`examples/github-actions/retro-weekly.yml`](../examples/github-actions/retro-weekly.yml).
4. Enable instruction proposals only after the [retrieval replay](governance.md#deterministic-retrieval-replay) and trusted-base gate pass in CI.

For adoption from the beginning, return to [`adoption.md`](adoption.md).
