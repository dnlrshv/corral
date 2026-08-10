"""Regression tests for concise instruction-governance input errors."""

from __future__ import annotations

from pathlib import Path

import pytest

from corral.cli import main


VALID_REGISTRY = """\
schema_version: 1
rules:
  R-TEST-001:
    file: AGENTS.md
    anchor: Changes MUST be reviewed.
    concern_key: review
    modality: MUST
    review_by: maintainers
"""


@pytest.mark.parametrize(
    ("registry", "config", "expected"),
    [
        pytest.param(
            "schema_version: 1\nrules: []\n",
            "",
            "'rules' must be a non-empty mapping",
            id="malformed-rules",
        ),
        pytest.param("rules: [\n", "", "while parsing", id="invalid-yaml"),
        pytest.param(
            VALID_REGISTRY,
            "governance:\n  rule_id_pattern: '['\n",
            "configured rule_id_pattern is invalid",
            id="bad-rule-id-pattern",
        ),
    ],
)
def test_governance_check_input_errors_are_one_line_without_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    registry: str,
    config: str,
    expected: str,
) -> None:
    (tmp_path / "instruction_rules.yaml").write_text(registry, encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("Changes MUST be reviewed.\n", encoding="utf-8")
    argv = ["governance", "check", "--root", str(tmp_path)]
    if config:
        config_path = tmp_path / "corral.yaml"
        config_path.write_text(config, encoding="utf-8")
        argv.extend(["--config", str(config_path)])

    assert main(argv) == 2
    error = capsys.readouterr().err
    assert error.startswith("instruction-governance input error:")
    assert expected in error
    assert len(error.splitlines()) == 1
    assert "Traceback" not in error
