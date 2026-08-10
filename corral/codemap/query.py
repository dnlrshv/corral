"""Unified code-map query CLI — one graph over structure + lineage artifacts.

Merges the generated code-map artifacts (symbols / imports / lineage edges)
and any hand-authored flow manifests into a single networkx DiGraph and
exposes four query commands.

Commands
--------
impact  <node>                  — transitive reverse-dependency blast radius.
lineage <table>                 — producers + consumers, transitive data-flow.
path    <src> <dst>             — shortest directed path between two nodes.
render  <node> [--mermaid]      — neighbourhood subgraph; Mermaid output by default.

Node ID conventions (same as the code_map parquets):
  * Symbol:      ``src/pkg/module.py:main``
  * Module:      ``src/pkg/module.py``
  * Table:       ``orders``
  * Flow node:   ``etl_pipeline::stage.run``

Usage
-----
    # Quick blast radius of a function:
    corral codemap query impact src/pkg/module.py:main

    # Full lineage of a table:
    corral codemap query lineage orders

    # Connection path between two symbols:
    corral codemap query path src/ingest.py:main src/execute.py:main

    # Mermaid diagram for a node (default) or the full graph:
    corral codemap query render orders --mermaid

Prerequisites
-------------
Run the builders first so the parquet data exists:
    corral codemap build
    corral lineage build
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from corral.codemap._query import (
    QueryResult,
    build_unified_graph,
    cmd_impact,
    cmd_lineage,
    cmd_path,
    render_mermaid,
)

# ---------------------------------------------------------------------------
# Output formatters
# ---------------------------------------------------------------------------


def _fmt_text(result: QueryResult) -> str:
    """Human-readable plain-text summary of a QueryResult."""
    lines: list[str] = [f"=== {result.command}: {result.query} ==="]

    if not result.ok:
        lines.append(f"ERROR: {result.metadata['error']}")
        return "\n".join(lines)

    meta = result.metadata

    if result.command == "impact":
        lines.append(f"Blast radius: {meta.get('count', 0)} dependent(s)")
        for n in result.nodes:
            lines.append(f"  {n}")

    elif result.command == "lineage":
        lines.append(f"Producers ({len(meta.get('producers', []))}):")
        for p in meta.get("producers", []):
            lines.append(f"  ← {p}")
        lines.append(f"Consumers ({len(meta.get('consumers', []))}):")
        for c in meta.get("consumers", []):
            lines.append(f"  → {c}")
        other = [
            n
            for n in result.nodes
            if n not in meta.get("producers", []) + meta.get("consumers", []) + [result.query]
        ]
        if other:
            lines.append(f"Also reachable ({len(other)}):")
            for n in other[:20]:
                lines.append(f"    {n}")
            if len(other) > 20:
                lines.append(f"    … and {len(other) - 20} more")

    elif result.command == "path":
        lines.append(f"Path length: {meta.get('length', '?')} hop(s)")
        for i, n in enumerate(result.nodes):
            prefix = "  " if i == 0 else "  → "
            lines.append(f"{prefix}{n}")
            if i < len(result.edges):
                etype = result.edges[i][2].get("edge_type", "?")
                lines[-1] += f"  [{etype}]"

    return "\n".join(lines)


def _fmt_json(result: QueryResult) -> str:
    obj: dict[str, Any] = {
        "command": result.command,
        "query": result.query,
        "ok": result.ok,
        "nodes": result.nodes,
        "edges": [{"src": u, "dst": v, **data} for u, v, data in result.edges],
        "metadata": result.metadata,
    }
    return json.dumps(obj, indent=2, default=str)


# ---------------------------------------------------------------------------
# Sub-command handlers
# ---------------------------------------------------------------------------


def _graph_from_args(args: argparse.Namespace):
    return build_unified_graph(
        root=args.root,
        code_map_dir=args.code_map_dir,
        include_imports=args.include_imports,
        edges_path=args.edges_path,
    )


def _run_impact(args: argparse.Namespace) -> int:
    G = _graph_from_args(args)
    result = cmd_impact(G, args.node, max_depth=args.depth)
    _print_result(result, args)
    return 0 if result.ok else 1


def _run_lineage(args: argparse.Namespace) -> int:
    G = _graph_from_args(args)
    result = cmd_lineage(G, args.table, max_depth=args.depth)
    _print_result(result, args)
    return 0 if result.ok else 1


def _run_path(args: argparse.Namespace) -> int:
    G = _graph_from_args(args)
    result = cmd_path(G, args.src, args.dst)
    _print_result(result, args)
    return 0 if result.ok else 1


def _run_render(args: argparse.Namespace) -> int:
    G = _graph_from_args(args)
    focal = getattr(args, "node", None)
    hops = getattr(args, "hops", 2)
    try:
        mermaid = render_mermaid(G, focal=focal, hops=hops)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    if args.mermaid:
        print("```mermaid")
        print(mermaid)
        print("```")
    else:
        print(mermaid)
    return 0


def _print_result(result: QueryResult, args: argparse.Namespace) -> None:
    fmt = getattr(args, "format", "text")
    if fmt == "json":
        print(_fmt_json(result))
    else:
        print(_fmt_text(result))


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)",
    )
    parser.add_argument(
        "--include-imports",
        action="store_true",
        help="Also load imports.parquet edges (slower; broader graph)",
    )
    parser.add_argument(
        "--depth",
        type=int,
        default=10,
        help="Maximum traversal depth for impact/lineage (default: 10)",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="Repository root (default: auto-detected from corral.yaml)",
    )
    parser.add_argument(
        "--code-map-dir",
        type=Path,
        default=None,
        help="Directory holding the code-map parquets "
        "(default: <root>/<codemap.output_dir> from corral.yaml)",
    )
    parser.add_argument(
        "--edges-path",
        type=Path,
        default=None,
        help="Lineage edges.parquet to load (default: lineage.output from "
        "corral.yaml when set, else <code-map-dir>/edges.parquet)",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # impact
    p_impact = sub.add_parser("impact", help="Transitive reverse-dependency blast radius")
    p_impact.add_argument("node", help="Node ID or suffix, e.g. 'module.py:main'")

    # lineage
    p_lineage = sub.add_parser("lineage", help="Producers + consumers of a table/node")
    p_lineage.add_argument("table", help="Table name or node ID, e.g. 'orders'")

    # path
    p_path = sub.add_parser("path", help="Shortest path between two nodes")
    p_path.add_argument("src", help="Source node")
    p_path.add_argument("dst", help="Destination node")

    # render
    p_render = sub.add_parser("render", help="Emit a Mermaid subgraph")
    p_render.add_argument(
        "node",
        nargs="?",
        default=None,
        help="Focal node (shows N-hop neighbourhood); omit for whole graph",
    )
    p_render.add_argument(
        "--mermaid",
        action="store_true",
        default=True,
        help="Wrap output in Mermaid fences (default: on)",
    )
    p_render.add_argument(
        "--no-mermaid",
        dest="mermaid",
        action="store_false",
        help="Print raw Mermaid without fences",
    )
    p_render.add_argument(
        "--hops", type=int, default=2, help="Neighbourhood radius around focal node (default: 2)"
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    dispatch = {
        "impact": _run_impact,
        "lineage": _run_lineage,
        "path": _run_path,
        "render": _run_render,
    }
    handler = dispatch.get(args.command)
    if handler is None:
        parser.print_help()
        return 1
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
