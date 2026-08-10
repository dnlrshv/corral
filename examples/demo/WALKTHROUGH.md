# Synthetic demo walkthrough

This demo is a dependency-free Python package with cross-module calls, a tiny `pipeline.yaml` loader, SQL strings, file I/O, and a pipeline-manifest declaration. It is small enough to inspect directly while exercising the real code-map, lineage, hook, preflight, and governance commands.

Run from `examples/demo/`. The captured session below used the repository virtual environment:

```bash
cd examples/demo
export PATH="../../.venv/bin:$PATH"
```

With corral installed normally, no `PATH` adjustment is needed. For configuration details, see the [adoption guide](../../docs/adoption.md); for the policy commands, see [governance](../../docs/governance.md).

## 1. Build the structural map

```console
$ corral codemap build
```

The command produced no stdout and exited 0. It created `code_map/imports.parquet` and `code_map/symbols.parquet`.

`code_map/` is generated data. [`../../.gitignore`](../../.gitignore) contains `examples/demo/code_map/`, so none of the generated parquet or cache artifacts should be committed.

## 2. Build lineage

```console
$ corral lineage build
```

This also produced no stdout and exited 0. It created `code_map/edges.parquet` from calls, the SQL strings, fixed file-I/O paths, `load_pipeline_config()` accesses, and the `groups` entry in `pipeline.yaml`.

## 3. Query impact and a cross-module path

The config-key impact query resolves the configured loader call and then walks reverse call dependencies:

```console
$ corral codemap query impact pipeline.yaml::minimum_total
=== impact: pipeline.yaml::minimum_total ===
Blast radius: 3 dependent(s)
  acme_pipeline/pipeline.py:run_pipeline
  acme_pipeline/settings.py:minimum_total
  acme_pipeline/transform.py:build_statements
```

The path query connects the entry point to a table through two project-local calls and a SQL read:

```console
$ corral codemap query path acme_pipeline/pipeline.py:run_pipeline curated_orders
=== path: acme_pipeline/pipeline.py:run_pipeline → curated_orders ===
Path length: 3 hop(s)
  acme_pipeline/pipeline.py:run_pipeline  [calls]
  → acme_pipeline/transform.py:build_statements  [calls]
  → acme_pipeline/queries.py:active_customer_query  [reads_table]
  → curated_orders
```

That second query exposed a real package integration defect during this documentation batch: `extract_calls.py` existed, but the lineage builder did not invoke it. The minimal fix wires the existing call extractor into `corral.lineage.build`; the output above is from the corrected real CLI.

## 4. Run a repository hook

This demo intentionally does not configure a project constants module, so the membership lint reports the skipped check and succeeds:

```console
$ corral hooks magic-numbers
Magic-number lint: no constants module configured; constants-membership checks skipped.
```

`surfaces.yaml` separately declares the SQL module as human-reviewed and `pipeline.yaml` as a shadow-run/validation surface. The staged hook acts on Git's index, so it is best exercised in a disposable branch after staging a matching edit.

## 5. Render a no-auth preflight brief

The captured command explicitly removed all three supported preflight credential variables. The warning detail can differ when the optional SDK is absent, but the fallback brief fields are deterministic for the same task, surfaces, and Git revision.

```console
$ env -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN -u CLAUDE_CODE_OAUTH_TOKEN \
    corral preflight --task \
    'Change acme_pipeline/queries.py to add a curated-orders status filter'
::warning::Preflight LLM failed; using fallback: TypeError: "Could not resolve authentication method. Expected one of api_key, auth_token, or credentials to be set. Or for one of the `X-Api-Key` or `Authorization` headers to be explicitly omitted"
# preflight_fingerprint: f81602346ef0
agent_gotchas: []
cross_cutting_concerns:
- Preflight LLM was unavailable; using deterministic code-map fallback.
do_not_touch:
- acme_pipeline/queries.py
estimated_blast_radius: medium
fallback_reason: preflight_llm_unavailable
files_to_read_only:
- acme_pipeline/queries.py
files_to_touch: []
invariants_to_preserve:
- Preserve high-risk surface controls and production safety rules.
preflight_error: 'TypeError: "Could not resolve authentication method. Expected one
  of api_key, auth_token, or credentials to be set. Or for one of the `X-Api-Key`
  or `Authorization` headers to be explicitly omitted"'
preflight_status: fallback
recent_related_prs: []
surfaces_in_scope:
- curated-orders-sql
test_files: []
```

The key claims are `preflight_status: fallback`, exit 0, the mentioned file under `files_to_read_only`, and the human-gated file under `do_not_touch`. The fingerprint is derived from task mode, task text, and Git `HEAD`; it changes when those inputs change.

## 6. Check the example rule registry

The demo config points governance at the maintained files in `examples/governance/`. Those paths are repository-root-relative, so these two commands pass `--root ../..` while continuing to load this directory's `corral.yaml`:

```console
$ corral governance check --root ../.. --config corral.yaml
instruction-governance: clean
```

The local check confirms that every registered anchor is present in its declared example instruction file.

## 7. Replay reviewed retrieval cases

```console
$ corral governance replay --root ../.. --config corral.yaml
[  ok] pr#101 tier=critical recall=2/2 bundle=108<=2000 fired=['payments_config']
[  ok] pr#102 tier=elevated recall=2/2 bundle=110<=1800 fired=['orders_api']
[  ok] issue#103 tier=standard recall=1/1 bundle=77<=1200 fired=[]
overall expected-load recall: 1.0000 (floor 0.95)
  critical recall: 1.0000
  elevated recall: 1.0000
  standard recall: 1.0000
Instruction retrieval-replay: clean (3 cases; overall recall 1.0000).
```

These are synthetic corpus results, not a product benchmark. They show that the checked-in trigger rules retrieve the reviewed expected files without loading each case's forbidden topic file. Continue with the [weekly retrospective guide](../../docs/retro.md) or return to the [README adoption path](../../README.md#adoption-path).
