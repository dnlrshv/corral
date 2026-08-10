"""Regression coverage for the runnable example documented in WALKTHROUGH.md."""

from __future__ import annotations

from pathlib import Path

import pyarrow.parquet as pq

from corral.cli import main
from corral.codemap.build import build_code_map_with_cache
from corral.lineage.build import build_and_write_lineage

from .conftest import REPO_ROOT
from .preflight_support import clean_preflight_env, parse_brief_output  # noqa: F401


DEMO_ROOT = REPO_ROOT / "examples" / "demo"


def test_demo_build_and_fallback_claims(tmp_path: Path, capsys) -> None:
    code_map = tmp_path / "code_map"
    build_code_map_with_cache(
        DEMO_ROOT,
        code_map,
        use_cache=False,
        scan_dirs=["acme_pipeline"],
        skip_dirs=["__pycache__"],
    )
    build_and_write_lineage(
        DEMO_ROOT,
        code_map,
        pipeline_yaml=DEMO_ROOT / "pipeline.yaml",
        scan_dirs=["acme_pipeline"],
        skip_dirs=["__pycache__"],
        config_loaders={"load_pipeline_config": "pipeline.yaml"},
        yaml_manifest_schema={"groups": "target_table"},
    )

    symbols = pq.read_table(code_map / "symbols.parquet").to_pylist()
    assert {
        "file": "acme_pipeline/pipeline.py",
        "symbol": "run_pipeline",
        "kind": "function",
        "lineno": 9,
        "is_public": True,
    } in symbols

    edges = pq.read_table(code_map / "edges.parquet").to_pylist()
    edge_keys = {
        (row["src"], row["dst"], row["edge_type"])
        for row in edges
    }
    assert (
        "acme_pipeline/pipeline.py:run_pipeline",
        "acme_pipeline/transform.py:build_statements",
        "calls",
    ) in edge_keys
    assert (
        "acme_pipeline/queries.py:rebuild_curated_orders",
        "curated_orders",
        "writes_table",
    ) in edge_keys

    rc = main(
        [
            "preflight",
            "--root",
            str(DEMO_ROOT),
            "--config",
            str(DEMO_ROOT / "corral.yaml"),
            "--code-map",
            str(code_map),
            "--task",
            "Change acme_pipeline/queries.py to add a curated-orders status filter",
        ]
    )
    assert rc == 0
    _, brief = parse_brief_output(capsys.readouterr().out)
    assert brief["preflight_status"] == "fallback"
    assert brief["surfaces_in_scope"] == ["curated-orders-sql"]
    assert brief["do_not_touch"] == ["acme_pipeline/queries.py"]

    walkthrough = (DEMO_ROOT / "WALKTHROUGH.md").read_text(encoding="utf-8")
    assert "Path length: 3 hop(s)" in walkthrough
    assert "preflight_status: fallback" in walkthrough
