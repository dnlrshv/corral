"""Fix-up PR mining: PRs that touch files from a recent merged agent PR.

Port of the source ``find_fixup_prs.py`` pairing logic.  A "fix-up pair" is
an agent-authored PR plus a later PR (within :data:`FIXUP_WINDOW_DAYS`) that
touches at least one of the same files -- a co-editing correlation used as a
LEAD for defect mining, never proof on its own.

GitHub access goes through :class:`corral.retro.github.GitHubClient`; the
pairing itself is pure and operates on ``gh pr list --json``-shaped rows.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from datetime import datetime
from typing import Any

import pyarrow as pa

from corral.retro.github import GitHubClient

FIXUP_WINDOW_DAYS = 7

SCHEMA = pa.schema(
    [
        ("original_pr", pa.int64()),
        ("original_author", pa.string()),
        ("original_merged_at", pa.timestamp("us", tz="UTC")),
        ("fixup_pr", pa.int64()),
        ("fixup_author", pa.string()),
        ("fixup_merged_at", pa.timestamp("us", tz="UTC")),
        ("days_between", pa.float64()),
        ("shared_files", pa.list_(pa.string())),
        ("agent", pa.string()),
        ("area", pa.string()),
        ("original_cycle_time_days", pa.float64()),
    ]
)

AGENTS = ("claude", "codex", "dependabot")

ROOT_DOCS = {
    "AGENTS.md",
    "CLAUDE.md",
    "README.md",
}

ROOT_TOOLING = {
    "poetry.lock",
    "pyproject.toml",
    "ruff.toml",
}

# Default top-level-directory -> area labels for fix-up classification.
# Adopters with a different layout can extend or replace this mapping;
# unmatched directories classify as "other".
AREA_BY_TOP_LEVEL = {
    ".github": "agent-ops",
    "agent_memory": "agent-ops",
    "agent_telemetry": "agent-ops",
    "docs": "docs",
    "wiki": "docs",
    "config": "config",
    "src": "src",
    "scripts": "scripts",
    "tests": "tests",
}


def classify_agent(login: str) -> str:
    lower = login.lower()
    if "claude" in lower:
        return "claude"
    if "codex" in lower:
        return "codex"
    if "dependabot" in lower:
        return "dependabot"
    return "human"


def _label_names(labels: list[dict[str, Any]] | list[str] | None) -> set[str]:
    names: set[str] = set()
    for label in labels or []:
        name = label.get("name") if isinstance(label, dict) else label
        if name:
            names.add(str(name).lower())
    return names


def classify_pr_agent(pr: dict[str, Any]) -> str:
    """Classify a PR's agent even when automation uses a human-owned token."""
    author_login = str(pr.get("author", {}).get("login", ""))
    agent = classify_agent(author_login)
    if agent != "human":
        return agent

    head_ref = str(pr.get("headRefName") or pr.get("head_ref") or "").lower()
    labels = _label_names(pr.get("labels"))
    if head_ref.startswith("codex/"):
        return "codex"
    if head_ref.startswith("claude/") or any(
        label.startswith("claude-fix") for label in labels
    ):
        return "claude"
    return "human"


def classify_area(path: str) -> str:
    """Map a repository path to a coarse area for mining stratification."""
    if path in ROOT_DOCS:
        return "docs"
    if path in ROOT_TOOLING:
        return "tooling"
    first_part = path.split("/", maxsplit=1)[0]
    return AREA_BY_TOP_LEVEL.get(first_part, "other")


def classify_pr_area(files: list[dict[str, Any]]) -> str:
    """Return the dominant area touched by a PR, breaking ties deterministically."""
    if not files:
        return "unknown"
    areas = Counter(classify_area(str(f["path"])) for f in files)
    return sorted(areas.items(), key=lambda item: (-item[1], item[0]))[0][0]


def _parse_merged_at(ts: str) -> datetime:
    return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))


def _cycle_time_days(pr: dict[str, Any]) -> float | None:
    created_at = pr.get("createdAt")
    merged_at = pr.get("mergedAt")
    if not created_at or not merged_at:
        return None
    created = _parse_merged_at(created_at)
    merged = _parse_merged_at(merged_at)
    return round((merged - created).total_seconds() / 86400, 4)


def fetch_merged_prs(client: GitHubClient, since: str, until: str) -> list[dict[str, Any]]:
    """Fetch merged PRs in the window through the injected GitHub client."""
    return client.merged_prs(since, until)


def find_fixup_pairs(prs: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return fixup rows: each row is an (agent PR, later PR touching same files) pair."""
    sorted_prs = sorted(prs, key=lambda p: p["mergedAt"])

    rows: list[dict[str, Any]] = []
    for i, p1 in enumerate(sorted_prs):
        p1_author = p1["author"]["login"]
        agent = classify_pr_agent(p1)
        if agent == "human":
            continue

        p1_files = {f["path"] for f in p1.get("files", [])}
        if not p1_files:
            continue

        p1_merged = _parse_merged_at(p1["mergedAt"])
        p1_area = classify_pr_area(p1.get("files", []))
        p1_cycle_time_days = _cycle_time_days(p1)

        for p2 in sorted_prs[i + 1 :]:
            p2_merged = _parse_merged_at(p2["mergedAt"])
            days = (p2_merged - p1_merged).total_seconds() / 86400
            if days > FIXUP_WINDOW_DAYS:
                break

            p2_files = {f["path"] for f in p2.get("files", [])}
            shared = sorted(p1_files & p2_files)
            if shared:
                rows.append(
                    {
                        "original_pr": p1["number"],
                        "original_author": p1_author,
                        "original_merged_at": p1_merged,
                        "fixup_pr": p2["number"],
                        "fixup_author": p2["author"]["login"],
                        "fixup_merged_at": p2_merged,
                        "days_between": round(days, 4),
                        "shared_files": shared,
                        "agent": agent,
                        "area": p1_area,
                        "original_cycle_time_days": p1_cycle_time_days,
                    }
                )

    return rows


def build_table(rows: Sequence[dict[str, Any]]) -> pa.Table:
    if not rows:
        return pa.table(
            {col: pa.array([], type=SCHEMA.field(col).type) for col in SCHEMA.names},
            schema=SCHEMA,
        )
    return pa.table(
        {
            "original_pr": pa.array([r["original_pr"] for r in rows], type=pa.int64()),
            "original_author": pa.array([r["original_author"] for r in rows]),
            "original_merged_at": pa.array(
                [r["original_merged_at"] for r in rows],
                type=pa.timestamp("us", tz="UTC"),
            ),
            "fixup_pr": pa.array([r["fixup_pr"] for r in rows], type=pa.int64()),
            "fixup_author": pa.array([r["fixup_author"] for r in rows]),
            "fixup_merged_at": pa.array(
                [r["fixup_merged_at"] for r in rows],
                type=pa.timestamp("us", tz="UTC"),
            ),
            "days_between": pa.array([r["days_between"] for r in rows], type=pa.float64()),
            "shared_files": pa.array([r["shared_files"] for r in rows], type=pa.list_(pa.string())),
            "agent": pa.array([r["agent"] for r in rows]),
            "area": pa.array([r["area"] for r in rows]),
            "original_cycle_time_days": pa.array(
                [r["original_cycle_time_days"] for r in rows],
                type=pa.float64(),
            ),
        },
        schema=SCHEMA,
    )


__all__ = [
    "AGENTS",
    "AREA_BY_TOP_LEVEL",
    "FIXUP_WINDOW_DAYS",
    "SCHEMA",
    "build_table",
    "classify_agent",
    "classify_area",
    "classify_pr_agent",
    "classify_pr_area",
    "fetch_merged_prs",
    "find_fixup_pairs",
]
