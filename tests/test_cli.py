"""End-to-end tests for the `corral` CLI (in-process via corral.cli.main)."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from corral.cli import main

from .conftest import DEMO_LOADERS, edge_tuples, read_parquet_rows


def write_config(path: Path, loaders: dict[str, str] | None = None, extra: str = "") -> Path:
    loader_lines = "\n".join(f"    {name}: {target}" for name, target in (loaders or {}).items())
    body = "lineage:\n  config_loaders:\n" + (loader_lines or "    {}") + "\n"
    path.write_text(body + extra)
    return path


def test_codemap_build_cli(demo_pkg: Path, tmp_path: Path) -> None:
    out = tmp_path / "code_map"
    rc = main(
        ["codemap", "build", "--root", str(demo_pkg), "--output-dir", str(out), "--no-cache"]
    )
    assert rc == 0
    assert (out / "imports.parquet").is_file()
    assert (out / "symbols.parquet").is_file()


def test_lineage_build_cli_honours_config_loaders(demo_pkg: Path, tmp_path: Path) -> None:
    config_path = write_config(tmp_path / "corral.yaml", DEMO_LOADERS)
    out = tmp_path / "code_map"
    rc = main(
        [
            "lineage",
            "build",
            "--root",
            str(demo_pkg),
            "--output-dir",
            str(out),
            "--config",
            str(config_path),
        ]
    )
    assert rc == 0

    rows = read_parquet_rows(out / "edges.parquet")
    tuples = edge_tuples(rows)
    # Declared loader → reads_config edges appear.
    assert (
        "symbol",
        "settings.py:get_threshold",
        "config",
        "config/app.yaml::threshold",
        "reads_config",
    ) in tuples
    # Unknown loader (load_secrets) never produces edges.
    assert all("token" not in t[3] for t in tuples)


def test_lineage_build_cli_without_loaders(demo_pkg: Path, tmp_path: Path) -> None:
    config_path = write_config(tmp_path / "corral.yaml", loaders=None)
    out = tmp_path / "code_map"
    rc = main(
        [
            "lineage",
            "build",
            "--root",
            str(demo_pkg),
            "--output-dir",
            str(out),
            "--config",
            str(config_path),
        ]
    )
    assert rc == 0
    rows = read_parquet_rows(out / "edges.parquet")
    assert all(r["edge_type"] != "reads_config" for r in rows)


def test_lineage_build_cli_output_default_from_config(
    demo_pkg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # corral.yaml lives next to the fixture, so root and the output default
    # both derive from it when --root/--output-dir are omitted.
    (demo_pkg / "corral.yaml").write_text(
        "lineage:\n  output: custom/edges.parquet\n"
    )
    monkeypatch.chdir(demo_pkg)

    rc = main(["lineage", "build", "--config", "corral.yaml"])

    assert rc == 0
    assert (demo_pkg / "custom" / "edges.parquet").is_file()


def test_codemap_query_cli_via_corral_entrypoint(demo_pkg: Path, tmp_path: Path) -> None:
    pytest.importorskip("networkx")
    out = tmp_path / "code_map"
    assert main(
        ["codemap", "build", "--root", str(demo_pkg), "--output-dir", str(out), "--no-cache"]
    ) == 0
    config_path = write_config(tmp_path / "corral.yaml", DEMO_LOADERS)
    assert (
        main(
            [
                "lineage",
                "build",
                "--root",
                str(demo_pkg),
                "--output-dir",
                str(out),
                "--config",
                str(config_path),
            ]
        )
        == 0
    )

    rc = main(
        [
            "codemap",
            "query",
            "--root",
            str(demo_pkg),
            "--code-map-dir",
            str(out),
            "lineage",
            "orders_archive",
        ]
    )
    assert rc == 0


def test_version_flag(capsys) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    assert "corral 0.1.0.dev0" in capsys.readouterr().out


def test_magic_numbers_hook_cli_skips_without_constants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.chdir(tmp_path)

    assert main(["hooks", "magic-numbers", "--root", str(tmp_path)]) == 0
    assert "constants-membership checks skipped" in capsys.readouterr().out
