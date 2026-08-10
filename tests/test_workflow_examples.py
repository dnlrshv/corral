"""Sanity checks for the sanitized GitHub Actions workflow examples.

The four examples under ``examples/github-actions/`` are shapes only (private
originals were deliberately not provided), so these tests validate structure
rather than behavior: YAML parseability, runner/permissions hygiene, pinned
actions, and the contract each workflow encodes (single-writer guard,
trusted-base pattern, etc.).

Note: YAML 1.1 parses the bare ``on:`` key as boolean ``True``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

EXAMPLES = Path(__file__).parent.parent / "examples" / "github-actions"
WORKFLOW_FILES = [
    "retro-weekly.yml",
    "governance-gate.yml",
    "replay.yml",
    "telemetry-rollup.yml",
]
ALLOWED_PERMISSIONS = {"actions", "checks", "contents", "pull-requests", "issues"}


def _load(name: str) -> tuple[dict, str]:
    text = (EXAMPLES / name).read_text(encoding="utf-8")
    return yaml.safe_load(text), text


def _steps(doc: dict) -> list[dict]:
    (job,) = doc["jobs"].values()
    return job["steps"]


def _all_runs(doc: dict) -> str:
    return "\n".join(step.get("run", "") for step in _steps(doc))


def test_all_workflows_parse_with_expected_top_level_keys() -> None:
    for name in WORKFLOW_FILES:
        doc, _ = _load(name)
        assert isinstance(doc, dict), name
        assert doc.get("name"), f"{name}: missing workflow name"
        assert True in doc, f"{name}: missing `on:` trigger block"
        assert "permissions" in doc, f"{name}: missing top-level permissions block"
        assert doc["jobs"], f"{name}: no jobs defined"


def test_single_job_runs_on_stock_ubuntu() -> None:
    for name in WORKFLOW_FILES:
        doc, text = _load(name)
        assert len(doc["jobs"]) == 1, f"{name}: expected exactly one job"
        (job,) = doc["jobs"].values()
        assert job["runs-on"] == "ubuntu-latest", f"{name}: unexpected runner"
        # No self-hosted runners, owner/bot/label names leak into examples.
        assert "self-hosted" not in text, name
        assert "dnlrshv" not in text, name


def test_actions_are_version_pinned() -> None:
    for name in WORKFLOW_FILES:
        doc, _ = _load(name)
        for step in _steps(doc):
            uses = step.get("uses")
            if uses is None:
                continue
            assert "@" in uses, f"{name}: unpinned action {uses!r}"
            ref = uses.split("@", 1)[1]
            assert ref, f"{name}: empty ref in {uses!r}"
            assert ref not in {"main", "master", "HEAD"}, f"{name}: floating ref {uses!r}"
            assert ref.startswith(("v", "sha-")) or len(ref) == 40, (
                f"{name}: ref {ref!r} is neither a version tag nor a full SHA"
            )


def test_permissions_blocks_are_read_only_and_minimal() -> None:
    for name in WORKFLOW_FILES:
        doc, _ = _load(name)
        permissions = doc["permissions"]
        assert isinstance(permissions, dict) and permissions, name
        assert set(permissions) <= ALLOWED_PERMISSIONS, f"{name}: {sorted(permissions)}"
        for scope, level in permissions.items():
            assert level in {"read", "none"}, f"{name}: {scope}={level} is not read-only"


def test_retro_weekly_shape() -> None:
    doc, text = _load("retro-weekly.yml")
    triggers = doc[True]
    assert "schedule" in triggers and triggers["schedule"], "weekly schedule missing"
    (cron_entry,) = triggers["schedule"]
    cron_fields = cron_entry["cron"].split()
    assert len(cron_fields) == 5 and cron_fields[4] != "*", "cron must run weekly"
    assert "workflow_dispatch" in triggers, "manual dispatch missing"

    runs = _all_runs(doc)
    assert "corral retro run" in runs
    assert "--expected-base" in runs, "single-writer base-SHA contract missing"
    # Seat credentials arrive as placeholder secret names only.
    assert "secrets.CORRAL_DRAFTER_API_KEY" in text
    assert "secrets.CORRAL_VERIFIER_API_KEY" in text
    assert doc["permissions"] == {
        "contents": "read",
        "issues": "read",
        "pull-requests": "read",
    }
    retro_step = next(step for step in _steps(doc) if step["name"] == "Run weekly retrospective")
    assert retro_step["env"]["GH_TOKEN"] == "${{ github.token }}"
    # Base-SHA guard: re-check remote HEAD before push, abort if moved.
    guard_steps = [s for s in _steps(doc) if "Re-check remote HEAD" in s.get("name", "")]
    assert guard_steps, "base-SHA guard step missing"
    assert "exit 1" in guard_steps[0]["run"]
    # PR opened with a PAT/App placeholder token, not the workflow token.
    assert "secrets.CORRAL_RETRO_PR_TOKEN" in text
    assert "gh pr create" in runs
    assert "git add agent_memory/gotchas.json retrospective.md" in runs
    assert "git add ." not in runs
    assert "docs/instructions" not in runs and "instruction_rules.yaml" not in runs


def test_governance_gate_trusted_base_shape() -> None:
    doc, text = _load("governance-gate.yml")
    triggers = doc[True]
    assert "pull_request" in triggers
    assert triggers["pull_request"]["paths"], "gate must trigger on instruction paths"

    runs = _all_runs(doc)
    assert "governance check" in runs
    assert "--base-ref" in runs and "--head-ref" in runs
    assert '--root "$GITHUB_WORKSPACE"' in runs
    assert '--pr-body-file "$GITHUB_WORKSPACE/pr_body.md"' in runs
    # The launcher itself is installed from a BASE archive in an isolated venv;
    # the untrusted head checkout is never installed as validator policy.
    assert "git archive --format=tar \"$BASE_REF\"" in runs
    assert 'pip install "$trusted_src"' in runs
    assert "pip install ." not in runs
    assert "-I -c" in runs
    # The trusted-base rationale is documented, including PYTHONPATH hardening.
    assert "TRUSTED-BASE" in text
    assert "PYTHONPATH" in text
    # Gate runs on github.token alone -- no extra secrets.
    assert "secrets." not in text
    assert doc["permissions"] == {"contents": "read"}


def test_replay_shape() -> None:
    doc, _ = _load("replay.yml")
    assert "pull_request" in doc[True]
    assert "corral governance replay" in _all_runs(doc)


def test_telemetry_rollup_shape() -> None:
    doc, text = _load("telemetry-rollup.yml")
    triggers = doc[True]
    assert "schedule" in triggers and triggers["schedule"], "weekly schedule missing"
    (cron_entry,) = triggers["schedule"]
    assert len(cron_entry["cron"].split()) == 5 and cron_entry["cron"].split()[4] != "*"

    runs = _all_runs(doc)
    assert "corral telemetry rollup" in runs
    assert "ci-outcome" in runs, "CI-outcome reconstruction step missing"
    assert "gh pr create" in runs, "rollup must open a PR with parquet + summary"
    # Read-side calls use the default workflow token; push uses a PAT placeholder.
    assert "github.token" in text
    assert "secrets.CORRAL_TELEMETRY_PR_TOKEN" in text
    assert doc["permissions"] == {
        "actions": "read",
        "checks": "read",
        "contents": "read",
        "pull-requests": "read",
    }
