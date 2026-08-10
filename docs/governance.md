# Instruction governance contract

corral treats agent instructions as repository policy: normative rules are registered, proposed changes carry structured evidence, retrieval behavior is replayed, and merges remain a human decision. This page defines that contract. See [adoption](adoption.md) for installation, [retro](retro.md) for proposal generation, and [seats](seats.md) for independent verification.

## Normative rules registry

`governance.registry` in `corral.yaml` points to a versioned YAML registry. Each rule binds a stable ID to a distinctive verbatim anchor in an instruction file:

```yaml
schema_version: 1
rules:
  R-PAYMENTS-001:
    file: docs/instructions/core.md
    anchor: "Changes to payments-config MUST include a rollback note."
    concern_key: payments-rollbacks
    modality: MUST
    selectors:
      paths: [config/payments.yaml]
      workflows: [payments-rollout]
      surfaces: [payments-config]
    review_by: platform-maintainers
    note: Optional context for maintainers.
```

Every entry requires `file`, `anchor`, `concern_key`, `modality`, and `review_by`. The anchor must be a distinctive substring at least eight characters long and must still occur in `file`. `concern_key` is a lower-case kebab-case identity for duplicate and overlap checks. `modality` must come from `governance.modalities` (by default `MUST`, `MUST NOT`, `ASK`, and `READ`). Selectors are optional lists on three axes: repository paths, normalized workflow kinds, and IDs from `surfaces.yaml`.

`corral governance check` validates registry structure and anchor consistency locally. In gate mode it also detects added normative-looking instruction lines, compares the base and head registries, checks near-duplicates and supersession, enforces protected paths, and requires a proposal for every genuinely new, changed, or removed normative rule.

## Proposal contract

A governance pull request carries exactly one fenced YAML block with a non-empty `proposals` list. The configured cap cannot exceed three. Every changed rule ID maps to exactly one proposal.

This annotated `add_rule` example includes every field required for that operation and the optional fields commonly needed by reviewers:

```yaml
proposals:
  - operation: add_rule              # configured operation
    target_tier: topic_file          # configured instruction/control tier
    concern_key: payments-rollbacks  # stable concern identity
    review_by: platform-maintainers  # non-empty; allowlisted when configured
    rule_ids: [R-PAYMENTS-005]       # every registry rule changed by this proposal

    evidence:                        # add_rule and sharpen require evidence
      - root_incident: "#418"        # each item needs a distinct root incident
        note: Missing rollback note caused a follow-up.
      - root_incident: "#431"
        note: The same omission recurred independently.

    existing_rules_considered:       # key is required for add_rule
      - R-PAYMENTS-001
    why_sharpen_is_insufficient: >-  # non-empty and required for add_rule
      The existing rule covers configuration edits, not the rollout workflow.
    control:                         # required for add_rule and sharpen
      type: topic_file               # type must be a configured tier
      path: docs/instructions/payments.md

    supersedes: []                   # optional list of replaced rule IDs
```

The complete operation-specific rules are:

- All proposals require a valid `operation`, valid `target_tier`, `concern_key`, `review_by`, and a non-empty string list `rule_ids`.
- `add_rule` and `sharpen` require at least one `evidence` item, with a `root_incident` on every item, plus `control.type` from the configured tiers.
- `add_rule` additionally requires `existing_rules_considered` and a non-empty `why_sharpen_is_insufficient`.
- `supersedes`, when present, must be a list of rule IDs. Duplicate root incidents and mapping one rule ID to multiple proposals are rejected.
- The weekly retrospective applies its own configured distinct-incident floor before drafting. Passing the PR-body shape is not a substitute for clearing that evidence floor or independent verification.

Keep proposal configuration explicit:

```yaml
governance:
  proposals:
    operations: [sharpen, add_rule, add_skill, demote, delete]
    tiers: [executable, core, workflow_prompt, topic_file, gotcha, skill, wiki]
    max: 3
    reviewers: [platform-maintainers, api-maintainers]
```

## Trusted-base gate topology

On a pull request, validator code is policy while the head ref is untrusted data. If CI installs corral from the pull-request checkout, the same change being judged could weaken its judge. The gate therefore starts from a launcher installed from the base ref and runs:

```bash
corral governance check \
  --root "$GITHUB_WORKSPACE" \
  --base-ref "origin/${GITHUB_BASE_REF}" \
  --head-ref "$GITHUB_SHA" \
  --pr-body-file pr_body.md
```

The base validator reads governance configuration and the baseline registry from base. From head it reads only proposed registry and instruction documents, the diff, and the PR-body proposal block. `governance.protected_paths` prevents one governance-lane change from modifying the gate or other protected policy paths at the same time.

> **Design note — launcher and import hardening.** The outer workflow in [`examples/github-actions/governance-gate.yml`](../examples/github-actions/governance-gate.yml) installs the launcher from a base archive. The command then materializes the base `corral` package again for its trusted child. That child receives a `PYTHONPATH` containing only the materialized base package, not the ambient checkout path, and runs outside the head checkout. This prevents head-controlled `sitecustomize.py` or shadow modules from entering the trusted interpreter. Do not set the internal base-executed marker yourself or replace this with a direct head install.

## Deterministic retrieval replay

Instruction governance covers whether the right guidance loads, not only whether prose exists. Replay has three versioned inputs:

- The **manifest** declares instruction units, profiles, token-estimation method, and bundle budgets.
- **Trigger rules** declare the always-loaded paths and path/keyword triggers for additional units.
- The reviewed **corpus** freezes representative tasks with expected loads, forbidden loads, case tiers, and bundle ceilings.

Run:

```bash
corral governance replay
corral governance lint-budget
```

Replay deterministically evaluates which rules fire, checks expected and forbidden loads, reports per-tier and overall recall, and enforces the corpus/manifest/configured token ceilings. `corral governance build-corpus --reviewed-cases …` can normalize reviewed case metadata into a corpus; review that generated corpus before treating it as policy. The maintained synthetic inputs are under [`examples/governance/`](../examples/governance/).

## Staleness lifecycle

`corral governance staleness` compares rule selectors with telemetry sessions and merged-PR paths. It reports one of five outcomes:

- `RETAIN`: recent applicability clears both the configured rate and workflow-kind breadth requirements.
- `DEMOTE`: long-window applicability is below the configured demotion floor, recent activity did not retain it, and enough evaluable sessions exist.
- `MONITOR`: the rule sits in the neutral band between thresholds.
- `INSUFFICIENT_DATA`: the report cannot support an action with the available evaluable sessions.
- `EXEMPT`: the rule intersects a `needs_human` surface, or another explicit executable-control condition owns the behavior.

The two-window retain/demote design adds hysteresis: recent activity protects a rule from a long-window demotion, while the neutral band avoids action at the boundary. Sessions without path data are excluded from path/surface-only rule denominators; coverage is reported rather than guessed.

Surface selectors resolve through one `surfaces.yaml` mapping for both session matching and high-risk exemptions. Unknown surface IDs fail closed. If `governance.staleness.demote_target_glob` or `governance.reviewer` is absent, the report withholds an actionable demotion proposal rather than inventing a destination or reviewer. Even when configured, staleness renders a proposal for a follow-up—it does not move prose itself.

Start read-only:

```bash
corral governance staleness --dry-run
```

## Human-review-only invariant

corral may validate, draft, independently verify, replay, and render a commit plan. It does not ratify its own instruction changes:

- `corral retro run` may write schema-validated gotchas and a weekly summary, but doc/skill proposals are rendered separately and never auto-applied.
- Staleness emits demotion proposals; a human performs any follow-up edit.
- The GitHub Actions examples open pull requests. Repository reviewers decide whether to merge them.
- Refinement reversion renders a reverse patch; it never writes target instruction files.

That invariant is the boundary between evidence-producing automation and repository policy ownership. Configure CODEOWNERS or branch protection around the registry, instruction paths, workflow, and protected gate paths to express the corresponding repository-level review policy.
