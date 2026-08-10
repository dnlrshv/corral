# Adopt corral in an existing repository

This guide gets an existing repository from a local code map to enforced hooks and CI. Start with deterministic components; add model-backed features only after the repository-owned inputs are useful on their own. For the conceptual overview, see the [README](../README.md). For a command-by-command sample, use the [synthetic walkthrough](../examples/demo/WALKTHROUGH.md).

## 1. Author `corral.yaml`

Place `corral.yaml` at the repository root. Commands search upward from the current directory for this file, and relative paths resolve from its directory.

```yaml
codemap:
  output_dir: code_map
  scan_dirs: [src, scripts]
  skip_dirs: [.venv, data, tests/fixtures, .claude/worktrees]

lineage:
  output: code_map/edges.parquet
  pipeline_yaml: config/data_pipeline.yaml
  yaml_manifest_schema:
    sources: table
    groups: target_table
  config_loaders:
    load_app_config: config/app.yaml
  config_loader_key_prefixes: {}

hooks:
  surfaces: surfaces.yaml
  magic_numbers:
    constants: src/app/constants.py
    allowlist: .magic-number-allowlist.yaml

preflight:
  gotchas: agent_memory/gotchas.json
  output: null

telemetry:
  rollup_output_dir: agent_telemetry
  lookback_days: 7
  required_ci_contexts: [lint, test]
```

Key choices:

- `codemap.scan_dirs` is the context boundary. List source roots rather than scanning vendored or generated trees.
- `codemap.skip_dirs` removes paths beneath any scan root. Keep agent worktrees excluded because they can duplicate the main tree.
- `lineage.pipeline_yaml` is optional in practice: a missing file contributes no manifest edges. `yaml_manifest_schema` maps each manifest section to the table-name key in its entries.
- `lineage.config_loaders` is an allowlist of function name to config path. Only declared loader calls create `reads_config` edges. Use `config_loader_key_prefixes` when a wrapper has already descended into a subtree.
- `hooks.magic_numbers.constants` is optional. If unset, the lint exits cleanly and says membership checks were skipped.
- `preflight.output: null` writes briefs to stdout. Set a repository-relative path only when a shared file is part of your workflow.

Compare the complete annotated defaults in [`corral.example.yaml`](../corral.example.yaml). Then run:

```bash
corral codemap build
corral lineage build
corral codemap query impact src/app.py:main
corral codemap query lineage orders
```

Generated parquet is derived data. Add your configured code-map output directory to `.gitignore` unless the repository intentionally versions it.

## 2. Author `surfaces.yaml`

The top-level `surfaces` mapping is keyed by stable surface ID. The same IDs are used by preflight gotchas, governance selectors, and staleness resolution.

```yaml
surfaces:
  payments-config:
    description: Configuration controlling payment behavior.
    paths:
      - config/payments.yaml
    line_ranges: []
    needs_human: true
    needs_shadow_run: false
    needs_equivalence_check: true
    needs_validation: false
    notes: Confirm the rollout and rollback plan with a maintainer.
    yaml_block_selectors:
      - config/payments.yaml::retry_policy
```

Field reference:

| Field | Meaning |
| --- | --- |
| surface ID | Stable mapping key used by briefs, gotchas, and rule selectors. |
| `description` | Short explanation printed with a hook hit. |
| `paths` | Exact repository-relative files monitored by the staged hook. |
| `line_ranges` | Optional `path:start-end` entries. A matching staged hunk triggers the surface; deletion/binary cases fall back conservatively to the whole file. |
| `needs_human` | Makes a staged hit blocking. `--warn-only` downgrades it for an explicit non-blocking invocation. |
| `needs_shadow_run` | Printed obligation for workflows that need a shadow execution. |
| `needs_equivalence_check` | Printed obligation for old/new behavior comparison. |
| `needs_validation` | Printed obligation for project-specific validation. |
| `notes` | Maintainer guidance printed with the hit. |
| `yaml_block_selectors` | Structured YAML locations carried as surface metadata and reminders. Use `path::key.path` notation consistently in your repository. |

Start with a small set of genuinely high-risk paths. Run `corral hooks surface-check --warn-only` while tuning the registry, then remove `--warn-only` when `needs_human` should block a commit.

## 3. Install local hooks

corral publishes pre-commit hook definitions from [`.pre-commit-hooks.yaml`](../.pre-commit-hooks.yaml):

Prerequisite: `pip install pre-commit`.

```yaml
# .pre-commit-config.yaml
repos:
  - repo: https://github.com/dnlrshv/corral
    rev: <pin-a-release-tag-or-commit>
    hooks:
      - id: surface-check
      - id: magic-numbers
```

```bash
pre-commit install
pre-commit run --all-files
```

`surface-check` reads the staged diff and blocks only matching `needs_human` surfaces. `magic-numbers` compares numeric literals with dataclass-singleton fields in the configured constants module; use named constants, scoped allowlists, or a reasoned inline `# magic-ok` exception.

For Claude Code, merge [`templates/claude-settings.json`](../templates/claude-settings.json) into `.claude/settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Edit|Write",
        "hooks": [{"type": "command", "command": "corral-surface-reminder"}]
      }
    ],
    "Stop": [
      {
        "hooks": [{"type": "command", "command": "corral-telemetry-capture"}]
      }
    ]
  }
}
```

The editor reminder inspects the proposed edit payload before the tool runs. Telemetry capture is fail-soft so an unavailable spool cannot break agent shutdown.

## 4. Wire CI

Copy and adapt the workflows in [`examples/github-actions/`](../examples/github-actions/):

- `governance-gate.yml` runs the instruction gate from trusted base code. Preserve this topology; see the [governance contract](governance.md#trusted-base-gate-topology).
- `replay.yml` checks the reviewed retrieval corpus whenever instruction inputs change.
- `telemetry-rollup.yml` creates weekly rollup artifacts and CI-outcome records.
- `retro-weekly.yml` applies the single-writer base check, runs the retrospective, and opens one human-reviewed PR.

To reproduce the staged surface check in a dedicated, disposable pull-request job, fetch full history and present the base-to-head change as the index before running the hook:

```bash
git reset --soft "origin/${GITHUB_BASE_REF}"
corral hooks surface-check
```

`git reset --soft` changes the CI checkout's `HEAD` and index, so use this only in an isolated job after all commands that depend on the original checkout state. The working tree remains at the pull-request content and the staged diff becomes the proposed change.

Before enabling the weekly loop, configure [`seats.yaml`](seats.md), run `corral retro seats check`, and perform the first retrospective with `corral retro run --dry-run`. The full sequence and its evidence floors are in [`retro.md`](retro.md).

## 5. Validate the adoption

Run the adopter-owned checks from your repository root:

```bash
corral codemap build
corral lineage build
corral preflight --task "Inspect a declared high-risk path"
corral memory validate
```

Until you have authored an adopter-owned instruction registry and reviewed replay corpus,
validate the governance commands against corral's shipped synthetic files. From the root
of a corral source checkout, this literal sequence is runnable as-is:

```bash
corral governance check --root . --config examples/demo/corral.yaml
corral governance replay --root . --config examples/demo/corral.yaml
```

A useful first milestone is deterministic: code map, lineage, surface reminders, staged checks, fallback preflight, and local governance all work without provider credentials. Add telemetry and the retrospective only after those repository contracts are stable.
