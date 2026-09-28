# Changelog

All notable changes to Corral are recorded here, following Keep a Changelog.
Every release section must include a `### Consumer action` subsection, even when no action is required.

## [0.2.0] - unreleased

### Added

- Optional service blackout windows, per-host process priority, and host-wide seat caps with status and validation.
- GitHub dry-run Store records for advisory reviews, repair branch pushes, and merges.
- Optional per-route `allowed_repositories`, `allowed_workdirs`, and `max_packet_bytes` controls, with service validation and a bounded inspection packet default.
- Shared hardened Git configuration for inspection, candidate export, and repair object operations.
- Optional route CLI version gates, model denials, repository route allowlists, and service-wide provider concurrency budgets.
- Provider route templates and native output provenance in result records and service status.

### Changed

- GitHub publication now defaults to dry-run. Live advisory COMMENT reviews require `github_post_mode: comment`; repair pushes additionally require `allow_repair_push: true`, and merges require `allow_merge: true` in the coordinator or merge CLI config.
- Seat process environments remove credential-shaped variables (names ending in `_TOKEN`, `_KEY`, `_PAT`, `_PASS`, `_PASSWD` or `_CREDENTIALS`, containing `_SECRET` or `PASSWORD`, `AWS_*`/`ANTHROPIC_*`/`OPENAI_*`, and agent sockets such as `SSH_AUTH_SOCK`) unless the native route declares them in `credential_env`. Deterministic workers and non-native verifiers have no route declaration, so they never receive such variables, including ones passed in a task's `env`.
- Git subprocesses for inspection, candidate export and repair run without inherited credential variables. They keep only the SSH agent and tokens a caller passes explicitly.
- `allowed_repositories` entries must be repository profile names; values containing `/` or `:` (remote slugs) are refused.
- Native CLI version probes run under the demonstrated worker boundary before model launch; over-budget provider work remains queued.

### Fixed

- Packet-only inspection routes can verify HTTPS endpoints: the interpreter's public TLS trust store is now readable inside the boundary. Before this fix, the generic `*.pem` secret denial also blocked the CA bundle, so every inspection request to an HTTPS provider failed certificate verification. Earlier live proofs of the inspection path used synthetic, zero-inference fixtures and did not exercise a real provider call. Transport errors now name the underlying reason class (for example `URLError(SSLCertVerificationError)`). No consumer action is required.
- Route scope is checked at service admission and again before native launch, including resolved checkout paths.

### Consumer action

Declare every credential a native route needs in `credential_env` and provide it through the controller's private credential configuration. Set route scopes before enabling implementation or repair lanes. Consumers that relied on inherited credential variables must update their route declarations. A deterministic worker or non-native verifier that needs a credential must now run through a native route that declares it. A route with `allowed_workdirs` refuses tasks that carry no workspace. The new route, repository, and service fields are optional; set `version_argv` when using `min_cli_version`, and align provider names across profiles and budgets. **Consumers relying on default GitHub publication must set `github_post_mode: comment` explicitly.** To continue repair branch publication, set per-repository `allow_repair_push: true`; to continue merges, set `allow_merge: true` in the coordinator or merge CLI config. For standalone advisory publication, pass `--post-mode comment --allow-network`. Otherwise these writes create dry-run Store records only. Run `corral-service validate` on each execution host before enabling priority tools. Protocol and store schema remain at version 1.

## [0.1.0] - 2026-09-27

### Added

- Initial repository toolkit: code map and lineage, surface hooks, preflight briefs, agent memory, telemetry, governed retrospectives, and instruction governance.
- Optional managed execution with a maintained service, local and authenticated remote clients, durable recovery, owner fencing, explicit profiles, usage observation, and guarded advisory publication (#1).
- Finite waves through the maintained service, including named plans, capacity and workspace fences, generation-aware reconciliation, and artifact handoffs (#3).
- Service repair pipeline with admission checks, candidate-bound publication, durable intents, and authenticated readback (#2).
- Protocol and store schema version 1, machine-readable version commands, request compatibility checks, and a documented release and consumer pickup process.

### Changed

- Wave admission now returns its record while service ticks advance the wave; source-checkout clients require explicit development mode (#3).
- Route binaries execute by their declared path, preserving virtual-environment selection (#4).

### Fixed

- Dead launcher reconciliation settles eligible work without leaving a permanent workspace fence; launch marks use compare-and-swap (#4).
- Native verifier reconciliation applies the verifier boundary; handoff Git operations stay pinned to the registered checkout (#4).

### Consumer action

Pin protocol 1; no config changes are required for this release. Pin the release tag and exact Corral commit, compare the `commit` and `protocol` fields from `corral-service version` at startup, and run the read-only `corral-service validate` before cutover. Follow [the pickup procedure](docs/upgrading.md). If a consumer uses the earlier wave action, account for its admission-only behavior described above.
