"""Edge dataclass and Arrow schema for ``code_map/edges.parquet``.

The schema is intentionally narrow — one row per directed lineage edge — so the
file stays cheap to scan and easy to diff. ``src``/``dst`` use the same
repo-relative ``file.py:symbol`` / table-name / path conventions that
``symbols.parquet`` already uses.
"""

from __future__ import annotations

from dataclasses import dataclass

import pyarrow as pa

EDGES_SCHEMA = pa.schema(
    [
        ("src_kind", pa.string()),
        ("src", pa.string()),
        ("dst_kind", pa.string()),
        ("dst", pa.string()),
        ("edge_type", pa.string()),
        ("lineno", pa.int64()),
        ("evidence", pa.string()),
    ]
)

EDGE_TYPES = (
    "writes_table",
    "reads_table",
    "writes_file",
    "reads_file",
    "reads_config",
    "calls",
)


@dataclass(frozen=True)
class Edge:
    src_kind: str
    src: str
    dst_kind: str
    dst: str
    edge_type: str
    lineno: int
    evidence: str


def sorted_edges(edges: list[Edge]) -> list[Edge]:
    """Return ``edges`` sorted into a canonical order.

    Used to make ``edges.parquet`` byte-identical across runs over the same
    inputs.
    """

    return sorted(
        edges,
        key=lambda e: (
            e.src_kind,
            e.src,
            e.dst_kind,
            e.dst,
            e.edge_type,
            e.lineno,
            e.evidence,
        ),
    )
