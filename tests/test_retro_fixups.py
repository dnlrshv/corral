from __future__ import annotations

from corral.retro.fixups import (
    SCHEMA,
    build_table,
    classify_area,
    classify_pr_agent,
    find_fixup_pairs,
)
from tests.retro_support import pr_row


def test_agent_classification_login_and_head_ref() -> None:
    assert classify_pr_agent(pr_row(1, author="claude-bot", merged_at="2026-08-01T00:00:00Z", files=["a"])) == "claude"
    assert classify_pr_agent(pr_row(1, author="dev", merged_at="2026-08-01T00:00:00Z", files=["a"], head_ref="codex/fix")) == "codex"
    assert classify_pr_agent(pr_row(1, author="dev", merged_at="2026-08-01T00:00:00Z", files=["a"], head_ref="claude/x")) == "claude"
    assert classify_pr_agent(pr_row(1, author="dev", merged_at="2026-08-01T00:00:00Z", files=["a"])) == "human"

    labeled = pr_row(1, author="dev", merged_at="2026-08-01T00:00:00Z", files=["a"])
    labeled["labels"] = [{"name": "claude-fix"}]
    assert classify_pr_agent(labeled) == "claude"

    # Any claude-fix* label variant (case-insensitive) classifies as claude.
    string_labeled = dict(labeled, labels=["CLAUDE-FIX-VARIANT"])
    assert classify_pr_agent(string_labeled) == "claude"


def test_area_classification() -> None:
    assert classify_area("AGENTS.md") == "docs"
    assert classify_area("pyproject.toml") == "tooling"
    assert classify_area("src/x.py") == "src"
    assert classify_area("features/x.py") == "other"
    assert classify_area("root-file.txt") == "other"


def test_find_fixup_pairs_window_and_shared_files() -> None:
    agent_pr = pr_row(
        10, author="codex-bot", merged_at="2026-08-01T00:00:00Z",
        files=["src/a.py", "src/b.py"], created_at="2026-07-31T00:00:00Z",
    )
    quick_fix = pr_row(
        11, author="dev", merged_at="2026-08-03T00:00:00Z", files=["src/a.py"]
    )
    late_fix = pr_row(
        12, author="dev", merged_at="2026-08-20T00:00:00Z", files=["src/a.py"]
    )
    disjoint = pr_row(
        13, author="dev", merged_at="2026-08-02T00:00:00Z", files=["other/c.py"]
    )
    human_pr = pr_row(
        14, author="human-dev", merged_at="2026-08-01T00:00:00Z", files=["src/a.py"]
    )
    rows = find_fixup_pairs([agent_pr, quick_fix, late_fix, disjoint, human_pr])
    # Any later PR touching the files counts as a fix-up (even human-authored);
    # the ORIGINAL must be agent-authored. Late fixes fall outside the window.
    assert [(r["original_pr"], r["fixup_pr"]) for r in rows] == [(10, 14), (10, 11)]
    assert rows[1]["shared_files"] == ["src/a.py"]
    assert rows[1]["agent"] == "codex"
    assert rows[1]["days_between"] == 2.0


def test_build_table_schema_roundtrip() -> None:
    empty = build_table([])
    assert empty.schema.equals(SCHEMA)
    assert empty.num_rows == 0

    rows = find_fixup_pairs(
        [
            pr_row(1, author="claude-x", merged_at="2026-08-01T00:00:00Z", files=["a.py"]),
            pr_row(2, author="dev", merged_at="2026-08-02T00:00:00Z", files=["a.py"]),
        ]
    )
    table = build_table(rows)
    assert table.num_rows == 1
    assert table.schema.equals(SCHEMA)
