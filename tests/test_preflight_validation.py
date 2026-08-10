"""Post-validation of LLM briefs: hallucination drops + recognized modules."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from corral.preflight import brief
from corral.preflight.brief_validation import (
    BriefQualityError,
    apply_semantic_validation,
    recognized_modules_from_code_map,
)

SURFACES_YAML = """
surfaces:
  real-surface:
    paths: [src/real.py]
  other-surface:
    paths: [src/other.py]
"""


def make_brief(**overrides) -> dict:
    base = {
        "files_to_touch": [],
        "files_to_read_only": [],
        "surfaces_in_scope": [],
        "cross_cutting_concerns": [],
        "recent_related_prs": [],
        "invariants_to_preserve": [],
        "test_files": [],
        "estimated_blast_radius": "low",
        "do_not_touch": [],
    }
    base.update(overrides)
    return base


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "real.py").write_text("VALUE = 1\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "util.py").write_text("def helper():\n    return 1\n")
    return tmp_path


def test_drops_unknown_paths_and_surfaces(repo: Path) -> None:
    validated = make_brief(
        files_to_touch=[
            "src/real.py",
            "tests/util.py",
            "src/ghost.py",
            "../outside.py",
            "/abs.py",
        ],
        files_to_read_only=["src/real.py", "tests/util.py", "docs/missing.md"],
        surfaces_in_scope=["real-surface", "hallucinated-surface"],
        recent_related_prs=["#1023", "PR 42", "not-a-ref"],
    )

    # Valid paths outnumber invalid ones, so the quality gate does not trip
    # and the drops below are observable.
    apply_semantic_validation(validated, SURFACES_YAML, repo, frozenset({"src", "tests"}))

    assert validated["files_to_touch"] == ["src/real.py", "tests/util.py"]
    assert validated["files_to_read_only"] == ["src/real.py", "tests/util.py"]
    assert validated["surfaces_in_scope"] == ["real-surface"]
    assert validated["recent_related_prs"] == ["#1023"]


def test_test_files_creation_bounded_by_recognized_modules(repo: Path) -> None:
    validated = make_brief(
        test_files=[
            "src/test_real.py",  # existing dir, recognized module -> allowed
            "tests/test_new.py",  # existing dir, recognized module -> allowed
            "brand_new/test_x.py",  # unrecognized top-level module -> dropped
            "src/deep/nested/test_y.py",  # parent dir does not exist -> dropped
        ]
    )

    apply_semantic_validation(validated, SURFACES_YAML, repo, frozenset({"src", "tests"}))

    assert validated["test_files"] == ["src/test_real.py", "tests/test_new.py"]


def test_quality_error_when_majority_of_paths_hallucinated(repo: Path) -> None:
    validated = make_brief(
        files_to_touch=["src/ghost1.py", "src/ghost2.py"],
        files_to_read_only=["src/real.py"],
    )
    with pytest.raises(BriefQualityError):
        apply_semantic_validation(validated, SURFACES_YAML, repo, frozenset({"src"}))


def test_symlinked_paths_are_rejected(repo: Path) -> None:
    (repo / "link.py").symlink_to(repo / "src" / "real.py")
    validated = make_brief(files_to_read_only=["link.py", "src/real.py"])

    apply_semantic_validation(validated, SURFACES_YAML, repo, frozenset({"src"}))

    assert validated["files_to_read_only"] == ["src/real.py"]


def test_recognized_modules_derived_from_code_map_artifacts(tmp_path: Path) -> None:
    from corral.codemap.build import build_code_map_with_cache

    (tmp_path / "pkg_a").mkdir()
    (tmp_path / "pkg_a" / "mod.py").write_text("def alpha():\n    return 1\n")
    (tmp_path / "pkg_b").mkdir()
    (tmp_path / "pkg_b" / "mod.py").write_text("def beta():\n    return 2\n")
    (tmp_path / "root_level.py").write_text("def gamma():\n    return 3\n")
    build_code_map_with_cache(
        tmp_path, tmp_path / "code_map", use_cache=False, scan_dirs=["."], skip_dirs=[]
    )

    modules = recognized_modules_from_code_map(tmp_path / "code_map")

    # Top-level directories only; root-level files contribute nothing.
    assert modules == frozenset({"pkg_a", "pkg_b"})


def test_recognized_modules_missing_artifacts(tmp_path: Path) -> None:
    assert recognized_modules_from_code_map(tmp_path / "nope") == frozenset()


def test_fallback_brief_uses_mentioned_paths_for_gotcha_matching(tmp_path: Path) -> None:
    # The fallback path matches gotchas against task-mentioned paths.
    gotchas_path = tmp_path / "gotchas.json"
    gotchas_path.write_text(
        json.dumps(
            {
                "gotchas": [
                    {
                        "id": "G-2025-001",
                        "rule": "Watch the query module.",
                        "workflow_kinds": [],
                        "repo_paths": ["src/*.py"],
                        "surface_ids": [],
                        "source_prs": [],
                        "control_type": "prompt_only",
                        "control_pr": None,
                        "control_path": None,
                        "inject_into_briefer": True,
                        "created": "2025-01-01",
                        "expires": None,
                    }
                ]
            }
        )
    )
    issue = {"title": "Interactive task", "body": "Refactor src/real.py today."}

    fallback = brief.generate_fallback_brief(
        issue, SURFACES_YAML, gotchas_path=gotchas_path
    )

    assert [entry["id"] for entry in fallback["agent_gotchas"]] == ["G-2025-001"]
    assert fallback["files_to_read_only"] == ["src/real.py"]
