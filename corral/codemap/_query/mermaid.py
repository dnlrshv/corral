"""Mermaid flowchart renderer for the unified code-map graph.

Given a networkx DiGraph (or a subgraph) and an optional focal node, emits
a Mermaid ``graph TD`` block that can be pasted directly into GitHub Markdown
or a Mermaid live editor.

Node IDs in Mermaid must be valid identifiers (no slashes, colons, dots).
We assign short sequential IDs (``N0``, ``N1``, …) and put the full node
name in the label bracket so the diagram stays human-readable.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from .commands import _resolve_node

if TYPE_CHECKING:
    import networkx as nx

# Maximum number of nodes to render before we truncate with a warning comment.
MAX_RENDER_NODES = 80

# Map edge_type values to short Mermaid arrow labels.
_EDGE_LABEL: dict[str, str] = {
    "writes_table": "writes",
    "reads_table": "reads",
    "writes_file": "writes_file",
    "reads_file": "reads_file",
    "reads_config": "reads_cfg",
    "calls": "calls",
    "import": "import",
    "flow_references": "refs",
    "transition": "",
    "happy": "",
    "branch": "branch",
    "error": "error",
    "cross_flow": "cross",
    "cross_flow_transition": "cross",
}

# Map node kind to a Mermaid shape.
_NODE_SHAPE: dict[str, tuple[str, str]] = {
    "table": ("[(", ")]"),
    "flow_node": ("([", "])"),
    "config": ("{{", "}}"),
    "symbol": ("[", "]"),
    "module": ("[", "]"),
    "artifact": ("[/", "/]"),
}
_DEFAULT_SHAPE = ("[", "]")


def _safe_id(index: int) -> str:
    return f"N{index}"


def _mermaid_label(raw: str, kind: str, node_attrs: dict) -> str:
    """Format a node's display label for a Mermaid box."""
    label_text = node_attrs.get("label") or raw
    # Truncate long labels to keep the diagram readable
    if len(label_text) > 60:
        label_text = label_text[:57] + "..."
    # Escape double-quotes inside Mermaid string literals
    label_text = label_text.replace('"', "'")
    open_s, close_s = _NODE_SHAPE.get(kind, _DEFAULT_SHAPE)
    return f'{open_s}"{label_text}"{close_s}'


def _mermaid_edge(label: str) -> str:
    if label:
        return f"-->|{label}|"
    return "-->"


def _neighbourhood_subgraph(
    G: nx.DiGraph,
    focal: str,
    hops: int = 2,
) -> nx.DiGraph:
    """Return the sub-DiGraph within *hops* of *focal* (in either direction)."""
    import networkx as nx

    resolved = _resolve_node(G, focal)
    if resolved is None:
        raise ValueError(f"Node not found or ambiguous: {focal!r}")

    # BFS in both directions
    undirected = G.to_undirected()
    reachable = nx.single_source_shortest_path_length(undirected, resolved, cutoff=hops)
    return G.subgraph(list(reachable))


def render_mermaid(
    G: nx.DiGraph,
    focal: str | None = None,
    *,
    hops: int = 2,
) -> str:
    """Render *G* (or the *hops*-neighbourhood of *focal*) as a Mermaid diagram.

    Parameters
    ----------
    G:
        The full or pre-filtered DiGraph.
    focal:
        If given, restrict output to nodes within *hops* of this node.
    hops:
        Neighbourhood radius when *focal* is set.

    Returns
    -------
    str
        A ``graph TD\\n...`` Mermaid block (no fencing — caller may wrap it).
    """
    sub = _neighbourhood_subgraph(G, focal, hops) if focal else G

    nodes = list(sub.nodes(data=True))
    truncated = len(nodes) > MAX_RENDER_NODES
    if truncated:
        nodes = nodes[:MAX_RENDER_NODES]

    node_ids: dict[str, str] = {n: _safe_id(i) for i, (n, _) in enumerate(nodes)}
    visible_nodes: set[str] = set(node_ids)

    lines: list[str] = ["graph TD"]

    if truncated:
        lines.append(
            f"    %% WARNING: graph truncated to {MAX_RENDER_NODES} nodes"
            f" (total {sub.number_of_nodes()})"
        )

    for raw, attrs in nodes:
        nid = node_ids[raw]
        kind = attrs.get("kind", "symbol")
        label_expr = _mermaid_label(raw, kind, attrs)
        lines.append(f"    {nid}{label_expr}")

    for u, v, data in sub.edges(data=True):
        if u not in visible_nodes or v not in visible_nodes:
            continue
        uid, vid = node_ids[u], node_ids[v]
        etype = data.get("edge_type", "")
        edge_label = _EDGE_LABEL.get(etype, etype)
        # Clean up the label for Mermaid (no special chars)
        edge_label = re.sub(r"[^\w]", "_", edge_label).strip("_")
        lines.append(f"    {uid} {_mermaid_edge(edge_label)} {vid}")

    return "\n".join(lines)
