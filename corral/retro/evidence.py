"""External evidence sources for the weekly retrospective.

Everything here is best-effort: a GitHub failure or a missing/malformed input
file degrades gracefully (empty string / skip) rather than aborting the whole
run -- losing one PR's diff excerpt should not stop the retrospective from
proposing other candidates.  All GitHub access goes through the injected
:class:`corral.retro.github.GitHubClient`.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from corral.retro.github import GitHubClient

MAX_DIFF_CHARS = 2000
MAX_REVIEW_CHARS = 1200
MAX_PR_EXCERPTS_PER_GROUP = 4
SESSION_LEARNING_GLOB = "session_learning*.json"


def fetch_pr_diff_excerpt(
    client: GitHubClient, pr_number: int, *, max_chars: int = MAX_DIFF_CHARS
) -> str:
    """Return a truncated PR diff patch, or ``""`` if it could not be fetched."""
    return client.pr_diff_excerpt(pr_number, max_chars=max_chars)


def fetch_pr_review_excerpt(
    client: GitHubClient, pr_number: int, *, max_chars: int = MAX_REVIEW_CHARS
) -> str:
    """Return truncated review + comment bodies for a PR, or ``""`` if unavailable."""
    return client.pr_review_excerpt(pr_number, max_chars=max_chars)


def fetch_pr_excerpt(client: GitHubClient, pr_number: int) -> str:
    """Combine a compact diff + review/comment excerpt for one PR."""
    diff = fetch_pr_diff_excerpt(client, pr_number)
    review = fetch_pr_review_excerpt(client, pr_number)
    blocks = []
    if diff:
        blocks.append(f"--- diff (truncated) ---\n{diff}")
    if review:
        blocks.append(f"--- review/comments (truncated) ---\n{review}")
    return "\n".join(blocks)


def fetch_pr_excerpts(
    client: GitHubClient,
    pr_numbers: Iterable[int],
    *,
    max_prs: int = MAX_PR_EXCERPTS_PER_GROUP,
) -> dict[int, str]:
    """Fetch excerpts for up to ``max_prs`` PRs (bounds GitHub calls per group)."""
    excerpts: dict[int, str] = {}
    for pr_number in sorted(set(pr_numbers))[:max_prs]:
        excerpts[pr_number] = fetch_pr_excerpt(client, pr_number)
    return excerpts


def fetch_open_gotcha_issues(client: GitHubClient, *, label: str) -> list[dict[str, Any]]:
    """Return open issues carrying *label*, for candidate dedup."""
    return client.open_issues(label)


def load_session_learning_notes_by_pr(telemetry_dir: Path) -> dict[int, list[str]]:
    """Defensively load SessionLearning JSON events, if any exist.

    The loader is forward-compatible (glob + tolerant per-record parsing) and
    simply returns ``{}`` until such files exist: consuming the notes is
    optional and a malformed file must never abort mining.
    """
    notes_by_pr: dict[int, list[str]] = {}
    if not telemetry_dir.is_dir():
        return notes_by_pr
    for path in sorted(telemetry_dir.glob(SESSION_LEARNING_GLOB)):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        records = payload if isinstance(payload, list) else [payload]
        for record in records:
            if not isinstance(record, dict):
                continue
            pr_number = record.get("pr_number")
            note = record.get("lesson") or record.get("summary") or record.get("note")
            if isinstance(pr_number, int) and isinstance(note, str) and note.strip():
                notes_by_pr.setdefault(pr_number, []).append(note.strip())
    return notes_by_pr


__all__ = [
    "MAX_DIFF_CHARS",
    "MAX_PR_EXCERPTS_PER_GROUP",
    "MAX_REVIEW_CHARS",
    "SESSION_LEARNING_GLOB",
    "fetch_open_gotcha_issues",
    "fetch_pr_diff_excerpt",
    "fetch_pr_excerpt",
    "fetch_pr_excerpts",
    "fetch_pr_review_excerpt",
    "load_session_learning_notes_by_pr",
]
