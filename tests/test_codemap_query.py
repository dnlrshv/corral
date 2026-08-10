"""Tests for the unified code-map graph and its query commands.

Requires networkx (the optional `query` extra); skipped when absent.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

nx = pytest.importorskip("networkx")

from corral.codemap.build import build_code_map_with_cache
from corral.codemap.query import main as query_main
from corral.codemap._query import (
    build_unified_graph,
    cmd_impact,
    cmd_lineage,
    cmd_path,
    render_mermaid,
)
from corral.lineage.build import build_and_write_lineage

from .conftest import DEMO_LOADERS


@pytest.fixture
def built_code_map(demo_pkg: Path, tmp_path: Path) -> tuple[Path, Path]:
    """Build codemap + lineage parquets for the fixture; return (root, dir)."""
    code_map_dir = tmp_path / "code_map"
    build_code_map_with_cache(demo_pkg, code_map_dir, use_cache=False)
    build_and_write_lineage(demo_pkg, code_map_dir, config_loaders=DEMO_LOADERS)
    return demo_pkg, code_map_dir


def test_build_unified_graph_nodes(built_code_map) -> None:
    root, cm = built_code_map
    G = build_unified_graph(root=root, code_map_dir=cm)
    assert "orders" in G
    assert "orders_archive" in G
    assert G.nodes["orders"]["kind"] == "table"
    assert "queries.py:archive_orders" in G
    assert "config/app.yaml::threshold" in G
    assert G.nodes["config/app.yaml::threshold"]["kind"] == "config"


def test_cmd_lineage_producers_and_consumers(built_code_map) -> None:
    root, cm = built_code_map
    G = build_unified_graph(root=root, code_map_dir=cm)

    result = cmd_lineage(G, "orders_archive")
    assert result.ok
    assert result.metadata["producers"] == ["queries.py:archive_orders"]
    assert result.metadata["consumers"] == []

    # Suffix resolution: the bare query matches the single table node.
    result = cmd_lineage(G, "orders")
    assert result.ok
    assert sorted(result.metadata["consumers"]) == ["queries.py", "queries.py:archive_orders"]
    assert result.metadata["producers"] == []
    # Transitive neighbourhood reaches the archive table via the writer.
    assert "orders_archive" in result.nodes


def test_cmd_impact_reverse_dependencies(built_code_map) -> None:
    root, cm = built_code_map
    G = build_unified_graph(root=root, code_map_dir=cm)

    result = cmd_impact(G, "config/app.yaml::threshold")
    assert result.ok
    assert result.nodes == [
        "pipeline.py:Reporter",
        "pipeline.py:run",
        "settings.py:get_threshold",
    ]
    assert result.metadata["count"] == 3


def test_cmd_path_between_nodes(built_code_map) -> None:
    root, cm = built_code_map
    G = build_unified_graph(root=root, code_map_dir=cm)

    result = cmd_path(G, "settings.py:get_threshold", "config/app.yaml::threshold")
    assert result.ok
    assert result.nodes == ["settings.py:get_threshold", "config/app.yaml::threshold"]
    assert result.metadata["length"] == 1
    assert result.edges[0][2]["edge_type"] == "reads_config"


def test_cmd_impact_unknown_node_errors(built_code_map) -> None:
    root, cm = built_code_map
    G = build_unified_graph(root=root, code_map_dir=cm)
    result = cmd_impact(G, "no_such_node")
    assert not result.ok
    assert "Node not found" in result.metadata["error"]


def test_render_mermaid_neighbourhood(built_code_map) -> None:
    root, cm = built_code_map
    G = build_unified_graph(root=root, code_map_dir=cm)
    mermaid = render_mermaid(G, focal="orders_archive", hops=2)
    assert mermaid.startswith("graph TD")
    assert "orders_archive" in mermaid
    assert "queries.py:archive_orders" in mermaid


def test_flow_manifests_are_loaded(built_code_map) -> None:
    root, cm = built_code_map
    flows = cm / "flows"
    flows.mkdir()
    (flows / "demo.flow.yaml").write_text(
        textwrap.dedent(
            '''
            flow_id: demo_pipeline
            nodes:
              - id: archive
                label: Archive orders
                links:
                  table: orders_archive
                  symbol: "queries:archive_orders"
                next:
                  - to: report
                    kind: happy
              - id: report
                label: Write report
                links:
                  script: pipeline.py
            '''
        )
    )

    G = build_unified_graph(root=root, code_map_dir=cm)
    assert G.nodes["demo_pipeline::archive"]["kind"] == "flow_node"
    assert G.nodes["demo_pipeline::archive"]["flow_id"] == "demo_pipeline"
    # Soft links to code-map artifacts.
    assert G.edges["demo_pipeline::archive", "orders_archive"]["edge_type"] == "flow_references"
    assert (
        G.edges["demo_pipeline::archive", "queries.py:archive_orders"]["edge_type"]
        == "flow_references"
    )
    assert G.edges["demo_pipeline::report", "pipeline.py"]["edge_type"] == "flow_references"
    # Transition edge to the next node in the same flow.
    transition = G.edges["demo_pipeline::archive", "demo_pipeline::report"]
    assert transition["edge_type"] == "happy"

    # Flow nodes show up in lineage results when they touch traced nodes.
    result = cmd_lineage(G, "orders_archive")
    assert "demo_pipeline::archive" in result.nodes


def test_query_cli_text_output(built_code_map, capsys) -> None:
    root, cm = built_code_map
    rc = query_main(
        ["--root", str(root), "--code-map-dir", str(cm), "lineage", "orders_archive"]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "=== lineage: orders_archive ===" in out
    assert "Producers (1):" in out
    assert "← queries.py:archive_orders" in out


def test_query_cli_json_output(built_code_map, capsys) -> None:
    root, cm = built_code_map
    rc = query_main(
        [
            "--format",
            "json",
            "--root",
            str(root),
            "--code-map-dir",
            str(cm),
            "impact",
            "config/app.yaml::threshold",
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["nodes"] == [
        "pipeline.py:Reporter",
        "pipeline.py:run",
        "settings.py:get_threshold",
    ]


def test_query_cli_include_imports(built_code_map) -> None:
    root, cm = built_code_map
    G = build_unified_graph(root=root, code_map_dir=cm, include_imports=True)
    # External third-party import target resolved to file form.
    assert G.has_edge("loaders.py", "yaml.py") or ("loaders.py", "yaml.py") in G.edges
    # Intra-project relative imports are skipped by design.
    assert not any(u == "pipeline.py" and v.startswith(".settings") for u, v in G.edges)


# ---------------------------------------------------------------------------
# Root auto-detection and lineage edges path resolution (corral.yaml)
# ---------------------------------------------------------------------------


@pytest.fixture
def configured_root(demo_pkg: Path) -> Path:
    """demo_pkg with a corral.yaml whose lineage.output lives outside the
    code-map directory, plus both builds run."""
    (demo_pkg / "corral.yaml").write_text(
        "codemap:\n"
        "  output_dir: code_map\n"
        "lineage:\n"
        "  output: custom/lineage/edges.parquet\n"
    )
    build_code_map_with_cache(demo_pkg, demo_pkg / "code_map", use_cache=False)
    build_and_write_lineage(
        demo_pkg, demo_pkg / "custom" / "lineage", config_loaders=DEMO_LOADERS
    )
    return demo_pkg


def test_auto_root_from_nested_cwd(configured_root: Path, monkeypatch) -> None:
    nested = configured_root / "nested" / "deep"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)

    # No root / code-map-dir / edges-path given: everything resolves from
    # the nearest corral.yaml above cwd.
    G = build_unified_graph()
    assert G.nodes["orders"]["kind"] == "table"
    result = cmd_lineage(G, "orders_archive")
    assert result.ok
    assert result.metadata["producers"] == ["queries.py:archive_orders"]


def test_configured_lineage_output_is_read(configured_root: Path) -> None:
    # edges.parquet lives outside codemap.output_dir; the graph must still
    # pick it up from lineage.output.
    G = build_unified_graph(root=configured_root)
    result = cmd_lineage(G, "orders_archive")
    assert result.ok
    assert result.metadata["producers"] == ["queries.py:archive_orders"]


def test_missing_configured_lineage_output_errors(demo_pkg: Path) -> None:
    (demo_pkg / "corral.yaml").write_text(
        "codemap:\n"
        "  output_dir: code_map\n"
        "lineage:\n"
        "  output: custom/lineage/edges.parquet\n"
    )
    build_code_map_with_cache(demo_pkg, demo_pkg / "code_map", use_cache=False)
    # The configured edges file was never built: fail loudly instead of
    # silently dropping all lineage edges.
    with pytest.raises(FileNotFoundError, match="edges"):
        build_unified_graph(root=demo_pkg)


def test_default_edges_location_missing_is_not_an_error(demo_pkg: Path) -> None:
    # lineage.output not configured: the default location inside the
    # code-map dir stays optional (lineage simply not built yet).
    (demo_pkg / "corral.yaml").write_text("codemap:\n  output_dir: code_map\n")
    build_code_map_with_cache(demo_pkg, demo_pkg / "code_map", use_cache=False)
    G = build_unified_graph(root=demo_pkg)
    assert "orders_archive" not in G


def test_explicit_edges_path_overrides_config(demo_pkg: Path) -> None:
    # Config points at a missing file; the explicit flag wins.
    (demo_pkg / "corral.yaml").write_text("lineage:\n  output: missing/edges.parquet\n")
    code_map_dir = demo_pkg / "code_map"
    build_code_map_with_cache(demo_pkg, code_map_dir, use_cache=False)
    elsewhere = demo_pkg / "elsewhere"
    build_and_write_lineage(demo_pkg, elsewhere, config_loaders=DEMO_LOADERS)

    G = build_unified_graph(root=demo_pkg, edges_path=elsewhere / "edges.parquet")
    assert "orders_archive" in G

    # An explicit path that does not exist errors too.
    with pytest.raises(FileNotFoundError, match="edges"):
        build_unified_graph(root=demo_pkg, edges_path=demo_pkg / "nope.parquet")


def test_query_cli_edges_path_flag(configured_root: Path, capsys) -> None:
    edges = configured_root / "custom" / "lineage" / "edges.parquet"
    rc = query_main(
        [
            "--root",
            str(configured_root),
            "--edges-path",
            str(edges),
            "lineage",
            "orders_archive",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "=== lineage: orders_archive ===" in out
    assert "queries.py:archive_orders" in out
