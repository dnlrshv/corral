"""Release-QA contracts for runnable docs and dependency floors."""

from __future__ import annotations

from pathlib import Path

from corral.cli import main

ROOT = Path(__file__).resolve().parents[1]


def test_documented_governance_validation_uses_shipped_examples(capsys) -> None:
    args = ["--root", str(ROOT), "--config", str(ROOT / "examples/demo/corral.yaml")]

    assert main(["governance", "check", *args]) == 0
    assert main(["governance", "replay", *args]) == 0
    assert "Instruction retrieval-replay: clean" in capsys.readouterr().out


def test_hook_docs_install_pre_commit_prerequisite() -> None:
    for relative in ("README.md", "docs/adoption.md"):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "pip install pre-commit" in text
        assert text.index("pip install pre-commit") < text.index("pre-commit install")


def test_dependency_lower_bounds_are_declared() -> None:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    for requirement in (
        '"pyyaml>=6"',
        '"pyarrow>=14"',
        '"networkx>=3"',
        '"anthropic>=0.40"',
        '"jsonschema>=4"',
    ):
        assert requirement in text
