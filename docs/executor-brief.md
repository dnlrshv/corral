# Executor briefs

An executor brief is the task contract for a managed native coding agent.
It supplies context, verifiable acceptance criteria, reasoned constraints and
scope. The preflight brief produced by `corral preflight` is a separate artifact:
it gathers repository context before a task and does not use this contract.

## Write and submit a contract

Use a JSON file with `schema: "corral-executor-brief-v1"`. Required fields are
`goal`, `why`, `sources`, `acceptance`, `commands`, `constraints`, `scope` and
`on_ambiguity`. Optional `links` attach issue or document references; optional
`result.schema` names `corral-executor-result-v1`.

List sources in priority order, because the earlier entry resolves a conflict;
within one source the newest decision takes precedence. Source roles are
`authoritative` (default) or `reference`. Give each acceptance criterion an id
matching `AC[0-9]{1,3}` (for example `AC1`), text, a command id in `verify`,
and an optional test path or id.
A criterion needs a declared verifying command so the executor can demonstrate
it. Command and criterion ids are unique to keep evidence unambiguous.

Command ids match `[a-z0-9][a-z0-9._-]*`; criteria refer to those ids.
Commands contain an id and argv list, with optional `cwd` (default `.`), `where`
(default `workspace`), `env` (variable names, without values) and `evidence`.
The controller does not execute these commands. They tell the executor what
checks demonstrate the task; the controller's verifier remains the authority
for acceptance. Report command, exit code and output tail for a run, because
syntax checks alone do not demonstrate tested behavior.

Every constraint has text and a reason, with an optional source. Scope has
`change`, `read_only` and `out` lists. Sources, change/read-only paths and command
working directories are relative normalized paths without parent references,
so they refer to the prepared checkout. `out` is descriptive text. The only
ambiguity policy is `stop-and-report`: conflicts, missing verification resources
and necessary scope changes belong in open questions or blockers so the
orchestrator can resolve them. Unknown fields are errors to expose typos.
Credential-shaped text is refused because briefs become worker-readable input.

The following example is illustrative; replace its paths, commands and criteria
with those of the repository being changed.

```json
{
  "schema": "corral-executor-brief-v1",
  "goal": "Normalize CSV header whitespace while preserving column order.",
  "why": "Exported files contain padded headers; lookups need stable names without moving data columns.",
  "links": [
    "docs/import-format.md#headers"
  ],
  "sources": [
    {
      "path": "docs/import-format.md",
      "role": "authoritative",
      "note": "The header section defines normalization."
    },
    {
      "path": "src/csv_rows.py",
      "role": "reference"
    }
  ],
  "acceptance": [
    {
      "id": "AC1",
      "text": "Leading and trailing spaces are removed from header names, preserving column order.",
      "verify": "unit",
      "test": "tests/test_csv_rows.py::test_padded_headers"
    },
    {
      "id": "AC2",
      "text": "Two names that become equal after stripping raise ValueError.",
      "verify": "unit",
      "test": "tests/test_csv_rows.py::test_duplicate_headers"
    }
  ],
  "commands": [
    {
      "id": "unit",
      "argv": [
        "python",
        "-m",
        "pytest",
        "tests/test_csv_rows.py",
        "-q"
      ],
      "cwd": ".",
      "where": "workspace",
      "evidence": "Both header tests pass; include their names and the output tail."
    }
  ],
  "constraints": [
    {
      "text": "Preserve header case.",
      "reason": "Existing imports distinguish case-sensitive field names."
    }
  ],
  "scope": {
    "change": [
      "src/csv_rows.py",
      "tests/test_csv_rows.py"
    ],
    "read_only": [
      "docs/import-format.md"
    ],
    "out": [
      "CSV value conversion"
    ]
  },
  "on_ambiguity": "stop-and-report",
  "result": {
    "schema": "corral-executor-result-v1"
  }
}
```

```bash
corral executor-brief check task.json
corral executor-brief render task.json --root . --harness codex
corral-agent --config client.json submit --repository example --brief task.json
corral-service --config service.json submit --event-id task-1 --repository example --brief task.json
```

`--objective` is optional with `--brief`: the service derives it from `goal`.
If both are supplied, their stripped text agrees so the task has one objective.
The Python client accepts `CorralAgent.submit(..., brief=...)`, and JSON submit
requests carry `brief`. Steering and continuation accept brief amendments;
`corral-agent amend` and `continue` read `--brief FILE` too. An amendment with a
brief derives the new objective the same way. A continuation still creates a
fresh generation; it does not resume a harness session or grant new authority.

## Rendered prompt and output

The fixed prompt order is goal, why, prioritized sources, numbered criteria,
verification commands, constraints, scope, ambiguity handling and result.
Continuation context and compact amendment history follow when present.
Brief constraints appear first, then applicable [rules](rules.md), each next to
its reason. The controller renders once; the trusted adapter writes those bytes
verbatim. User text and placeholder-shaped text are preserved.

`corral executor-brief render FILE` defaults to tier `standard`, phase
`implementation`, no harness filter, and harness-flag schema delivery. Use
`--root` to select repository rules, `--tier`, `--phase` or `--harness` to preview
applicability, and `--inline-schema` to preview a route without a schema flag.

For dispatch, a route containing `{schema_file}` delivers the schema through
its harness flag. The prompt describes that mechanism without embedding local
paths. Other routes get an inline schema and a request for a JSON last message.
The portable default `corral-executor-result-v1` asks for status (`done`,
`partial`, `blocked`), summary, criterion status/evidence, commands run with exit
codes and output tails, files changed, open questions and blockers. The result
names its contract in `schema`. Each acceptance entry includes an id, status
(`met`, `not-met`, `not-verified`), command id and evidence; `commands_run` records
`id`, `argv`, `cwd`, `exit_code` and `output_tail`. Codex gets a variant with
every object closed for strict schema support. A task's own
`result_schema` still overrides the default. Assessment reports shape errors,
missing and unknown criteria, and run/file counts; it does not gate acceptance,
because executor self-reports remain claims rather than independent evidence.

## Repository policy, tiers and personal rules

```yaml
executor_brief:
  rules_file: null
  legacy_objective: warn
  max_prompt_chars: null
```

All three keys are optional. `rules_file: null` omits the repository layer.
To add repository rules, declare a relative file such as `agent_rules.yaml`;
its schema is documented in [rules](rules.md).
Objective-only native tasks continue with one recorded warning by default;
`legacy_objective: refuse` requires an executor brief before launch. This allows
a repository to adopt contracts gradually. Inspection-only routes and
deterministic commands skip this prompt policy because they use different
execution contracts.

Profiles have a `tier`: `small`, `standard` (default for configured profiles),
or `frontier`. The shipped Gemini Flash profiles use `small`; the shipped Qwen
profile uses `standard`. Default input budgets are 40,000, 80,000 and 120,000
characters respectively. A positive `max_prompt_chars` overrides the tier
budget. The bound protects the input context window; it does not cap output.
Tier also filters rule applicability; task sections and wording are the same
for every tier.
Oversized prompts are refused before launch with the size, budget and tier.

The controller reads only `corral.yaml` directly in the dispatch workspace;
it does not search parents, because a parent repository has a different policy.
Malformed configuration in any section refuses launch. Repository rules resolve
inside that workspace without symlinks or escapes. A previous generation can
edit repository policy and rules, which have the same trust level as other
repository files; records expose their selected contents and digests. They grant
no new paths, credentials or verifier authority.

Rules merge packaged → repository → personal overlay. The overlay location is
`CORRAL_OVERLAY_FILE`, otherwise `$XDG_CONFIG_HOME/corral/overlay.yaml`, otherwise
`~/.config/corral/overlay.yaml`. It stays outside repository workspaces so
personal content cannot become shared repository defaults. A missing overlay is
normal; malformed overlays are refused. `corral-service validate` reports a
missing overlay as a warning and malformed or workspace-contained overlays as
errors. For a launchd-managed service, set `CORRAL_OVERLAY_FILE` or
`XDG_CONFIG_HOME` in the plist's `EnvironmentVariables`, because launchd does not
inherit an interactive shell's environment.

## Records and migration

Before launching a native coding attempt, the controller writes `brief.json`.
It records the effective brief and digest, applied rules and merged digest,
layer paths and file hashes, policy warnings, tier, delivery mode, config source,
prompt size and SHA-256. `context.json` carries the brief, rules, rendered prompt
and result schema. The terminal result's `brief` block includes the same digest
references and informational assessment; status exposes it and `brief_mode`.
Equal render inputs produce identical prompt bytes. Each continuation gets its
own artifacts and leaves earlier prompts unchanged. Policy refusals record a
redacted reason in task state. After a pre-launch refusal, fix the input or
policy and use `corral-agent continue ... --brief task.json` to schedule a fresh
generation with the corrected contract.

Existing objective-only submissions continue under `warn`. Adopt `--brief`,
review the preview, then choose `refuse` per repository when ready. Previously
copied Codex routes can add `"--output-schema", "{schema_file}"` alongside
`--output-last-message`; the maintained template already includes it. Routes
without the flag still receive the inline contract and informational assessment.
Protocol and store schema integers remain 1. Version numbers are changed by the
release process, not by contract adoption.
