# Instruction tools

## Lint

`corral instructions lint` finds dated prompting patterns and dead references in agent instruction files. It favours precision over recall so that ordinary workflow rules remain useful. It reports findings without rewriting files, executing documented commands, fetching network data, or following symbolic links.

```bash
corral instructions lint
corral instructions lint --json --min-severity info
corral instructions lint AGENTS.md --strict
corral instructions lint --list-checks
corral instructions lint --select dead-path,dead-command
```

With no paths, discovery includes tracked and untracked files that Git does not ignore; outside Git it walks the root. The include set covers AGENTS.md, CLAUDE.md, CLAUDE.local.md, GEMINI.md, Cursor and Copilot rules, Claude commands and agents, and Claude/Codex skills. Nested instruction-file variants are included. Governance instruction globs are appended by default. Dependencies, virtualenvs, Git metadata, vendor trees and agent worktrees are excluded.

Only `.md`, `.mdc`, `.markdown`, `.txt` and extensionless instruction files are read. JSON settings are skipped because they can contain secrets. Explicit paths and directories bypass the include set; exclusions still apply. `--only-configured` keeps the include filter for explicit staged paths. Files over the byte threshold, files with more than 1% replacement characters after UTF-8 decoding, and symbolic links are skipped with a diagnostic; those skips do not fail the run.

Frontmatter, code, comments and link destinations are masked for prose checks. Retired model pins are also checked in code, and in the `description` and `model` frontmatter fields. Skill trigger checks inspect only the frontmatter description. Reasoned emphasis and files declaring RFC 2119/BCP 14 keyword semantics are exempt from caps checks.

### Checks

| Check | Instruction / skill | Generated rule | Example signal |
| --- | --- | --- | --- |
| caps-emphasis | warning; density info | error | `NEVER edit files. ALWAYS run checks.` |
| double-bang | warning | error | `Stop!!` |
| retired-model-id | retired error; deprecated warning | same | A pin to a model in the packaged retirement table |
| thinking-scaffold | warning | error | `Think step by step` |
| word-cap | warning | warning | `at most 50 words` |
| history-narrative | info; dated state warning | error | `As of 2026-08-23`, `(#123)` |
| migration-relative | warning; historical sub-patterns info | error | `no longer`, `previously` |
| grader-vocabulary | warning | error | `You will be graded on precision` |
| dead-path | import/link error; code/shell warning | warning | `@docs/missing.md` |
| dead-command | error | warning | `make undefined-target`, `npm run undefined-script` |
| trigger-bloat | skill only: warning/error | not run | A missing description or one over the configured limit |
| missing-reason | not run | warning | `Run the check.` without its reason |
| unknown-suppression | warning | warning | A directive naming an unknown check |
| unused-suppression | info | not run | A line directive that removes no findings |

Reference checks need a root. Paths resolve from the instruction file, repository root, or skill directory, with generated outputs, placeholders, creation sentences and out-of-root references excluded. Makefiles with variables or missing includes, missing manifests and malformed package manifests are unverifiable and produce no target findings. Git failures produce a diagnostic and leave Git-dependent reference checks unverifiable. Commands are read from code, never run.

For generated rules, callers use `lint_text(..., kind="generated_rule", config=default_lint_config())`. That kind promotes several prose signals to errors and adds missing-reason. Callers handling untrusted generated text should strip suppression comments before linting so that the producer cannot suppress its own findings.

### Configuration

Every key is optional. `include` and `exclude` replace their defaults; `extend_include` adds paths. `--root` overrides the directory holding corral.yaml, or cwd when there is no config. `--config` selects a config file; otherwise the nearest corral.yaml above cwd is used. The default include and exclude lists are shown explicitly below.

```yaml
instructions:
  lint:
    # Replaces the default list when set.
    include:
      - "AGENTS.md"
      - "**/AGENTS.md"
      - "CLAUDE.md"
      - "**/CLAUDE.md"
      - "CLAUDE.local.md"
      - "GEMINI.md"
      - "**/GEMINI.md"
      - ".cursorrules"
      - ".cursor/rules/**"
      - ".github/copilot-instructions.md"
      - ".github/instructions/**"
      - ".claude/skills/**/SKILL.md"
      - ".agents/skills/**/SKILL.md"
      - ".claude/commands/**"
      - ".claude/agents/**"
      - ".claude/rules/**"
    extend_include: []             # appended to include
    # Replaces the default list when set.
    exclude:
      - "**/node_modules/**"
      - "**/.venv/**"
      - "**/venv/**"
      - ".git/**"
      - ".claude/worktrees/**"
      - "**/vendor/**"
      - "**/site-packages/**"
    use_governance_globs: true     # also lint governance.instruction_globs
    max_file_bytes: 1048576
    min_severity: warning          # display threshold for text and JSON (findings below it are counted, not listed)
    checks:
      caps-emphasis:
        enabled: true
        severity: null             # null = per-kind defaults in the table above
        severity_by_kind: {}
        exclude: []                # path globs where this check does not run
        tokens: ["MUST NOT", "DO NOT", MUST, NEVER, ALWAYS, CRITICAL, IMPORTANT]
        booster_labels: [CRITICAL, IMPORTANT, NOTE, WARNING, REMEMBER]
        reason_markers: [because, since, "so that", otherwise, "to avoid", "to prevent", "to keep", "which would", "as this", "as it", "reason:", "why:"]
        paragraph_threshold: 2
        file_min_count: 5
        file_density_per_1000_words: 5.0
      double-bang: {enabled: true}
      retired-model-id:
        enabled: true
        severity_retired: error
        severity_deprecated: warning
        extra: []                  # [{id, status, replacement, pattern}]
        allow: []                  # regexes
        exclude: []                # e.g. ["docs/migrations/**", "CHANGELOG.md"]
      thinking-scaffold: {enabled: true, allow_agent_keywords: true}
      word-cap: {enabled: true}
      history-narrative: {enabled: true}
      migration-relative: {enabled: true}
      grader-vocabulary: {enabled: true}
      dead-path:
        enabled: true
        check_ignored: true
        ignore: []                 # globs of paths never reported
      dead-command: {enabled: true}
      trigger-bloat:
        enabled: true
        max_chars: 1024
        warn_chars: 600
        max_quoted_phrases: 5
      missing-reason: {enabled: true}
```

Every check accepts `enabled`, `severity`, `severity_by_kind` and `exclude`. A per-kind severity wins over the general severity, which wins over the default. Unknown keys, unknown check names, incorrect types and invalid regular expressions fail with exit 2. `--select` runs only named checks; `--ignore` skips named checks. Both accept comma-separated names and reject unknown names.

The retired-model table is a release snapshot with an `as_of` date printed in each finding. It is refreshed with corral releases, without a network lookup during lint. Add provider IDs with `checks.retired-model-id.extra` entries containing `id`, `status` (`retired` or `deprecated`), `replacement` and `pattern`. `allow` contains regexes that fully cover an allowed model match; per-check `exclude` is suitable for migration histories.

### Suppressions

Prefer a per-check config `exclude` for a known exception, because an inline suppression is itself text the coding agent loads. For a local exception, use an HTML comment:

```markdown
<!-- corral-lint: disable-next-line=double-bang -- quoted external instruction -->
Stop!!

Stop!! <!-- corral-lint: disable-line=double-bang -- quoted external instruction -->
<!-- corral-lint: disable-file=retired-model-id -- historical migration record -->
```

Names may be comma-separated, or `all`. A next-line directive applies to the next nonblank line. In `.txt` and extensionless files such as `.cursorrules`, a standalone `# corral-lint: disable-next-line=...` is also accepted. Markdown uses HTML comments because `#` denotes a heading. The report counts suppressed findings, flags unknown check names, and reports unused line directives at info severity.

### Reports and exit codes

Text output gives `path:line:column: severity check message`, then a summary. JSON (`--json`) has schema version 1, the tool and corral version, files checked, display threshold, counts for all unsuppressed severities, suppressed count, displayed findings, input diagnostics and exit code. Paths are repository-relative. Findings are sorted by path, line, column and check; JSON keys are sorted. `--min-severity` changes what is listed, while counts and exit status still consider all findings.

Exit 0 means no errors. Exit 1 means errors, or warnings with `--strict`. Exit 2 means a configuration error, invalid root, unknown selected check or missing explicit input path. Skipped files and an unreadable optional governance registry are diagnostics, not exit-2 errors.

### Governance

The governance registry is resolved against the lint root. Registered anchor spans are exempt from caps-emphasis, history-narrative and migration-relative checks because governance requires those anchors to stay verbatim. Rewriting a registered rule needs a governance proposal and an updated anchor. A malformed registry produces a diagnostic and disables anchor exemptions for that run.

`corral governance check` enforces the rule/proposal contract. `corral governance lint-budget` checks replay-manifest token ceilings. Instruction lint checks text and references; it does not duplicate those gates.

### Pre-commit and CI

Install the prerequisite first:

```bash
pip install pre-commit
```

```yaml
repos:
  - repo: https://github.com/<owner>/corral
    rev: <pinned-sha>
    hooks:
      - id: instructions-lint
        args: [--strict]  # Optional: warnings fail too.
```

```bash
pre-commit install
pre-commit run --all-files
```

The hook passes staged filenames and keeps `--only-configured` in its entry, so overriding args retains instruction-file filtering. A staged run cannot notice a deleted file referenced by an unchanged instruction file. Run the full lint without paths in CI as well:

```yaml
- run: python -m pip install "corral[memory] @ git+https://github.com/<owner>/corral@<pinned-sha>"
- run: corral instructions lint --json > instructions-lint.json; status=$?; cat instructions-lint.json; exit $status
```
