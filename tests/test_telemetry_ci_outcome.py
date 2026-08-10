"""Tests for CI-outcome reconstruction (corral.telemetry.ci_outcome).

All ``gh`` interaction is mocked via subprocess; no network and no real gh
binary are involved.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from corral.cli import main as cli_main
from corral.telemetry import ci_outcome

CONTEXTS = ("lint", "test")


def _completed(stdout: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout)


def test_commit_required_checks_green_variants() -> None:
    assert (
        ci_outcome.commit_required_checks_green({"lint": "success", "test": "neutral"}, CONTEXTS)
        is True
    )
    assert (
        ci_outcome.commit_required_checks_green({"lint": "success", "test": "skipped"}, CONTEXTS)
        is True
    )
    assert (
        ci_outcome.commit_required_checks_green({"lint": "success", "test": "failure"}, CONTEXTS)
        is False
    )
    # Missing required context -> unknown, not failed.
    assert ci_outcome.commit_required_checks_green({"lint": "success"}, CONTEXTS) is None


def test_compute_ci_outcome_green_first_try() -> None:
    outcome = ci_outcome.compute_ci_outcome(
        ["a", "b"], lambda sha: {"lint": "success", "test": "success"}
    )
    assert outcome == {"first_head_ci_green": True, "ci_fix_iterations": 0, "final_ci_green": True}


def test_compute_ci_outcome_fix_iterations() -> None:
    conclusions = {
        "a": {"lint": "failure", "test": "success"},
        "b": {"lint": "success", "test": "success"},
    }
    outcome = ci_outcome.compute_ci_outcome(["a", "b"], conclusions.get)
    assert outcome == {
        "first_head_ci_green": False,
        "ci_fix_iterations": 1,
        "final_ci_green": True,
    }


def test_compute_ci_outcome_never_green() -> None:
    conclusions = {
        "a": {"lint": "failure", "test": "failure"},
        "b": {"lint": "failure", "test": "success"},
    }
    outcome = ci_outcome.compute_ci_outcome(["a", "b"], conclusions.get)
    assert outcome == {
        "first_head_ci_green": False,
        "ci_fix_iterations": None,
        "final_ci_green": False,
    }


def test_compute_ci_outcome_unknown_first_commit() -> None:
    outcome = ci_outcome.compute_ci_outcome(["a"], lambda sha: {"lint": "success"})
    assert outcome == {
        "first_head_ci_green": None,
        "ci_fix_iterations": None,
        "final_ci_green": None,
    }


def test_compute_ci_outcome_no_commits() -> None:
    assert ci_outcome.compute_ci_outcome([], lambda sha: {}) == {
        "first_head_ci_green": None,
        "ci_fix_iterations": None,
        "final_ci_green": None,
    }


def test_fetch_pr_commit_shas(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return _completed("sha1\nsha2\n")

    monkeypatch.setattr(ci_outcome.subprocess, "run", fake_run)
    assert ci_outcome.fetch_pr_commit_shas(42, "octo/repo") == ["sha1", "sha2"]
    assert "/repos/octo/repo/pulls/42/commits" in calls[0]


def test_fetch_pr_commit_shas_fail_soft_without_gh(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("gh")

    monkeypatch.setattr(ci_outcome.subprocess, "run", fake_run)
    assert ci_outcome.fetch_pr_commit_shas(42, "octo/repo") == []


def test_fetch_commit_check_run_conclusions_rerun_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lines = [
        json.dumps({"name": "lint", "conclusion": "failure", "started_at": "2026-08-01T00:00:00Z"}),
        json.dumps({"name": "lint", "conclusion": "success", "started_at": "2026-08-01T01:00:00Z"}),
        json.dumps({"name": "test", "conclusion": None, "started_at": "2026-08-01T01:00:00Z"}),
        json.dumps({"name": "docs", "conclusion": "success", "started_at": "2026-08-01T01:00:00Z"}),
        "not json",
    ]
    monkeypatch.setattr(
        ci_outcome.subprocess, "run", lambda cmd, **kwargs: _completed("\n".join(lines))
    )
    assert ci_outcome.fetch_commit_check_run_conclusions("sha", "octo/repo", CONTEXTS) == {
        "lint": "success"
    }


def test_fetch_ci_outcome_for_pr_with_mocked_gh(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        joined = " ".join(cmd)
        if "pulls/42/commits" in joined:
            return _completed("sha-a\nsha-b\n")
        if "commits/sha-a/check-runs" in joined:
            return _completed(
                json.dumps({"name": "lint", "conclusion": "failure", "started_at": "2026-08-01T00:00:00Z"})
                + "\n"
                + json.dumps({"name": "test", "conclusion": "success", "started_at": "2026-08-01T00:00:00Z"})
            )
        if "commits/sha-b/check-runs" in joined:
            return _completed(
                json.dumps({"name": "lint", "conclusion": "success", "started_at": "2026-08-01T01:00:00Z"})
                + "\n"
                + json.dumps({"name": "test", "conclusion": "success", "started_at": "2026-08-01T01:00:00Z"})
            )
        raise AssertionError(f"unexpected gh call: {joined}")

    monkeypatch.setattr(ci_outcome.subprocess, "run", fake_run)
    assert ci_outcome.fetch_ci_outcome_for_pr(42, "octo/repo", CONTEXTS) == {
        "first_head_ci_green": False,
        "ci_fix_iterations": 1,
        "final_ci_green": True,
    }


def test_fetch_ci_outcome_for_pr_honors_custom_required_contexts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    custom_contexts = ("ci", "policy")

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        joined = " ".join(cmd)
        if "pulls/42/commits" in joined:
            return _completed("sha-a\n")
        if "commits/sha-a/check-runs" in joined:
            return _completed(
                json.dumps(
                    {"name": "ci", "conclusion": "success", "started_at": "2026-08-01T00:00:00Z"}
                )
                + "\n"
                + json.dumps(
                    {
                        "name": "policy",
                        "conclusion": "success",
                        "started_at": "2026-08-01T00:00:00Z",
                    }
                )
            )
        raise AssertionError(f"unexpected gh call: {joined}")

    monkeypatch.setattr(ci_outcome.subprocess, "run", fake_run)
    assert ci_outcome.fetch_ci_outcome_for_pr(42, "octo/repo", custom_contexts) == {
        "first_head_ci_green": True,
        "ci_fix_iterations": 0,
        "final_ci_green": True,
    }


def test_ci_outcome_cli_prints_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # gh returns no commit SHAs -> null outcome, still exit 0 (fail-soft).
    monkeypatch.setattr(
        ci_outcome.subprocess, "run", lambda cmd, **kwargs: _completed("")
    )
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)

    rc = cli_main(["telemetry", "ci-outcome", "--pr", "42", "--repo", "octo/repo"])

    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {
        "first_head_ci_green": None,
        "ci_fix_iterations": None,
        "final_ci_green": None,
    }


def test_ci_outcome_cli_requires_repo(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    assert cli_main(["telemetry", "ci-outcome", "--pr", "42"]) == 1
    assert "required" in capsys.readouterr().out
