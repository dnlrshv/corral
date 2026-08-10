"""Tests for corral.config (corral.yaml loading and defaults)."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from corral.config import find_config_file, load_config


def test_defaults_when_no_config_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = load_config()
    assert cfg.source_path is None
    assert cfg.root == Path.cwd()
    assert cfg.codemap.output_dir == "code_map"
    assert cfg.codemap.scan_dirs == ["."]
    assert cfg.codemap.skip_dirs == [".venv", "data", "tests/fixtures", ".claude/worktrees"]
    assert cfg.lineage.output == "code_map/edges.parquet"
    assert cfg.lineage.output_configured is False
    assert cfg.lineage.pipeline_yaml == "config/data_pipeline.yaml"
    assert cfg.lineage.yaml_manifest_schema == {"sources": "table", "groups": "target_table"}
    assert cfg.lineage.config_loaders == {}
    assert cfg.lineage.config_loader_key_prefixes == {}
    assert cfg.hooks.surfaces == "surfaces.yaml"
    assert cfg.hooks.magic_numbers.constants is None
    assert cfg.hooks.magic_numbers.allowlist == ".magic-number-allowlist.yaml"
    assert cfg.hooks.magic_numbers.scan_dirs is None


def test_explicit_path_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "absent.yaml")


def test_load_config_overrides(tmp_path: Path) -> None:
    config_path = tmp_path / "corral.yaml"
    config_path.write_text(
        textwrap.dedent(
            '''
            codemap:
              output_dir: generated/map
              scan_dirs: ["src", "scripts"]
              skip_dirs: ["vendor"]
            lineage:
              output: generated/map/edges.parquet
              pipeline_yaml: config/pipelines.yaml
              yaml_manifest_schema:
                ingestion_sources: table
              config_loaders:
                load_app_config: config/app.yaml
              config_loader_key_prefixes:
                load_metric_defs: metrics
            hooks:
              surfaces: policy/surfaces.yaml
              magic_numbers:
                constants: src/app/constants.py
                allowlist: config/magic-number-allowlist.yaml
                scan_dirs: ["src", "tools"]
            '''
        )
    )
    cfg = load_config(config_path)
    assert cfg.source_path == config_path.resolve()
    assert cfg.root == tmp_path
    assert cfg.codemap.output_dir == "generated/map"
    assert cfg.codemap.scan_dirs == ["src", "scripts"]
    assert cfg.codemap.skip_dirs == ["vendor"]
    assert cfg.lineage.output == "generated/map/edges.parquet"
    assert cfg.lineage.output_configured is True
    assert cfg.lineage.pipeline_yaml == "config/pipelines.yaml"
    assert cfg.lineage.yaml_manifest_schema == {"ingestion_sources": "table"}
    assert cfg.lineage.config_loaders == {"load_app_config": "config/app.yaml"}
    assert cfg.lineage.config_loader_key_prefixes == {"load_metric_defs": "metrics"}
    assert cfg.hooks.surfaces == "policy/surfaces.yaml"
    assert cfg.hooks.magic_numbers.constants == "src/app/constants.py"
    assert cfg.hooks.magic_numbers.allowlist == "config/magic-number-allowlist.yaml"
    assert cfg.hooks.magic_numbers.scan_dirs == ["src", "tools"]


def test_partial_config_keeps_defaults(tmp_path: Path) -> None:
    config_path = tmp_path / "corral.yaml"
    config_path.write_text("codemap:\n  output_dir: out\n")
    cfg = load_config(config_path)
    assert cfg.codemap.output_dir == "out"
    assert cfg.codemap.scan_dirs == ["."]
    assert cfg.lineage.config_loaders == {}


def test_find_config_file_walks_up(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "corral.yaml").write_text("codemap: {}\n")
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    assert find_config_file() == tmp_path / "corral.yaml"
    assert load_config().root == tmp_path


def test_bad_types_raise_value_error(tmp_path: Path) -> None:
    config_path = tmp_path / "corral.yaml"
    config_path.write_text("codemap:\n  scan_dirs: 'not-a-list'\n")
    with pytest.raises(ValueError, match="codemap.scan_dirs"):
        load_config(config_path)

    config_path.write_text("lineage:\n  config_loaders: [1, 2]\n")
    with pytest.raises(ValueError, match="lineage.config_loaders"):
        load_config(config_path)


def test_unknown_keys_are_ignored(tmp_path: Path) -> None:
    config_path = tmp_path / "corral.yaml"
    config_path.write_text("future_section:\n  anything: true\ncodemap:\n  unknown: 1\n")
    cfg = load_config(config_path)
    assert cfg.codemap.output_dir == "code_map"


def test_falsey_but_invalid_document_rejected(tmp_path: Path) -> None:
    config_path = tmp_path / "corral.yaml"
    for body in ("[]", "false", "0", "just a string"):
        config_path.write_text(body)
        with pytest.raises(ValueError, match="top level"):
            load_config(config_path)


def test_falsey_but_invalid_section_rejected(tmp_path: Path) -> None:
    config_path = tmp_path / "corral.yaml"
    for section in ("codemap", "lineage", "hooks"):
        for value in ("[]", "false", "0"):
            config_path.write_text(f"{section}: {value}\n")
            with pytest.raises(ValueError, match=section):
                load_config(config_path)


def test_empty_document_and_sections_still_default(tmp_path: Path) -> None:
    config_path = tmp_path / "corral.yaml"
    # An empty file and present-but-empty sections are legitimate.
    for body in ("", "codemap:\n", "codemap: {}\nlineage:\n"):
        config_path.write_text(body)
        cfg = load_config(config_path)
        assert cfg.codemap.output_dir == "code_map"
        assert cfg.lineage.output == "code_map/edges.parquet"


def test_bad_magic_number_config_types_raise(tmp_path: Path) -> None:
    config_path = tmp_path / "corral.yaml"
    config_path.write_text("hooks:\n  magic_numbers: []\n")
    with pytest.raises(ValueError, match="magic_numbers"):
        load_config(config_path)

    config_path.write_text("hooks:\n  magic_numbers:\n    constants: 42\n")
    with pytest.raises(ValueError, match="hooks.magic_numbers.constants"):
        load_config(config_path)


def test_retro_root_incident_floor_cannot_be_lowered(tmp_path: Path) -> None:
    config_path = tmp_path / "corral.yaml"
    config_path.write_text("retro:\n  evidence:\n    min_root_incidents: 1\n")
    with pytest.raises(ValueError, match=r"min_root_incidents must be >= 2"):
        load_config(config_path)
