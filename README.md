# corral

**corral is repository infrastructure for teams operating fleets of coding agents on a shared codebase.** It gives Claude, Codex, Qwen, and other agents a common map of the code, explicit high-risk surfaces, task briefs, durable gotchas, and an evidence-governed way to improve instructions. It was extracted from a production trading system operated day-to-day by a fleet of Claude/Codex/Qwen agents; the source deployment's telemetry covers 10,000+ agent sessions across 6+ months.

## Why

corral is designed around four mechanisms:

1. **Code map → agents load briefs, not repo dumps.** Deterministic symbol, import, call, SQL, file, configuration, and pipeline-manifest edges form a queryable graph. Impact and lineage queries bound what an agent needs to load for a task.
2. **Surface guardrails → high-risk edits are visible.** A repository-owned `surfaces.yaml` marks sensitive paths and validation obligations. Editor reminders, staged-change hooks, and CI checks flag matching edits at edit, commit, and merge time.
3. **Preflight briefs + gotcha memory → mistakes are made once.** A task brief combines relevant files, surfaces, invariants, tests, and schema-validated gotchas. When model access is unavailable, the command emits a deterministic fallback instead of silently dropping the brief.
4. **Telemetry + retrospective + governance → instructions evolve under an evidence contract.** Weekly telemetry supplies evidence; mining floors and caps bound candidate generation; a provider-distinct verifier challenges drafts; replay checks retrieval behavior; and proposed instruction changes remain human-review-only. During the port, every one of 10 cross-provider adversarial audits found real bugs, 9 classified High. That is the evidence for making multi-model verification part of the retrospective pattern—not an effect-size claim.

## Quickstart

Install the toolkit (PyPI publication is pending; until then install from a
checkout). Add the optional extras when you need graph queries, model-backed
preflight, or JSON Schema validation:

```bash
pip install -e .
# Optional full local toolset:
pip install -e '.[query,preflight,memory]'
```

Create `corral.yaml` at the repository root:

```yaml
codemap:
  output_dir: code_map
  scan_dirs: [src, scripts]
  skip_dirs: [.venv, data, tests/fixtures, .claude/worktrees]

lineage:
  output: code_map/edges.parquet
  pipeline_yaml: config/data_pipeline.yaml
  config_loaders:
    load_app_config: config/app.yaml

hooks:
  surfaces: surfaces.yaml

preflight:
  gotchas: agent_memory/gotchas.json
```

Build and query the map:

```bash
corral codemap build
corral lineage build
corral codemap query impact src/app.py:main
corral preflight --task "Change the retry policy in src/app.py"
```

Declare high-risk surfaces as shown in [`surfaces.example.yaml`](surfaces.example.yaml), then wire the staged checks with pre-commit. Prerequisite: `pip install pre-commit`.

```yaml
# .pre-commit-config.yaml
repos:
  - repo: https://github.com/dnlrshv/corral
    rev: <pin-a-release-tag-or-commit>
    hooks:
      - id: surface-check
      - id: magic-numbers
```

Install and run the hooks:

```bash
pre-commit install
pre-commit run --all-files
```

For Claude Code, merge [`templates/claude-settings.json`](templates/claude-settings.json) into `.claude/settings.json`. Its `PreToolUse` hook runs `corral-surface-reminder` before `Edit` or `Write`, and its `Stop` hook runs fail-soft telemetry capture.

The retrospective loop resolves named model seats from `seats.yaml`. Start with provider-distinct drafter and verifier seats; credentials are named, not embedded:

```yaml
schema_version: 1
seats:
  retro-drafter:
    provider: vendor-a
    model: model-id
    auth_env: CORRAL_DRAFTER_API_KEY
    adapter: anthropic-sdk
  retro-verifier:
    provider: vendor-b
    model: model-id
    auth_env: CORRAL_VERIFIER_API_KEY
    adapter: openai-compatible-endpoint
    options:
      base_url_env: CORRAL_VERIFIER_BASE_URL
      protocol: chat-completions
```

Set `seats_file: seats.yaml`, `retro.drafter_seat`, and `retro.verifier_seats` in `corral.yaml`, then validate availability with `corral retro seats check`. See [`docs/seats.md`](docs/seats.md) for all three adapters and [`docs/retro.md`](docs/retro.md) for the weekly loop.

## Components

| Subpackage | Purpose | CLI |
| --- | --- | --- |
| `corral.codemap` | Build and query symbol/import artifacts and the unified graph. | `corral codemap build`, `corral codemap query …` |
| `corral.lineage` | Extract call, SQL, file, config, and manifest lineage edges. | `corral lineage build` |
| `corral.hooks` | Flag declared surfaces and duplicated configured constants. | `corral hooks surface-check`, `surface-reminder`, `magic-numbers` |
| `corral.preflight` | Render task briefs with a deterministic no-auth fallback. | `corral preflight` |
| `corral.memory` | Validate durable gotcha and refinement registries. | `corral memory validate` |
| `corral.telemetry` | Capture session records, roll up weekly data, and reconstruct CI outcomes. | `corral telemetry capture`, `rollup`, `ci-outcome` |
| `corral.retro` | Mine evidence, draft candidates, verify them, and render weekly summaries. | `corral retro seats check`, `run`, `revert-refinement` |
| `corral.governance` | Enforce the rule/proposal contract, replay retrieval, lint budgets, and report staleness. | `corral governance check`, `replay`, `build-corpus`, `lint-budget`, `staleness` |

## GitHub Actions

Ready-to-adapt workflows live in [`examples/github-actions/`](examples/github-actions/): telemetry rollup, weekly retrospective, deterministic retrieval replay, and the instruction-governance gate.

The governance workflow deliberately uses a **trusted-base gate**. Validator code is policy and a pull request's head is untrusted data, so the workflow installs and launches corral from the base ref. The validator then reads the proposed registry, instruction text, diff, and PR-body contract from head without executing head's validator. Keep that topology intact; [`docs/governance.md`](docs/governance.md) explains the launcher and `PYTHONPATH` hardening.

## Adoption path

1. Start with the code map, lineage builder, and surface hooks. This establishes bounded context loading and visible high-risk edits without model credentials.
2. Add preflight and a small gotcha registry. Exercise the deterministic fallback before enabling model-backed briefs.
3. Add fail-soft telemetry capture and weekly rollups. Treat missing path or CI data as unknown, not failure.
4. Add provider-distinct retrospective seats, retrieval replay, staleness reporting, and the trusted-base governance gate. Keep all proposed instruction merges human-reviewed.

The detailed sequence is in [`docs/adoption.md`](docs/adoption.md); the runnable synthetic tour is in [`examples/demo/WALKTHROUGH.md`](examples/demo/WALKTHROUGH.md).

## Status and roadmap

corral is pre-release (`0.1.0.dev0`) and not yet published. The toolkit entered this documentation batch with 442 tests, with CI on Ubuntu and macOS across Python 3.10 and 3.12.

Near-term roadmap: stabilize configuration and registry schemas, expand portable examples, and harden the adoption path from local hooks through governed weekly retrospectives. The project is designed to improve context discipline and review quality; it does not claim measured token or quality effects.

## License

Apache-2.0. See [`LICENSE`](LICENSE).

### Optional managed execution

`python -m corral.execution.demo /tmp/corral-example` runs one synthetic worker
through explicit profile resolution, durable observation, and an exact-candidate
verifier. `python -m corral.execution.pr_demo /tmp/corral-pr-example` exercises a
fake GitHub lifecycle with a process-backed repair and guarded merge receipt.
No provider calls, service installation, or GitHub writes occur in these demos.

`corral-execution-client --config private-client.json --request request.json`
(or `python -m corral.execution.client`) supports local and authenticated SSH
channels. Requests use `submit`, `steer`, `status`, `dispatch`, `dispatch-wave`,
`continue`, `cancel`, `snapshot`, and `transfer`; worker execution is detached
from the client. Host capacity, enabled routes, profiles and controller paths
are private config. The controller store is the only capacity authority: the
service reserves capacity when it claims an event, the dispatch adopts that
reservation, and a refused dispatch acquires no ownership or capacity.
Input/result manifests bind the Git base and explicitly selected file digests;
changed destinations fail closed. Verifiers run in their own process group under
a wall-clock bound (`verifier_timeout_seconds` per host, default 3600 s); one
that exceeds it is killed and receipted as timed out with exit code 124. Use
isolated repositories for development.

`continue` is the only post-terminal action. It checkpoints a task's terminal
attempt and schedules exactly one later generation under the same task id, with
its own objective, verifier policy, candidate declaration, artifact directory,
worker session and usage attribution. It rebinds nothing else: host, endpoint,
workspace, profile/model/route, role, resources, dependencies, command and
credentials stay bound to the immutable submitted request, and a payload naming
any of them is refused rather than merged. It never dispatches, retries or caps
on its own, a repeated continuation id deduplicates, and an active, unresolved
or cancelled attempt must be reconciled first. `status` then reports the
current result together with the immutable per-generation history and lineage,
so an accepted earlier artifact stays addressable and its receipt is never
re-derived or overwritten.

The execution package defaults to observation, with soft thresholds recorded
without budget admission caps. Model/effort, harness, billing identities and
observations remain separate; unknown telemetry stays unknown. Native provider
routes require explicit controller-owned executable, model, effort, account and
capability declarations. A same-user process is not an OS security boundary.
Validate installed routes and effective permissions before admitting real work;
fixture verdicts do not establish native isolation.

Checkout workspaces require readable Git metadata before inference. An immutable
snapshot instead declares its source revisions and export digest; the controller
checks its candidate bytes without inventing Git history. Cancelling a running
attempt stops its worker's process group and leaves the attempt fenced as
`uncertain`, with its workspace and capacity held, without running the verifier
or recording a result. That attempt, like any whose finalization was
interrupted, is settled through the authenticated client's `reconcile`.
Reconciliation collects process, artifact and delivery observations (a cancelled
attempt without a completed adapter result reports its artifacts as
unavailable), retains the original failure and usage, and releases only the
matching ownership epoch and allocation. An unresolved external effect prevents
settlement.

GitHub advisory publication is a separate trusted transport with candidate-bound
authorization, ownership fencing, authenticated receipt readback and duplicate
prevention. A POST with an unknown outcome stays unresolved until readback
finds the review, or proves it absent after a quiet period and repeated
consistent readbacks of the unchanged candidate; only then is the same intent
sent again. Repository policy sources and runner declarations are explicit
`policy_inputs`; the core has no repository-specific filename or host defaults
for policy capture. Advisory comments confer no merge authority.
