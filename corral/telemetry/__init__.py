"""Agent telemetry: session capture, weekly rollup, and CI-outcome joins.

Mechanism overview:

- :mod:`corral.telemetry.capture` — Claude Code Stop-hook session capture.
  Reads the hook payload from stdin, reconstructs the session record
  (timestamps, token usage, tool calls, PR/repo/CI context), and spools one
  JSON record per session. Fail-soft by contract: capture always exits 0 so
  telemetry can never break an agent session.
- :mod:`corral.telemetry.writer` — provider-neutral record writer for agents
  whose runtime does not expose structured token usage.
- :mod:`corral.telemetry.rollup_schema` — the decision-grade parquet schema
  plus row normalization/coercion helpers shared by the rollup.
- :mod:`corral.telemetry.rollup` — weekly rollup of session artifacts into
  ``rollup_<YYYY-Www>.parquet``.
- :mod:`corral.telemetry.ci_outcome` — reconstructs first-push / final-push
  CI outcomes from a PR's full commit history via the ``gh`` CLI.
"""
