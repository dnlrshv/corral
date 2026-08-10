"""Shared fakes and builders for the retrospective test suite (hermetic:
no network, no real gh, no real seats)."""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from datetime import timezone, datetime
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from corral.retro.fixups import build_table
from corral.retro.providers.base import Availability, SeatResult, SeatStatus

SEATS_YAML = (
    "schema_version: 1\n"
    "seats:\n"
    "  draft:\n"
    "    provider: vendor-a\n    model: draft-model\n    auth_env: null\n"
    "    adapter: shell-command\n    options:\n"
    f"      argv: [{sys.executable!r}, --version]\n"
    "  verify:\n"
    "    provider: vendor-b\n    model: verify-model\n    auth_env: null\n"
    "    adapter: shell-command\n    options:\n"
    f"      argv: [{sys.executable!r}, --version]\n"
)

BASE_CONFIG = (
    "seats_file: seats.yaml\n"
    "retro:\n"
    "  drafter_seat: draft\n"
    "  verifier_seats: [verify]\n"
    "  repository: example/test-repo\n"
)


class FakeGitHub:
    """In-memory GitHubClient double recording every write-side call."""

    def __init__(
        self,
        *,
        prs: list[dict[str, Any]] | None = None,
        diffs: dict[int, str] | None = None,
        reviews: dict[int, str] | None = None,
        issues: list[dict[str, Any]] | None = None,
    ) -> None:
        self.repo = "example/test-repo"
        self.prs = prs or []
        self.diffs = diffs or {}
        self.reviews = reviews or {}
        self.issues = issues or []
        self.created_issues: list[dict[str, Any]] = []
        self.issue_labels_requested: list[str] = []

    def merged_prs(self, since: str, until: str) -> list[dict[str, Any]]:
        return self.prs

    def pr_diff_excerpt(self, pr_number: int, *, max_chars: int) -> str:
        return self.diffs.get(pr_number, "")[:max_chars]

    def pr_review_excerpt(self, pr_number: int, *, max_chars: int) -> str:
        return self.reviews.get(pr_number, "")[:max_chars]

    def open_issues(self, label: str) -> list[dict[str, Any]]:
        self.issue_labels_requested.append(label)
        return self.issues

    def create_issue(
        self,
        title: str,
        body: str,
        *,
        labels: Sequence[str] = (),
        assignee: str | None = None,
    ) -> str:
        self.created_issues.append(
            {"title": title, "body": body, "labels": list(labels), "assignee": assignee}
        )
        return f"https://github.com/{self.repo}/issues/{len(self.created_issues)}"


class FakeSeatRunner:
    """Scripted probe/complete results; records every prompt."""

    def __init__(self, outputs: list[Any], *, probe_status: SeatStatus = SeatStatus.OK) -> None:
        self.outputs = list(outputs)
        self.probe_status = probe_status
        self.prompts: list[str] = []
        self.calls = 0
        self._last: Any = None

    def probe(self, seat: Any) -> Availability:
        return Availability(self.probe_status, seat.provider, seat.model, seat=seat.name)

    def complete(self, seat: Any, prompt: str, *, timeout: float, max_tokens: int) -> SeatResult:
        self.calls += 1
        self.prompts.append(prompt)
        if self.outputs:
            self._last = self.outputs.pop(0)
        value = self._last
        if isinstance(value, SeatStatus):
            return SeatResult("", seat.provider, seat.model, value, "degraded", seat.name)
        return SeatResult(value, seat.provider, seat.model, SeatStatus.OK, seat=seat.name)


def make_repo(tmp_path: Path, config_extra: str = "") -> Path:
    """Create a repo root with seats.yaml and corral.yaml."""
    root = tmp_path / "repo"
    root.mkdir(parents=True, exist_ok=True)
    (root / "seats.yaml").write_text(SEATS_YAML, encoding="utf-8")
    (root / "corral.yaml").write_text(BASE_CONFIG + config_extra, encoding="utf-8")
    return root


def pr_row(
    number: int,
    *,
    author: str,
    merged_at: str,
    files: Sequence[str],
    title: str = "",
    created_at: str | None = None,
    head_ref: str = "",
) -> dict[str, Any]:
    return {
        "number": number,
        "author": {"login": author},
        "createdAt": created_at or merged_at,
        "mergedAt": merged_at,
        "title": title,
        "files": [{"path": path} for path in files],
        "headRefName": head_ref,
        "labels": [],
    }


def fixup_rows(
    *,
    original_pr: int,
    fixup_pr: int,
    shared_files: Sequence[str],
    agent: str = "claude",
    area: str = "src",
    days_between: float = 2.0,
) -> dict[str, Any]:
    """One row shaped like find_fixup_pairs output (parquet-compatible)."""
    merged = datetime(2026, 8, 3, 12, 0, 0, tzinfo=timezone.utc)
    return {
        "original_pr": original_pr,
        "original_author": f"{agent}-agent",
        "original_merged_at": merged,
        "fixup_pr": fixup_pr,
        "fixup_author": "someone",
        "fixup_merged_at": merged,
        "days_between": days_between,
        "shared_files": list(shared_files),
        "agent": agent,
        "area": area,
        "original_cycle_time_days": 1.0,
    }


def write_fixup_parquet(root: Path, rows: list[dict[str, Any]], week: str) -> Path:
    path = root / "agent_telemetry" / f"fixup_{week}.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(build_table(rows), path)
    return path


def candidate_json(
    *,
    rule: str = "Always run the affected tests before merging",
    confidence: float = 0.9,
    severity: str = "info",
    repo_paths: Sequence[str] = ("src/orders.py",),
    rationale: str = "Two fix-ups patched the same oversight",
) -> str:
    return json.dumps(
        {
            "rule": rule,
            "workflow_kinds": ["fix-issue"],
            "repo_paths": list(repo_paths),
            "surface_ids": [],
            "control_type": "prompt_only",
            "control_path": None,
            "inject_into_briefer": True,
            "confidence": confidence,
            "rationale": rationale,
            "severity": severity,
        }
    )


CONFIRM = "VERDICT: CONFIRM\nREASONING: supported by both diffs\nSHARPENED: NONE"
REFUTE = "VERDICT: REFUTE\nREASONING: coincidental overlap only\nSHARPENED: NONE"


def runners_factory(runners: dict[str, FakeSeatRunner]):
    return lambda seat: runners[seat.name]
