"""Impact, lineage, and path query commands for the unified code-map graph.

Each command accepts a pre-built networkx DiGraph (from ``graph.build_unified_graph``)
and returns a ``QueryResult`` dataclass that the CLI and tests can inspect uniformly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import networkx as nx

# Edge types treated as "data flow" edges for lineage tracing.
_DATA_EDGE_TYPES = frozenset(
    ["writes_table", "reads_table", "writes_file", "reads_file", "reads_config"]
)
_WRITE_EDGE_TYPES = frozenset(["writes_table", "writes_file"])
_READ_EDGE_TYPES = frozenset(["reads_table", "reads_file"])


@dataclass
class QueryResult:
    """Uniform result container returned by every query command."""

    command: str
    query: str
    nodes: list[str]
    edges: list[tuple[str, str, dict[str, Any]]]
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return "error" not in self.metadata


def _resolve_node(G: nx.DiGraph, name: str) -> str | None:
    """Resolve *name* to an exact node ID, with a suffix-match fallback.

    Returns *None* when the name is ambiguous (multiple matches) or absent.
    """
    if name in G:
        return name
    # Try suffix match: "module.py:main" → "src/pkg/module.py:main"
    candidates = [n for n in G.nodes() if n.endswith(f"/{name}") or n.endswith(f":{name}")]
    if len(candidates) == 1:
        return candidates[0]
    return None


# ---------------------------------------------------------------------------
# impact
# ---------------------------------------------------------------------------


def cmd_impact(
    G: nx.DiGraph,
    node: str,
    *,
    max_depth: int = 10,
) -> QueryResult:
    """Return the transitive reverse-dependency blast radius of *node*.

    Traverses the *reversed* graph from *node*, so the result contains every
    node that (directly or transitively) depends on / calls *node*.
    """
    import networkx as nx

    resolved = _resolve_node(G, node)
    if resolved is None:
        candidates = [n for n in G.nodes() if node in n][:5]
        hint = f"  Possible matches: {candidates}" if candidates else ""
        return QueryResult(
            "impact",
            node,
            [],
            [],
            {"error": f"Node not found: {node!r}.{hint}"},
        )

    R: nx.DiGraph = G.reverse(copy=False)
    reached: dict[str, list[str]] = nx.single_source_shortest_path(R, resolved, cutoff=max_depth)
    dependents = [n for n in reached if n != resolved]

    # Build the edge list for the relevant subgraph
    involved = set(dependents) | {resolved}
    sub = G.subgraph(involved)
    edges = [(u, v, dict(data)) for u, v, data in sub.edges(data=True)]

    return QueryResult(
        "impact",
        resolved,
        sorted(dependents),
        edges,
        {"origin": resolved, "depth": max_depth, "count": len(dependents)},
    )


# ---------------------------------------------------------------------------
# lineage
# ---------------------------------------------------------------------------

_LINEAGE_EDGE_TYPES = _DATA_EDGE_TYPES | frozenset(["calls"])


def _lineage_bfs(G: nx.DiGraph, start: str, max_depth: int) -> set[str]:
    """Bidirectional BFS through data-flow and call edges from *start*.

    Treats the graph as undirected for traversal purposes: follows edges in
    both the forward direction (G[n]) and the backward direction (G.predecessors)
    when the edge type is a data-flow or call edge. This ensures that
    "module A reads table T, module B writes table T" chains are discovered
    regardless of which direction the caller starts from.
    """
    visited: set[str] = {start}
    frontier = [start]

    for _ in range(max_depth):
        if not frontier:
            break
        next_frontier: list[str] = []
        for n in frontier:
            # Forward: module → table (reads_table / writes_table / calls)
            for dst, edge_data in G[n].items():
                if edge_data.get("edge_type", "") in _LINEAGE_EDGE_TYPES and dst not in visited:
                    visited.add(dst)
                    next_frontier.append(dst)
            # Backward: treat edges as undirected — e.g. find modules that
            # write/read the same table as n, or callers of n.
            for src in G.predecessors(n):
                edge_data = G.edges[src, n]
                if edge_data.get("edge_type", "") in _LINEAGE_EDGE_TYPES and src not in visited:
                    visited.add(src)
                    next_frontier.append(src)
        frontier = next_frontier

    return visited


def cmd_lineage(
    G: nx.DiGraph,
    table: str,
    *,
    max_depth: int = 5,
) -> QueryResult:
    """Trace producers and consumers of *table* (or any data-node) transitively.

    For a table node the result includes:
    - **producers**: symbols/modules that write to the table
    - **consumers**: symbols/modules that read from the table
    - All nodes reachable via data-flow + call edges (upstream and downstream)
    """
    resolved = _resolve_node(G, table)
    if resolved is None:
        # Last-resort: find any table-kind node whose name contains the query
        table_matches = [n for n in G.nodes() if G.nodes[n].get("kind") == "table" and table in n]
        if len(table_matches) == 1:
            resolved = table_matches[0]
        else:
            hint = (
                f"  Table nodes containing {table!r}: {table_matches[:5]}" if table_matches else ""
            )
            return QueryResult(
                "lineage",
                table,
                [],
                [],
                {"error": f"Node not found: {table!r}.{hint}"},
            )

    # Direct producers (src --writes_table--> resolved) and
    # consumers (src --reads_table--> resolved).
    # Note: in edges.parquet *both* read and write edges point src→table.
    producers: list[str] = []
    consumers: list[str] = []
    for src, _, data in G.in_edges(resolved, data=True):
        etype = data.get("edge_type", "")
        if etype in _WRITE_EDGE_TYPES:
            producers.append(src)
        elif etype in _READ_EDGE_TYPES:
            consumers.append(src)

    # Full bidirectional lineage neighbourhood
    all_nodes = _lineage_bfs(G, resolved, max_depth)

    # Include flow-manifest nodes that reference any of the core nodes
    for n in list(G.nodes()):
        if G.nodes[n].get("kind") == "flow_node":
            for _, dst, _ in G.out_edges(n, data=True):
                if dst in all_nodes:
                    all_nodes.add(n)
                    break

    sub = G.subgraph(all_nodes)
    edges = [(u, v, dict(data)) for u, v, data in sub.edges(data=True)]

    return QueryResult(
        "lineage",
        resolved,
        sorted(all_nodes),
        edges,
        {
            "target": resolved,
            "producers": sorted(producers),
            "consumers": sorted(consumers),
        },
    )


# ---------------------------------------------------------------------------
# path
# ---------------------------------------------------------------------------


def cmd_path(G: nx.DiGraph, src: str, dst: str) -> QueryResult:
    """Find the shortest directed path from *src* to *dst* in the graph."""
    import networkx as nx

    src_r = _resolve_node(G, src)
    dst_r = _resolve_node(G, dst)

    label = f"{src} → {dst}"

    if src_r is None:
        return QueryResult("path", label, [], [], {"error": f"Source not found: {src!r}"})
    if dst_r is None:
        return QueryResult("path", label, [], [], {"error": f"Destination not found: {dst!r}"})

    try:
        path = nx.shortest_path(G, src_r, dst_r)
    except nx.NetworkXNoPath:
        return QueryResult(
            "path",
            f"{src_r} → {dst_r}",
            [src_r, dst_r],
            [],
            {"error": f"No path from {src_r!r} to {dst_r!r}"},
        )
    except nx.NodeNotFound as exc:
        return QueryResult("path", label, [], [], {"error": str(exc)})

    edges = [
        (path[i], path[i + 1], dict(G.edges[path[i], path[i + 1]])) for i in range(len(path) - 1)
    ]
    return QueryResult(
        "path",
        f"{src_r} → {dst_r}",
        path,
        edges,
        {"length": len(path) - 1},
    )
