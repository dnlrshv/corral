"""Build deterministic data-lineage edges as ``edges.parquet``.

The output is the connectivity layer that joins ``imports.parquet`` /
``symbols.parquet`` produced by :mod:`corral.codemap.build` — same
repo-relative ``file.py:symbol`` conventions, one row per directed edge.

Sources of truth (deterministic, no LLM):

1. The pipeline manifest YAML (``lineage.pipeline_yaml`` in ``corral.yaml``,
   default ``config/data_pipeline.yaml``) — declared
   ``module:entrypoint → table`` writes for the sections named by
   ``lineage.yaml_manifest_schema`` (section-name -> table-key).
2. AST + regex scan of every scanned Python file for table writes
   (``CREATE/INSERT/UPDATE/COPY/REPLACE/DELETE/DROP``) and table reads
   (``FROM`` / ``JOIN``).
3. AST scan for common file-I/O patterns (pandas ``read_parquet`` /
   ``to_parquet``, pyarrow ``read_table`` / ``write_table``, ``open(path,
   mode)`` and ``Path(path).read_text()`` etc.).
4. AST scan for calls to known config-loader functions
   (``lineage.config_loaders`` from ``corral.yaml``) and key accesses on
   their results.
5. AST scan for statically resolvable calls between project-local symbols.

Usage::

    corral lineage build --root . --output-dir code_map/

The CLI is intentionally identical in shape to ``corral codemap build`` so
automation can chain the two with the same wiring.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable, Mapping
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from corral.codemap.build import SKIP_DIRS, iter_python_files
from corral.config import DEFAULT_YAML_MANIFEST_SCHEMA

from .extract_calls import build_symbol_index, extract_call_edges
from .extract_config import extract_config_edges
from .extract_files import extract_file_edges
from .extract_sql import extract_sql_edges
from .extract_yaml import extract_yaml_edges
from .schema import EDGES_SCHEMA, Edge, sorted_edges

DEFAULT_PIPELINE_YAML = Path("config/data_pipeline.yaml")
OUTPUT_FILENAME = "edges.parquet"


def _python_edges(
    root: Path,
    *,
    scan_dirs: Iterable[str] = (".",),
    skip_dirs: Iterable[str] = SKIP_DIRS,
    config_loaders: Mapping[str, str] | None = None,
    loader_key_prefixes: Mapping[str, str] | None = None,
) -> list[Edge]:
    """Return call, SQL, file, and config edges from every scanned Python file."""

    scan_dirs = tuple(scan_dirs)
    skip_dirs = tuple(skip_dirs)
    files = list(iter_python_files(root, scan_dirs=scan_dirs, skip_dirs=skip_dirs))
    symbol_index = build_symbol_index(root, files)
    edges: list[Edge] = []
    for path in files:
        rel_path = path.relative_to(root).as_posix()
        edges.extend(extract_call_edges(path, rel_path, symbol_index))
        edges.extend(extract_sql_edges(path, rel_path))
        edges.extend(extract_file_edges(path, rel_path))
        edges.extend(
            extract_config_edges(
                path, rel_path, loaders=config_loaders, key_prefixes=loader_key_prefixes
            )
        )
    return edges


def build_lineage_edges(
    root: Path,
    *,
    pipeline_yaml: Path | None = None,
    scan_dirs: Iterable[str] = (".",),
    skip_dirs: Iterable[str] = SKIP_DIRS,
    config_loaders: Mapping[str, str] | None = None,
    loader_key_prefixes: Mapping[str, str] | None = None,
    yaml_manifest_schema: Mapping[str, str] | None = None,
) -> list[Edge]:
    """Compute the full set of deterministic lineage edges.

    ``pipeline_yaml`` defaults to ``<root>/config/data_pipeline.yaml`` so the
    caller does not need to know the manifest location. ``config_loaders``,
    ``loader_key_prefixes`` and ``yaml_manifest_schema`` mirror the
    ``lineage`` keys of ``corral.yaml``; the loaders default to empty
    mappings and the manifest schema to ``DEFAULT_YAML_MANIFEST_SCHEMA``.
    """

    scan_dirs = tuple(scan_dirs)
    skip_dirs = tuple(skip_dirs)
    root = root.resolve()
    yaml_path = (pipeline_yaml or (root / DEFAULT_PIPELINE_YAML)).resolve()
    schema = DEFAULT_YAML_MANIFEST_SCHEMA if yaml_manifest_schema is None else yaml_manifest_schema

    edges: list[Edge] = []
    edges.extend(extract_yaml_edges(yaml_path, repo_root=root, schema=schema))
    edges.extend(
        _python_edges(
            root,
            scan_dirs=scan_dirs,
            skip_dirs=skip_dirs,
            config_loaders=config_loaders,
            loader_key_prefixes=loader_key_prefixes,
        )
    )

    # De-duplicate exact rows so a literal repeated in two places does not
    # double-count, then return in canonical order for byte-identical output.
    unique = list(dict.fromkeys(edges))
    return sorted_edges(unique)


def _table_from_edges(edges: list[Edge]) -> pa.Table:
    columns = {field.name: [getattr(edge, field.name) for edge in edges] for field in EDGES_SCHEMA}
    return pa.table(columns, schema=EDGES_SCHEMA)


def write_edges(edges: list[Edge], output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / OUTPUT_FILENAME
    pq.write_table(
        _table_from_edges(edges),
        target,
        compression="zstd",
        use_dictionary=False,
        write_statistics=False,
    )
    return target


def build_and_write_lineage(
    root: Path,
    output_dir: Path,
    *,
    pipeline_yaml: Path | None = None,
    scan_dirs: Iterable[str] = (".",),
    skip_dirs: Iterable[str] = SKIP_DIRS,
    config_loaders: Mapping[str, str] | None = None,
    loader_key_prefixes: Mapping[str, str] | None = None,
    yaml_manifest_schema: Mapping[str, str] | None = None,
) -> Path:
    edges = build_lineage_edges(
        root,
        pipeline_yaml=pipeline_yaml,
        scan_dirs=scan_dirs,
        skip_dirs=skip_dirs,
        config_loaders=config_loaders,
        loader_key_prefixes=loader_key_prefixes,
        yaml_manifest_schema=yaml_manifest_schema,
    )
    return write_edges(edges, output_dir.resolve())


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="Repository or project root")
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="Directory that receives edges.parquet",
    )
    parser.add_argument(
        "--pipeline-yaml",
        type=Path,
        default=None,
        help="Path to the pipeline manifest YAML (defaults to <root>/config/data_pipeline.yaml)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    build_and_write_lineage(args.root, args.output_dir, pipeline_yaml=args.pipeline_yaml)


if __name__ == "__main__":
    main()
