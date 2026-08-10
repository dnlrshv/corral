"""Tests for the telemetry section of corral.yaml."""

from __future__ import annotations

from pathlib import Path

from corral.config import TelemetryConfig, load_config


def test_telemetry_defaults() -> None:
    cfg = TelemetryConfig()
    assert cfg.spool_dir is None
    assert cfg.rollup_output_dir == "agent_telemetry"
    assert cfg.lookback_days == 7
    assert cfg.required_ci_contexts == ["lint", "test"]


def test_telemetry_overrides(tmp_path: Path) -> None:
    config = tmp_path / "corral.yaml"
    config.write_text(
        "telemetry:\n"
        "  spool_dir: /tmp/spool-x\n"
        "  rollup_output_dir: rollouts\n"
        "  lookback_days: 14\n"
        '  required_ci_contexts: ["ci", "lint"]\n',
        encoding="utf-8",
    )
    cfg = load_config(config)
    assert cfg.telemetry.spool_dir == "/tmp/spool-x"
    assert cfg.telemetry.rollup_output_dir == "rollouts"
    assert cfg.telemetry.lookback_days == 14
    assert cfg.telemetry.required_ci_contexts == ["ci", "lint"]


def test_telemetry_absent_section_uses_defaults(tmp_path: Path) -> None:
    config = tmp_path / "corral.yaml"
    config.write_text("codemap:\n  output_dir: cm\n", encoding="utf-8")
    cfg = load_config(config)
    assert cfg.telemetry == TelemetryConfig()
