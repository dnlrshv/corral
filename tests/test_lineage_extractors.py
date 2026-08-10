"""Unit tests for each lineage extractor over the demo_pkg fixture."""

from __future__ import annotations

import textwrap
from pathlib import Path

from corral.lineage.extract_calls import build_symbol_index, extract_call_edges
from corral.lineage.extract_config import extract_config_edges
from corral.lineage.extract_files import extract_file_edges
from corral.lineage.extract_sql import extract_sql_edges
from corral.lineage.extract_yaml import extract_yaml_edges
from corral.lineage.python_ast import flatten_string_node

from .conftest import DEMO_LOADERS, edge_tuples


# ---------------------------------------------------------------------------
# SQL extractor
# ---------------------------------------------------------------------------


def test_sql_extractor_finds_reads_and_writes(demo_pkg: Path) -> None:
    edges = extract_sql_edges(demo_pkg / "queries.py", "queries.py")
    tuples = edge_tuples(
        [
            {
                "src_kind": e.src_kind,
                "src": e.src,
                "dst_kind": e.dst_kind,
                "dst": e.dst,
                "edge_type": e.edge_type,
            }
            for e in edges
        ]
    )
    assert tuples == {
        # Module-level query string is attributed to the module itself.
        ("module", "queries.py", "table", "orders", "reads_table"),
        # The INSERT...SELECT statement is both a write and a read.
        ("symbol", "queries.py:archive_orders", "table", "orders_archive", "writes_table"),
        ("symbol", "queries.py:archive_orders", "table", "orders", "reads_table"),
    }


def test_sql_extractor_ignores_docstrings(tmp_path: Path) -> None:
    path = tmp_path / "doc.py"
    path.write_text(
        textwrap.dedent(
            '''
            def f():
                """Example: SELECT id FROM not_a_real_table."""
                return 1
            '''
        )
    )
    assert extract_sql_edges(path, "doc.py") == []


# ---------------------------------------------------------------------------
# File I/O extractor
# ---------------------------------------------------------------------------


def test_file_extractor_finds_reads_and_writes(demo_pkg: Path) -> None:
    edges = extract_file_edges(demo_pkg / "pipeline.py", "pipeline.py")
    tuples = {(e.edge_type, e.dst, e.src) for e in edges}
    assert tuples == {
        ("writes_file", "out/report.txt", "pipeline.py:run"),
        ("reads_file", "out/report.txt", "pipeline.py:run"),
    }


def test_file_extractor_skips_dynamic_paths(tmp_path: Path) -> None:
    path = tmp_path / "dyn.py"
    path.write_text(
        textwrap.dedent(
            '''
            def f(name):
                with open(f"data/{name}.csv", "w") as fh:
                    fh.write("x")
            '''
        )
    )
    assert extract_file_edges(path, "dyn.py") == []


# ---------------------------------------------------------------------------
# Config extractor
# ---------------------------------------------------------------------------


def _config_tuples(edges) -> set[tuple[str, str, str]]:
    return {(e.src, e.dst, e.edge_type) for e in edges}


def test_config_extractor_honours_known_loaders(demo_pkg: Path) -> None:
    edges = extract_config_edges(
        demo_pkg / "settings.py", "settings.py", loaders=DEMO_LOADERS
    )
    assert _config_tuples(edges) == {
        ("settings.py:get_threshold", "config/app.yaml::threshold", "reads_config"),
        ("settings.py:get_mode", "config/app.yaml::mode", "reads_config"),
    }


def test_config_extractor_ignores_unknown_loaders(demo_pkg: Path) -> None:
    # With no loaders configured at all, settings.py produces nothing —
    # including the get_secret() call to the undeclared loader.
    assert extract_config_edges(demo_pkg / "settings.py", "settings.py") == []
    # And with only load_app_config declared, the unknown loader still
    # produces no edges.
    edges = extract_config_edges(
        demo_pkg / "settings.py", "settings.py", loaders=DEMO_LOADERS
    )
    assert all("token" not in e.dst for e in edges)


def test_config_extractor_key_prefixes(tmp_path: Path) -> None:
    path = tmp_path / "wrapper.py"
    path.write_text(
        textwrap.dedent(
            '''
            def use_metrics():
                cfg = load_metric_defs()
                return cfg.get("retention")
            '''
        )
    )
    edges = extract_config_edges(
        path,
        "wrapper.py",
        loaders={"load_metric_defs": "config/metrics.yaml"},
        key_prefixes={"load_metric_defs": "metrics"},
    )
    assert _config_tuples(edges) == {
        # The wrapper call itself surfaces the already-traversed prefix...
        ("wrapper.py:use_metrics", "config/metrics.yaml::metrics", "reads_config"),
        # ...and subsequent accesses extend it.
        ("wrapper.py:use_metrics", "config/metrics.yaml::metrics.retention", "reads_config"),
    }


def test_config_extractor_sub_bindings(tmp_path: Path) -> None:
    path = tmp_path / "sub.py"
    path.write_text(
        textwrap.dedent(
            '''
            def use_cfg():
                cfg = load_app_config()
                sub = cfg.get("limits")
                return sub.get("upper")
            '''
        )
    )
    edges = extract_config_edges(path, "sub.py", loaders=DEMO_LOADERS)
    assert _config_tuples(edges) == {
        ("sub.py:use_cfg", "config/app.yaml::limits", "reads_config"),
        ("sub.py:use_cfg", "config/app.yaml::limits.upper", "reads_config"),
    }


# ---------------------------------------------------------------------------
# YAML pipeline-manifest extractor
# ---------------------------------------------------------------------------


def test_yaml_extractor_declared_writes(tmp_path: Path) -> None:
    manifest = tmp_path / "pipeline.yaml"
    manifest.write_text(
        textwrap.dedent(
            '''
            sources:
              orders_feed:
                module: pipeline.data_source
                entrypoint: fetch
                table: orders
            groups:
              totals:
                module: analytics.rollups
                entrypoint: build
                target_table: order_totals
            '''
        )
    )
    edges = extract_yaml_edges(manifest, repo_root=tmp_path)
    assert [(e.src, e.dst, e.edge_type, e.lineno) for e in edges] == [
        ("pipeline/data_source.py:fetch", "orders", "writes_table", 0),
        ("analytics/rollups.py:build", "order_totals", "writes_table", 0),
    ]
    assert all(e.evidence.startswith("pipeline.yaml:") for e in edges)


def test_yaml_extractor_custom_schema(tmp_path: Path) -> None:
    manifest = tmp_path / "pipeline.yaml"
    manifest.write_text(
        textwrap.dedent(
            '''
            ingestion_sources:
              orders_feed:
                module: pipeline.data_source
                entrypoint: fetch
                table: orders
            '''
        )
    )
    # A project-specific schema maps its own section names to table keys.
    edges = extract_yaml_edges(
        manifest, repo_root=tmp_path, schema={"ingestion_sources": "table"}
    )
    assert [(e.src, e.dst, e.edge_type) for e in edges] == [
        ("pipeline/data_source.py:fetch", "orders", "writes_table")
    ]
    # Sections outside the configured schema contribute nothing.
    assert extract_yaml_edges(manifest, repo_root=tmp_path) == []


def test_yaml_extractor_missing_file(tmp_path: Path) -> None:
    assert extract_yaml_edges(tmp_path / "absent.yaml", repo_root=tmp_path) == []


# ---------------------------------------------------------------------------
# Call-graph extractor
# ---------------------------------------------------------------------------


def test_call_extractor_resolves_cross_module_calls(demo_pkg: Path) -> None:
    files = sorted(demo_pkg.rglob("*.py"))
    idx = build_symbol_index(demo_pkg, files)

    all_edges = []
    for path in files:
        rel = path.relative_to(demo_pkg).as_posix()
        all_edges.extend(extract_call_edges(path, rel, idx))

    calls = {(e.src, e.dst) for e in all_edges if e.edge_type == "calls"}
    assert calls == {
        ("pipeline.py:run", "pipeline.py:Reporter"),
        ("pipeline.py:run", "queries.py:archive_orders"),
        ("pipeline.py:Reporter", "settings.py:get_threshold"),
        ("settings.py:get_threshold", "loaders.py:load_app_config"),
        ("settings.py:get_mode", "loaders.py:load_app_config"),
    }
    # Every emitted edge is attributed to the owning top-level symbol.
    assert all(e.src_kind == "symbol" for e in all_edges)


def test_flatten_string_node_handles_fstrings() -> None:
    import ast

    node = ast.parse('f"a{value}b"', mode="eval").body
    assert flatten_string_node(node) == "a b"
