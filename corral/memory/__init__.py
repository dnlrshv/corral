"""Agent memory: schema-validated, evidence-gated registries.

Ships two JSON Schemas as package data plus loaders/validators:

- the gotcha registry (``schemas/gotchas.schema.json``), matched to tasks by
  path/surface/workflow-kind and injected into preflight briefs;
- the instruction-refinement ledger (``schemas/refinements.schema.json``).

``corral memory validate`` checks registry files against these schemas.
"""
