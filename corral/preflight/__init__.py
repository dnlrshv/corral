"""Preflight brief generation for coding-agent sessions.

``corral preflight`` compresses the surfaces registry, matching agent-memory
gotchas, and task context into a small per-task brief an agent reads before
touching code. When the Anthropic SDK (``corral[preflight]`` extra) or auth
is unavailable, a fully deterministic code-map fallback is emitted instead.

Submodules:

- :mod:`corral.preflight.brief` — brief assembly (LLM, fallback, general).
- :mod:`corral.preflight.brief_validation` — post-validation of LLM output.
- :mod:`corral.preflight.auth` — model calls and env-var auth precedence.
- :mod:`corral.preflight.retry` — prompt templates and one-shot retry.
- :mod:`corral.preflight.parser` — YAML parsing and secret redaction.
- :mod:`corral.preflight.gotcha_budget` — gotcha injection caps.
- :mod:`corral.preflight.quota` — optional quota-snapshot telemetry.
- :mod:`corral.preflight.cli` — the ``corral preflight`` entry point.
"""
