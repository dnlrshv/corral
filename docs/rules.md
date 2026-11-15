# Executor rules

`corral rules show --root .` shows applicable rules and their source. Add
`--json` for structured output or `--repo`, `--tier`, `--phase` and `--harness`
to preview applicability. Executor briefs use the same rules; the existing
preflight brief remains a separate artifact.

Rules merge in order: packaged generic defaults, the repository file declared
by `executor_brief.rules_file` in `corral.yaml`, then the personal overlay.
The overlay lives outside the repository so personal instructions cannot be
committed as shared defaults. Set `CORRAL_OVERLAY_FILE`, or use
`$XDG_CONFIG_HOME/corral/overlay.yaml` (default `~/.config/corral/overlay.yaml`).
A missing overlay is normal; malformed files and overlays inside the workspace
are refused.

Each file uses this shape:

```yaml
schema_version: 1
rules:
  - id: project.verification
    text: Run the listed verification commands in the workspace.
    reason: Their paths are relative to the prepared checkout.
    applies_to:
      phases: [implementation, repair]
```

Records require an id, text and reason. Optional applicability lists are
`repos` (globs against the repository profile name or resolved workspace),
`tiers`, `phases` and `harnesses` (exact matches). Each non-empty list must
match for the rule to apply. Omitted or empty lists match anything.

A later record replaces an earlier record with the same id as a whole, keeping
its position. Set `enabled: false` on a complete record to remove that id.
An enabled record's `supersedes` list removes those ids. Remaining records keep
packaged, repository, overlay order. Duplicate ids in one file and unknown
record keys are errors; unknown top-level sections are preserved for other
consumers. Credential-shaped rule text is refused because rules enter prompts.

Repository paths cannot escape the workspace or cross symlinks. File digests
make the selected repository and overlay contents auditable.

Preview the selected rules with `corral rules show --root . --json`, or preview
their placement in a task with `corral executor-brief render task.json --root .`.
See the [executor-brief contract](executor-brief.md) for policy, launchd environment
configuration, prompt budgets and per-attempt records. A launch record includes
the merged rules digest, applicable records and repository/overlay file hashes,
so a repository rule change between generations is visible. A declared repository
file must exist because silently omitting it would hide a misspelled policy path.
