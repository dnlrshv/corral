"""Extract declared producer→table edges from the pipeline manifest YAML.

The pipeline manifest (``lineage.pipeline_yaml`` in ``corral.yaml``,
default ``config/data_pipeline.yaml``) is the deterministic source of truth
for which symbol owns which table. We emit one ``writes_table`` edge per
declared entrypoint in every section listed in the manifest schema
(``lineage.yaml_manifest_schema`` in ``corral.yaml``), which maps
section-name -> table-key (default: ``{sources: table, groups:
target_table}``).

YAML line numbers are not preserved — :func:`yaml.safe_load` discards them
and the manifest schema does not embed lineno hints. ``lineno`` is therefore
``0`` for YAML-sourced edges (sentinel meaning "configured, not in code").
The ``evidence`` column points back to the config section so a reader can
trivially open the YAML to see the full declaration.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from corral.config import DEFAULT_YAML_MANIFEST_SCHEMA

from .schema import Edge


def _module_to_path(module: str) -> str:
    """Convert ``pkg.module_name`` to ``pkg/module_name.py``.

    We do not check that the file exists on disk — the lineage extractor
    runs in environments (CI, isolated worktrees) where modules may legitimately
    be missing, and the path is the canonical reference regardless.
    """

    return module.replace(".", "/") + ".py"


def _yaml_writes_table_edge(
    *,
    module: str,
    entrypoint: str,
    table: str,
    evidence: str,
) -> Edge:
    return Edge(
        src_kind="symbol",
        src=f"{_module_to_path(module)}:{entrypoint}",
        dst_kind="table",
        dst=table,
        edge_type="writes_table",
        lineno=0,
        evidence=evidence,
    )


def _emit_section_edges(
    section_name: str,
    section: dict[str, Any] | None,
    *,
    table_key: str,
    config_path_posix: str,
) -> list[Edge]:
    if not section:
        return []
    edges: list[Edge] = []
    for entry_name, entry in section.items():
        if not isinstance(entry, dict):
            continue
        module = entry.get("module")
        entrypoint = entry.get("entrypoint")
        table = entry.get(table_key)
        if not (isinstance(module, str) and isinstance(entrypoint, str) and isinstance(table, str)):
            continue
        edges.append(
            _yaml_writes_table_edge(
                module=module,
                entrypoint=entrypoint,
                table=table,
                evidence=f"{config_path_posix}:{section_name}.{entry_name}",
            )
        )
    return edges


def extract_yaml_edges(
    config_path: Path,
    *,
    repo_root: Path,
    schema: Mapping[str, str] | None = None,
) -> list[Edge]:
    """Return ``writes_table`` edges declared in the pipeline manifest YAML.

    ``schema`` maps manifest section-name -> table-key and defaults to
    ``DEFAULT_YAML_MANIFEST_SCHEMA``. Returns an empty list when the file is
    missing — callers can compose multiple extractors without a probe step.
    """

    if not config_path.is_file():
        return []
    document = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(document, dict):
        return []

    try:
        evidence_path = config_path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        evidence_path = config_path.as_posix()

    sections = DEFAULT_YAML_MANIFEST_SCHEMA if schema is None else schema

    edges: list[Edge] = []
    for section_name, table_key in sections.items():
        edges.extend(
            _emit_section_edges(
                section_name,
                document.get(section_name),
                table_key=table_key,
                config_path_posix=evidence_path,
            )
        )
    return edges
