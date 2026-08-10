"""Build a unified networkx DiGraph from the code-map parquet artifacts and
any hand-authored flow manifests.

Inputs:
  - lineage edges (src, dst, edge_type) from ``edges.parquet`` — resolved as
    an explicit ``edges_path`` argument, else ``lineage.output`` from
    ``corral.yaml`` when set there, else <code_map>/edges.parquet
  - <code_map>/imports.parquet       — module-level import edges
  - <code_map>/flows/*.flow.yaml     — hand-authored flow nodes + transitions

The graph is a DiGraph where every edge carries an ``edge_type`` attribute.
Node IDs follow these conventions:
  * Symbols:     ``"src/pkg/module.py:main"``
  * Modules:     ``"src/pkg/module.py"``
  * Tables:      ``"orders"``
  * Flow nodes:  ``"etl_pipeline::stage.run"``

Node attributes: ``kind`` (symbol | module | table | artifact | flow_node | config)
plus ``label``, ``flow_id`` for flow nodes.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    import networkx as nx

_NX_IMPORT_ERROR = "networkx is required for codemap query. Install it with: pip install networkx"


def _nx() -> type[nx]:  # type: ignore[valid-type]
    try:
        import networkx  # type: ignore[import-not-found]

        return networkx
    except ImportError as exc:
        raise ImportError(_NX_IMPORT_ERROR) from exc


def _find_repo_root() -> Path:
    """Walk up from the current directory looking for a built code map.

    A directory qualifies as the repo root when it holds a ``corral.yaml``
    and the code-map output directory configured there exists.
    """
    from corral.config import CONFIG_FILENAME, load_config

    here = Path.cwd().resolve()
    for cand in (here, *here.parents):
        config_path = cand / CONFIG_FILENAME
        if not config_path.is_file():
            continue
        config = load_config(config_path)
        if (cand / config.codemap.output_dir).is_dir():
            return cand
    raise RuntimeError(
        "codemap query: repo root not found (expected corral.yaml plus a built code map directory)"
    )


def _default_code_map_dir(root: Path) -> Path:
    """Return the code-map directory for *root*, honouring ``corral.yaml``."""
    from corral.config import CONFIG_FILENAME, load_config

    config_path = root / CONFIG_FILENAME
    if config_path.is_file():
        return root / load_config(config_path).codemap.output_dir
    return root / "code_map"


def _norm_links(value: object) -> list[str]:
    """Normalise a ``links`` value that may be a string or a list of strings."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str)]
    return []


def _module_file_for_dotted(root: Path, dotted_module: str) -> str | None:
    """Return the repo-relative file path for a dotted module, if it exists."""
    rel = Path(*dotted_module.split("."))
    module_file = rel.with_suffix(".py")
    if (root / module_file).is_file():
        return module_file.as_posix()
    package_init = rel / "__init__.py"
    if (root / package_init).is_file():
        return package_init.as_posix()
    return None


def _resolve_lineage_edges_path(root: Path, code_map_dir: Path) -> tuple[Path, bool]:
    """Resolve the lineage edges file and whether its location is configured.

    Resolution order: ``lineage.output`` from ``corral.yaml`` when set
    explicitly there, else the default ``edges.parquet`` inside the code-map
    directory. The second element is True when the path is configured —
    callers must fail loudly when a configured file is missing instead of
    silently dropping every lineage edge.
    """
    from corral.config import CONFIG_FILENAME, load_config

    config_path = root / CONFIG_FILENAME
    if config_path.is_file():
        lineage = load_config(config_path).lineage
        if lineage.output_configured:
            return (root / lineage.output).resolve(), True
    return code_map_dir / "edges.parquet", False


def _add_edges_parquet(G: nx.DiGraph, edges_path: Path) -> None:
    """Load ``edges.parquet`` and add every row as a directed edge."""
    if not edges_path.exists():
        return
    import pyarrow.parquet as pq

    for row in pq.read_table(edges_path).to_pylist():
        src, dst = row["src"], row["dst"]
        G.add_node(src, kind=row.get("src_kind", "symbol"))
        G.add_node(dst, kind=row.get("dst_kind", "symbol"))
        G.add_edge(
            src,
            dst,
            edge_type=row.get("edge_type", "unknown"),
            lineno=row.get("lineno"),
            evidence=row.get("evidence") or "",
        )


def _add_imports_parquet(G: nx.DiGraph, imports_path: Path) -> None:
    """Load ``imports.parquet`` and add import edges (module → module/symbol)."""
    if not imports_path.exists():
        return
    import pyarrow.parquet as pq

    root = imports_path.parent.parent
    for row in pq.read_table(imports_path).to_pylist():
        src_file: str = row["source_file"]
        G.add_node(src_file, kind="module")

        tmod: str = row.get("target_module") or ""
        tsym: str | None = row.get("target_symbol")
        if not tmod or tmod.startswith("."):
            # Relative imports — skip: edge resolution requires full package path
            continue
        if tsym:
            submodule_file = _module_file_for_dotted(root, f"{tmod}.{tsym}")
            if submodule_file is not None:
                dst = submodule_file
                G.add_node(dst, kind="module")
            else:
                base_module = _module_file_for_dotted(root, tmod)
                dst = (
                    f"{base_module}:{tsym}"
                    if base_module
                    else f"{tmod.replace('.', '/')}.py:{tsym}"
                )
                G.add_node(dst, kind="symbol")
        else:
            dst = _module_file_for_dotted(root, tmod) or f"{tmod.replace('.', '/')}.py"
            G.add_node(dst, kind="module")
        G.add_edge(src_file, dst, edge_type="import")


def _add_flow_manifests(G: nx.DiGraph, flows_dir: Path) -> None:
    """Load every ``*.flow.yaml`` and add flow nodes + transition edges."""
    if not flows_dir.exists():
        return

    for flow_path in sorted(flows_dir.glob("*.flow.yaml")):
        try:
            flow = yaml.safe_load(flow_path.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            continue

        flow_id: str = flow.get("flow_id") or flow_path.stem

        for node in flow.get("nodes") or []:
            key = f"{flow_id}::{node['id']}"
            G.add_node(
                key,
                kind="flow_node",
                label=node.get("label") or "",
                flow_id=flow_id,
            )

            # Soft links from the flow node to code-map artifacts
            links: dict = node.get("links") or {}
            for table in _norm_links(links.get("table")):
                G.add_node(table, kind="table")
                G.add_edge(key, table, edge_type="flow_references")
            for sym in _norm_links(links.get("symbol")):
                if ":" in sym:
                    # Normalise dotted-module form ("src.pkg.module:main")
                    # to file form ("src/pkg/module.py:main")
                    mod, name = sym.rsplit(":", 1)
                    if not mod.endswith(".py") and "/" not in mod:
                        sym = f"{mod.replace('.', '/')}.py:{name}"
                    G.add_node(sym, kind="symbol")
                    G.add_edge(key, sym, edge_type="flow_references")
            for script in _norm_links(links.get("script")):
                G.add_node(script, kind="module")
                G.add_edge(key, script, edge_type="flow_references")

            # Transition edges to next nodes in the same or other flows
            for edge in node.get("next") or []:
                to: str = edge["to"]
                to_key = to if "::" in to else f"{flow_id}::{to}"
                # Ensure the target node exists (may be a forward reference)
                if to_key not in G:
                    G.add_node(to_key, kind="flow_node")
                G.add_edge(
                    key,
                    to_key,
                    edge_type=edge.get("kind") or "transition",
                    condition=edge.get("condition") or "",
                )


def build_unified_graph(
    root: Path | None = None,
    *,
    code_map_dir: Path | None = None,
    include_imports: bool = False,
    edges_path: Path | None = None,
) -> nx.DiGraph:
    """Build and return the merged code-map networkx DiGraph.

    Parameters
    ----------
    root:
        Repo root. Detected automatically when *None*.
    code_map_dir:
        Override the code-map directory location (defaults to the
        ``codemap.output_dir`` configured at *root*).
    include_imports:
        When *True*, also add import edges from ``imports.parquet``.
        Off by default to keep the graph lean for impact/lineage queries.
    edges_path:
        Explicit lineage edges file. Wins over ``lineage.output`` from
        ``corral.yaml``, which in turn wins over the default
        ``edges.parquet`` inside *code_map_dir*. A configured or explicit
        path that does not exist raises :class:`FileNotFoundError`.
    """
    nx = _nx()
    if root is None:
        root = _find_repo_root()
    cm = code_map_dir if code_map_dir is not None else _default_code_map_dir(root)

    if edges_path is not None:
        lineage_edges_path, edges_configured = Path(edges_path), True
    else:
        lineage_edges_path, edges_configured = _resolve_lineage_edges_path(root, cm)
    if edges_configured and not lineage_edges_path.is_file():
        raise FileNotFoundError(
            f"codemap query: lineage edges file not found: {lineage_edges_path} "
            "(run `corral lineage build`, or point lineage.output in corral.yaml "
            "/ --edges-path at an existing file)"
        )

    G: nx.DiGraph = nx.DiGraph()
    _add_edges_parquet(G, lineage_edges_path)
    if include_imports:
        _add_imports_parquet(G, cm / "imports.parquet")
    _add_flow_manifests(G, cm / "flows")
    return G
