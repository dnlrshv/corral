"""CI-outcome reconstruction for merged agent PRs.

GitHub retains check-run data for every commit SHA a PR ever had, even after
the PR is squash-merged and the SHA is no longer reachable from any branch.
That makes first-pass CI outcome reconstructable for *historical* merged PRs,
not just prospectively going forward -- there is no separate "capture at push
time" mechanism here; both a one-time historical backfill and every future
weekly rollup call the exact same functions in this module against the PR's
commit history.

Coverage is incomplete, though: some commits -- most often the very first push,
before CI dispatch, or PRs predating one of the required checks -- have no
check-run record for one or more required contexts.
``commit_required_checks_green`` returns ``None`` (not ``False``) when any
required context is missing data, so ``compute_ci_outcome`` degrades to a null
result for that field rather than silently guessing pass/fail.

All ``gh`` calls are fail-soft: a missing ``gh`` binary or a failed API call
degrades to "no data" (empty commit list / empty conclusion map), which yields
null outcome fields rather than an error.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from typing import Any

#: Branch-protection required status checks assumed for ``main``. This is
#: per-repository CI configuration: override via
#: ``telemetry.required_ci_contexts`` in ``corral.yaml`` (or the
#: ``required_contexts`` parameter). Historical PRs predating a given check
#: will simply have no check-run for it -> that context reads as "missing",
#: not "failed" -- see commit_required_checks_green.
REQUIRED_CI_CONTEXTS: tuple[str, ...] = ("lint", "test")

# Conclusions that do not block merge. A "skipped"/"neutral" check (e.g. a
# path-filtered workflow, or a job short-circuited by an earlier gate) is not a
# CI failure.
_PASSING_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})


def fetch_pr_commit_shas(pr_number: int, repo: str) -> list[str]:
    """Return the PR's commit SHAs in push order (oldest first)."""
    try:
        result = subprocess.run(
            [
                "gh",
                "api",
                "--paginate",
                f"/repos/{repo}/pulls/{pr_number}/commits",
                "--jq",
                ".[].sha",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, OSError):
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def fetch_commit_check_run_conclusions(
    sha: str, repo: str, required_contexts: tuple[str, ...] = REQUIRED_CI_CONTEXTS
) -> dict[str, str]:
    """Return ``{context_name: conclusion}`` for this commit SHA.

    Only *required_contexts* names are kept. When a context re-ran (e.g. a
    manual re-run after a flaky failure), the run with the latest ``started_at``
    wins.
    """
    try:
        result = subprocess.run(
            [
                "gh",
                "api",
                "--paginate",
                f"/repos/{repo}/commits/{sha}/check-runs",
                "--jq",
                ".check_runs[] | {name, conclusion, started_at}",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, OSError):
        return {}

    latest_by_name: dict[str, tuple[str, str]] = {}
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            run = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = run.get("name")
        if name not in required_contexts:
            continue
        conclusion = run.get("conclusion")
        if not conclusion:
            # In-progress / never-completed run (conclusion is JSON null, e.g. a
            # runner killed mid-job): no verdict exists, so skip it -- the context
            # stays missing (-> None upstream) rather than reading as a failure.
            continue
        started_at = str(run.get("started_at") or "")
        previous = latest_by_name.get(name)
        if previous is None or started_at >= previous[0]:
            latest_by_name[name] = (started_at, str(conclusion))

    return {name: conclusion for name, (_started_at, conclusion) in latest_by_name.items()}


def commit_required_checks_green(
    conclusions: dict[str, str], required_contexts: tuple[str, ...] = REQUIRED_CI_CONTEXTS
) -> bool | None:
    """Whether every required context passed on one commit.

    Returns ``None`` (not ``False``) when any required context has no recorded
    check-run for this commit -- data not reconstructable, distinct from a
    confirmed failure.
    """
    if any(context not in conclusions for context in required_contexts):
        return None
    return all(conclusions[context] in _PASSING_CONCLUSIONS for context in required_contexts)


def compute_ci_outcome(
    commit_shas: list[str],
    fetch_conclusions: Callable[[str], dict[str, str]],
    required_contexts: tuple[str, ...] = REQUIRED_CI_CONTEXTS,
) -> dict[str, Any]:
    """Pure reconstruction given an injected per-commit conclusion fetcher.

    Kept separate from live gh calls (see ``fetch_ci_outcome_for_pr``) so it is
    unit-testable with fixture data and no live gh CLI.
    """
    if not commit_shas:
        return {"first_head_ci_green": None, "ci_fix_iterations": None, "final_ci_green": None}

    statuses = [
        commit_required_checks_green(fetch_conclusions(sha), required_contexts)
        for sha in commit_shas
    ]
    return {
        "first_head_ci_green": statuses[0],
        "ci_fix_iterations": _ci_fix_iterations(statuses),
        "final_ci_green": statuses[-1],
    }


def _ci_fix_iterations(statuses: list[bool | None]) -> int | None:
    """Commits pushed until the first fully-green commit, or ``None`` if unknown.

    A proxy for fix effort: intermediate commits may include unrelated scope or
    review-feedback changes, not only CI-triage pushes. Requires the first
    commit's outcome to be known -- if it is ``None``, we cannot say whether
    zero or more iterations were needed, so the result is ``None`` too.
    """
    if statuses[0] is None:
        return None
    for index, status in enumerate(statuses):
        if status is True:
            return index
    return None


def fetch_ci_outcome_for_pr(
    pr_number: int, repo: str, required_contexts: tuple[str, ...] = REQUIRED_CI_CONTEXTS
) -> dict[str, Any]:
    """Live gh-backed CI outcome for one PR.

    Reconstructs first-push green, iterations-to-green, and final-push green from
    the PR's full commit history. Used identically for a one-time historical
    backfill and every future weekly rollup (see
    :mod:`corral.telemetry.rollup`).
    """
    commit_shas = fetch_pr_commit_shas(pr_number, repo)
    return compute_ci_outcome(
        commit_shas,
        lambda sha: fetch_commit_check_run_conclusions(sha, repo, required_contexts),
        required_contexts,
    )


__all__ = [
    "REQUIRED_CI_CONTEXTS",
    "commit_required_checks_green",
    "compute_ci_outcome",
    "fetch_ci_outcome_for_pr",
    "fetch_commit_check_run_conclusions",
    "fetch_pr_commit_shas",
]
