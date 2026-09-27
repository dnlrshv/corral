# Changelog

All notable changes to Corral are recorded here, following Keep a Changelog.
Every release section must include a `### Consumer action` subsection, even when no action is required.

## [0.1.0] - unreleased

### Added

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

Pin protocol 1; no config changes are required for this release. Pin the release tag and exact Corral commit, check `corral-service version` at startup, and follow [the pickup procedure](docs/upgrading.md). If a consumer uses the earlier wave action, account for its admission-only behavior described above.
