"""Tests for corral.lineage.build over the demo_pkg fixture."""

from __future__ import annotations

import textwrap
from pathlib import Path

from corral.lineage.build import (
    OUTPUT_FILENAME,
    build_and_write_lineage,
    build_lineage_edges,
)

from .conftest import DEMO_LOADERS, edge_tuples, read_parquet_rows


def _rows(edges) -> list[dict]:
    return [
        {
            "src_kind": e.src_kind,
            "src": e.src,
            "dst_kind": e.dst_kind,
            "dst": e.dst,
            "edge_type": e.edge_type,
        }
        for e in edges
    ]


def test_build_lineage_edges_with_config_loaders(demo_pkg: Path) -> None:
    edges = build_lineage_edges(demo_pkg, config_loaders=DEMO_LOADERS)
    assert edge_tuples(_rows(edges)) == {
        # Call-graph extractor
        ("symbol", "pipeline.py:run", "symbol", "pipeline.py:Reporter", "calls"),
        ("symbol", "pipeline.py:run", "symbol", "queries.py:archive_orders", "calls"),
        ("symbol", "pipeline.py:Reporter", "symbol", "settings.py:get_threshold", "calls"),
        ("symbol", "settings.py:get_threshold", "symbol", "loaders.py:load_app_config", "calls"),
        ("symbol", "settings.py:get_mode", "symbol", "loaders.py:load_app_config", "calls"),
        # SQL extractor
        ("module", "queries.py", "table", "orders", "reads_table"),
        ("symbol", "queries.py:archive_orders", "table", "orders_archive", "writes_table"),
        ("symbol", "queries.py:archive_orders", "table", "orders", "reads_table"),
        # File I/O extractor
        ("symbol", "pipeline.py:run", "file", "out/report.txt", "writes_file"),
        ("symbol", "pipeline.py:run", "file", "out/report.txt", "reads_file"),
        # Config extractor (via the corral.yaml loader mapping)
        ("symbol", "settings.py:get_threshold", "config", "config/app.yaml::threshold", "reads_config"),
        ("symbol", "settings.py:get_mode", "config", "config/app.yaml::mode", "reads_config"),
    }


def test_build_lineage_edges_without_loaders_has_no_config_edges(demo_pkg: Path) -> None:
    edges = build_lineage_edges(demo_pkg)
    assert all(e.edge_type != "reads_config" for e in edges)


def test_build_lineage_edges_is_sorted_and_unique(demo_pkg: Path) -> None:
    edges = build_lineage_edges(demo_pkg, config_loaders=DEMO_LOADERS)
    keys = [(e.src_kind, e.src, e.dst_kind, e.dst, e.edge_type, e.lineno, e.evidence) for e in edges]
    assert keys == sorted(keys)
    assert len(keys) == len(set(keys))


def test_write_edges_parquet(demo_pkg: Path, tmp_path: Path) -> None:
    output_dir = tmp_path / "code_map"
    target = build_and_write_lineage(demo_pkg, output_dir, config_loaders=DEMO_LOADERS)

    assert target == output_dir / OUTPUT_FILENAME
    assert target.is_file()

    rows = read_parquet_rows(target)
    assert ("symbol", "settings.py:get_threshold", "config", "config/app.yaml::threshold", "reads_config") in {
        (r["src_kind"], r["src"], r["dst_kind"], r["dst"], r["edge_type"]) for r in rows
    }


def test_write_edges_is_byte_identical_across_runs(demo_pkg: Path, tmp_path: Path) -> None:
    first = build_and_write_lineage(demo_pkg, tmp_path / "a", config_loaders=DEMO_LOADERS)
    second = build_and_write_lineage(demo_pkg, tmp_path / "b", config_loaders=DEMO_LOADERS)
    assert first.read_bytes() == second.read_bytes()


def test_pipeline_yaml_contributes_writes_edges(demo_pkg: Path, tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        textwrap.dedent(
            '''
            sources:
              orders_feed:
                module: pipeline.data_source
                entrypoint: fetch
                table: orders
            '''
        )
    )
    edges = build_lineage_edges(demo_pkg, pipeline_yaml=manifest)
    # The manifest lives outside the scanned root, so its evidence path is
    # absolute; match on the file name instead.
    yaml_edges = [e for e in edges if "manifest.yaml:" in e.evidence]
    assert [(e.src, e.dst, e.edge_type) for e in yaml_edges] == [
        ("pipeline/data_source.py:fetch", "orders", "writes_table")
    ]


def test_default_pipeline_yaml_location(demo_pkg: Path) -> None:
    # config/data_pipeline.yaml does not exist in the fixture, so the
    # default location simply contributes nothing instead of failing.
    edges = build_lineage_edges(demo_pkg)
    assert all(not e.evidence.endswith("data_pipeline.yaml") for e in edges)


def test_build_lineage_edges_honours_yaml_manifest_schema(demo_pkg: Path, tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.yaml"
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
    # Default schema does not know ingestion_sources...
    edges = build_lineage_edges(demo_pkg, pipeline_yaml=manifest)
    assert all("manifest.yaml:" not in e.evidence for e in edges)
    # ...but a matching schema plumbed through build_lineage_edges does.
    edges = build_lineage_edges(
        demo_pkg,
        pipeline_yaml=manifest,
        yaml_manifest_schema={"ingestion_sources": "table"},
    )
    yaml_edges = [e for e in edges if "manifest.yaml:" in e.evidence]
    assert [(e.src, e.dst, e.edge_type) for e in yaml_edges] == [
        ("pipeline/data_source.py:fetch", "orders", "writes_table")
    ]
