"""corral — repo infrastructure for teams operating fleets of coding agents.

This package provides:

- :mod:`corral.codemap` — deterministic symbol/import maps and data-lineage
  edges as parquet artifacts, plus a unified query surface over them.
- :mod:`corral.lineage` — the lineage extractors (SQL, files, config
  references, pipeline manifests, calls).
- :mod:`corral.config` — ``corral.yaml`` loading with defaults.
- :mod:`corral.cli` — the ``corral`` command-line entry point.
"""

__version__ = "0.1.0.dev0"
