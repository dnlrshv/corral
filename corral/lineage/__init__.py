"""Data-lineage extractors used by :mod:`corral.lineage.build`.

The package is split into focused modules so each extractor stays small and
can be unit-tested in isolation:

- :mod:`corral.lineage.schema` — :class:`Edge` dataclass + Arrow schema
- :mod:`corral.lineage.python_ast` — shared AST helpers
- :mod:`corral.lineage.extract_yaml` — declarative writes from the pipeline
  manifest YAML
- :mod:`corral.lineage.extract_sql` — SQL read/write detection in Python
  string literals
- :mod:`corral.lineage.extract_files` — file read/write detection from
  common pandas / pyarrow / ``open()`` patterns
- :mod:`corral.lineage.extract_config` — config-reference edges
  (``reads_config``) from known config-loader call sites
- :mod:`corral.lineage.extract_calls` — intra-project ``calls`` edges
"""

from __future__ import annotations

from .extract_calls import SymbolIndex, build_symbol_index
from .schema import EDGE_TYPES, EDGES_SCHEMA, Edge, sorted_edges

__all__ = [
    "EDGES_SCHEMA",
    "EDGE_TYPES",
    "Edge",
    "SymbolIndex",
    "build_symbol_index",
    "sorted_edges",
]
