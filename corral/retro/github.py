"""Single GitHub isolation boundary for the retrospective pipeline.

Every ``gh`` subprocess invocation in the retrospective lives in this module.
The pipeline only sees :class:`GitHubClient`, so tests inject fakes and the
CLI constructs :class:`GhCliGitHub`.  All read methods are best-effort: a
``gh`` failure degrades to an empty result (losing one PR's excerpt must not
abort the run), while write methods surface errors to the caller, which owns
the ``--dry-run`` / ``retro.issue_sink`` policy.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from typing import Any, Protocol

DEFAULT_GH_TIMEOUT_S = 30
OPEN_ISSUE_LIMIT = 200
MERGED_PR_LIMIT = 2000

_REVIEW_EXCERPT_JQ = (
    '[.reviews[]?.body, .comments[]?.body] | map(select(. != null and . != "")) | join("\\n---\\n")'
)


class GitHubError(RuntimeError):
    """A GitHub write operation failed (issue filing, label ops, ...)."""


class GitHubClient(Protocol):
    """The pipeline's only view of GitHub."""

    repo: str

    def merged_prs(self, since: str, until: str) -> list[dict[str, Any]]:
        """Merged PRs in the window, shaped like ``gh pr list --json`` rows."""
        ...

    def pr_diff_excerpt(self, pr_number: int, *, max_chars: int) -> str:
        """Truncated ``gh pr diff`` patch, or ``""`` when unavailable."""
        ...

    def pr_review_excerpt(self, pr_number: int, *, max_chars: int) -> str:
        """Truncated review + comment bodies, or ``""`` when unavailable."""
        ...

    def open_issues(self, label: str) -> list[dict[str, Any]]:
        """Open issues carrying *label* (number/title/body rows)."""
        ...

    def create_issue(
        self,
        title: str,
        body: str,
        *,
        labels: Sequence[str] = (),
        assignee: str | None = None,
    ) -> str:
        """File an issue; return its URL. Raises :class:`GitHubError`."""
        ...


class GhCliGitHub:
    """``gh``-backed implementation bound to one ``owner/name`` repository."""

    def __init__(self, repo: str, *, timeout_s: float = DEFAULT_GH_TIMEOUT_S) -> None:
        if not repo or "/" not in repo:
            raise ValueError(f"repository must look like owner/name, got {repo!r}")
        self.repo = repo
        self.timeout_s = timeout_s

    def _run(self, command: list[str], *, check: bool) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=check,
            timeout=self.timeout_s,
        )

    def merged_prs(self, since: str, until: str) -> list[dict[str, Any]]:
        command = [
            "gh",
            "pr",
            "list",
            "--repo",
            self.repo,
            "--state",
            "merged",
            "--search",
            f"merged:>={since} merged:<={until}",
            "--limit",
            str(MERGED_PR_LIMIT),
            "--json",
            "number,author,createdAt,mergedAt,title,files,headRefName,labels",
        ]
        try:
            result = self._run(command, check=True)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
            return []
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            return []
        return payload if isinstance(payload, list) else []

    def pr_diff_excerpt(self, pr_number: int, *, max_chars: int) -> str:
        try:
            result = self._run(
                ["gh", "pr", "diff", str(pr_number), "--repo", self.repo], check=True
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
            return ""
        return result.stdout[:max_chars]

    def pr_review_excerpt(self, pr_number: int, *, max_chars: int) -> str:
        try:
            result = self._run(
                [
                    "gh",
                    "pr",
                    "view",
                    str(pr_number),
                    "--repo",
                    self.repo,
                    "--json",
                    "reviews,comments",
                    "--jq",
                    _REVIEW_EXCERPT_JQ,
                ],
                check=True,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
            return ""
        return result.stdout.strip()[:max_chars]

    def open_issues(self, label: str) -> list[dict[str, Any]]:
        try:
            result = self._run(
                [
                    "gh",
                    "issue",
                    "list",
                    "--repo",
                    self.repo,
                    "--label",
                    label,
                    "--state",
                    "open",
                    "--limit",
                    str(OPEN_ISSUE_LIMIT),
                    "--json",
                    "number,title,body",
                ],
                check=True,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
            return []
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            return []
        return payload if isinstance(payload, list) else []

    def create_issue(
        self,
        title: str,
        body: str,
        *,
        labels: Sequence[str] = (),
        assignee: str | None = None,
    ) -> str:
        command = [
            "gh",
            "issue",
            "create",
            "--repo",
            self.repo,
            "--title",
            title,
            "--body",
            body,
        ]
        for label in labels:
            command.extend(["--label", label])
        if assignee:
            command.extend(["--assignee", assignee])
        try:
            result = self._run(command, check=True)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
            detail = getattr(exc, "stderr", "") or str(exc)
            raise GitHubError(f"failed to file issue {title!r}: {detail}") from exc
        return result.stdout.strip()


class NullGitHub:
    """Offline stand-in: no reads, and writes are refused.

    Used when ``retro.repository`` is unset for read-only sub-operations; the
    pipeline treats it exactly like a host without ``gh`` auth.
    """

    repo = ""

    def merged_prs(self, since: str, until: str) -> list[dict[str, Any]]:
        return []

    def pr_diff_excerpt(self, pr_number: int, *, max_chars: int) -> str:
        return ""

    def pr_review_excerpt(self, pr_number: int, *, max_chars: int) -> str:
        return ""

    def open_issues(self, label: str) -> list[dict[str, Any]]:
        return []

    def create_issue(
        self,
        title: str,
        body: str,
        *,
        labels: Sequence[str] = (),
        assignee: str | None = None,
    ) -> str:
        raise GitHubError("no GitHub client configured (retro.repository is unset)")


__all__ = [
    "DEFAULT_GH_TIMEOUT_S",
    "GhCliGitHub",
    "GitHubClient",
    "GitHubError",
    "NullGitHub",
]
