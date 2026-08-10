"""Hermetic end-to-end tests for `corral retro run` / `revert-refinement`.

Synthetic telemetry parquet + FakeGitHub + scripted fake seats: no network,
no real gh, no real model calls.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import timezone, date, datetime, timedelta
from pathlib import Path

import pytest

from corral.cli import main
from corral.memory import registry as memory_registry
from corral.retro.cli import (
    find_fixup_parquet,
    iso_week_label,
    run_retro,
    week_window,
)
from corral.retro.github import GhCliGitHub
from corral.retro.providers.base import SeatStatus
from corral.retro.refinements import (
    RefinementEditSnapshot,
    append_records,
    capture_refinement,
    materialize_records,
)
from tests.retro_support import (
    CONFIRM,
    REFUTE,
    FakeGitHub,
    FakeSeatRunner,
    candidate_json,
    fixup_rows,
    make_repo,
    runners_factory,
    write_fixup_parquet,
)

WEEK = iso_week_label()


def make_args(config_path: Path, **overrides) -> argparse.Namespace:
    values = {
        "config": config_path,
        "dry_run": False,
        "week": None,
        "since": None,
        "until": None,
        "output_summary": None,
        "base_ref": None,
        "expected_base": None,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def standard_repo(tmp_path: Path, config_extra: str = "") -> tuple[Path, Path]:
    """Repo with a two-root-incident fixup parquet for the current week."""
    root = make_repo(tmp_path, config_extra)
    write_fixup_parquet(
        root,
        [
            fixup_rows(original_pr=11, fixup_pr=12, shared_files=["src/orders.py"]),
            fixup_rows(original_pr=13, fixup_pr=14, shared_files=["src/orders.py"]),
        ],
        WEEK,
    )
    return root, root / "corral.yaml"


def run_pipeline(
    config_path: Path,
    *,
    drafter_outputs: list,
    verifier_outputs: list,
    github: FakeGitHub | None = None,
    **arg_overrides,
) -> tuple[int, FakeSeatRunner, FakeSeatRunner, FakeGitHub]:
    drafter = FakeSeatRunner(drafter_outputs)
    verifier = FakeSeatRunner(verifier_outputs)
    factory = runners_factory({"draft": drafter, "verify": verifier})
    gh = github or FakeGitHub(diffs={11: "diff-11", 12: "diff-12", 13: "diff-13", 14: "diff-14"})
    code = run_retro(
        make_args(config_path, **arg_overrides),
        github=gh,
        runner_factory=factory,
    )
    return code, drafter, verifier, gh


def summary_text(root: Path) -> str:
    return (root / "agent_telemetry" / f"retrospective_{WEEK}.md").read_text(encoding="utf-8")


def tree_snapshot(root: Path) -> tuple[dict[str, bytes], set[str]]:
    files = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }
    directories = {
        path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_dir()
    }
    return files, directories


# -- happy paths -------------------------------------------------------------


def test_dry_run_confirms_and_renders_summary(tmp_path: Path, capsys, monkeypatch) -> None:
    root, config = standard_repo(tmp_path)
    before = tree_snapshot(root)
    registry_writes: list[object] = []
    refinement_writes: list[object] = []
    monkeypatch.setattr(
        "corral.retro.registry.write_gotchas_file",
        lambda *args, **kwargs: registry_writes.append((args, kwargs)),
    )
    monkeypatch.setattr(
        "corral.retro.refinements.append_records",
        lambda *args, **kwargs: refinement_writes.append((args, kwargs)),
    )
    code, drafter, verifier, _gh = run_pipeline(
        config,
        drafter_outputs=[candidate_json()],
        verifier_outputs=[CONFIRM],
        dry_run=True,
        output_summary=root / "explicit-dry-run-summary.md",
    )
    assert code == 0
    assert drafter.calls == 1 and verifier.calls == 1
    assert not (root / "agent_memory" / "gotchas.json").exists()
    assert tree_snapshot(root) == before
    assert registry_writes == []
    assert refinement_writes == []
    text = capsys.readouterr().out
    assert "confirmed by vendor-b/verify-model" in text
    assert "DRY RUN (nothing written)" in text
    assert "G-" in text
    lowered = text.lower()
    for banned in ("codex", "opus", "dnlrshv", "trading-bot", ".ai"):
        assert banned not in lowered


def test_live_run_writes_schema_valid_registry(tmp_path: Path) -> None:
    root, config = standard_repo(tmp_path)
    code, _d, _v, _gh = run_pipeline(
        config, drafter_outputs=[candidate_json()], verifier_outputs=[CONFIRM]
    )
    assert code == 0
    gotchas_path = root / "agent_memory" / "gotchas.json"
    assert gotchas_path.is_file()
    assert memory_registry.validate_gotchas_file(gotchas_path) == []
    import json

    entry = json.loads(gotchas_path.read_text())["gotchas"][0]
    year = str(datetime.now(timezone.utc).year)
    assert entry["id"] == f"G-{year}-001"
    assert entry["expires"] == (
        date.fromisoformat(entry["created"]) + timedelta(days=90)
    ).isoformat()
    assert entry["source_prs"] == [11, 12, 13, 14]


def test_sharpened_confirm_replaces_rule_text(tmp_path: Path) -> None:
    root, config = standard_repo(tmp_path)
    sharpened = "VERDICT: CONFIRM\nREASONING: ok\nSHARPENED: Never merge without running the tests"
    code, _d, _v, _gh = run_pipeline(
        config, drafter_outputs=[candidate_json()], verifier_outputs=[sharpened]
    )
    assert code == 0
    text = summary_text(root)
    assert "Never merge without running the tests" in text
    assert "original wording (pre-sharpening)" in text


def test_refuted_candidate_excluded_but_rendered(tmp_path: Path) -> None:
    root, config = standard_repo(tmp_path)
    code, _d, verifier, _gh = run_pipeline(
        config, drafter_outputs=[candidate_json()], verifier_outputs=[REFUTE]
    )
    assert code == 0
    assert not (root / "agent_memory" / "gotchas.json").exists()
    text = summary_text(root)
    assert "## Refuted candidates" in text
    assert "coincidental overlap only" in text
    assert "vendor-b/verify-model reasoning" in text
    assert verifier.calls == 1


def test_unverifier_error_proceeds_unverified(tmp_path: Path) -> None:
    root, config = standard_repo(tmp_path)
    code, _d, verifier, _gh = run_pipeline(
        config, drafter_outputs=[candidate_json()], verifier_outputs=[SeatStatus.ERROR]
    )
    assert code == 0
    assert (root / "agent_memory" / "gotchas.json").is_file()  # proceed-unverified
    text = summary_text(root)
    assert "unverified (error: verifier seat" in text
    assert verifier.calls == 1


# -- mining bounds -----------------------------------------------------------


def test_dedup_against_existing_registry_skips_drafting(tmp_path: Path, capsys) -> None:
    root, config = standard_repo(tmp_path)
    gotchas_dir = root / "agent_memory"
    gotchas_dir.mkdir(parents=True, exist_ok=True)
    (gotchas_dir / "gotchas.json").write_text(
        '{"gotchas": [{"id": "G-2026-001", "rule": "r", "workflow_kinds": [], '
        '"repo_paths": [], "surface_ids": [], "source_prs": [11], '
        '"control_type": "prompt_only", "control_pr": null, "control_path": null, '
        '"inject_into_briefer": true, "created": "2026-01-01", "expires": null}]}',
        encoding="utf-8",
    )
    code, drafter, _v, _gh = run_pipeline(
        config, drafter_outputs=[], verifier_outputs=[], dry_run=True
    )
    assert code == 0
    assert drafter.calls == 0
    assert "Groups skipped as duplicates of existing gotchas/open issues: 1" in capsys.readouterr().out


def test_two_root_floor_blocks_single_incident_groups(tmp_path: Path, capsys) -> None:
    root = make_repo(tmp_path)
    write_fixup_parquet(
        root,
        [fixup_rows(original_pr=11, fixup_pr=12, shared_files=["src/orders.py"])],
        WEEK,
    )
    # A SessionLearning note on the SAME incident is still one root incident.
    telemetry = root / "agent_telemetry"
    (telemetry / "session_learning_2026.json").write_text(
        '[{"pr_number": 11, "lesson": "we forgot the migration"}]', encoding="utf-8"
    )
    code, drafter, _v, _gh = run_pipeline(
        root / "corral.yaml", drafter_outputs=[], verifier_outputs=[], dry_run=True
    )
    assert code == 0
    assert drafter.calls == 0
    assert "Zero proposals this week is a successful outcome" in capsys.readouterr().out


def test_candidate_cap_drops_lowest_evidence_group(tmp_path: Path, capsys) -> None:
    root = make_repo(tmp_path, "  evidence:\n    max_candidates: 1\n")
    write_fixup_parquet(
        root,
        [
            fixup_rows(original_pr=11, fixup_pr=12, shared_files=["aaa.py"]),
            fixup_rows(original_pr=13, fixup_pr=14, shared_files=["aaa.py"]),
            fixup_rows(original_pr=15, fixup_pr=16, shared_files=["aaa.py"]),
            fixup_rows(original_pr=21, fixup_pr=22, shared_files=["zzz.py"]),
            fixup_rows(original_pr=23, fixup_pr=24, shared_files=["zzz.py"]),
        ],
        WEEK,
    )
    code, drafter, _v, _gh = run_pipeline(
        root / "corral.yaml",
        drafter_outputs=[candidate_json()],
        verifier_outputs=[CONFIRM],
        dry_run=True,
    )
    assert code == 0
    assert drafter.calls == 1  # highest-evidence group only
    text = capsys.readouterr().out
    assert "candidate cap (1) reached" in text
    assert "`zzz.py`" in text
    assert "claude::" not in text.lower()


def test_default_cap_is_three_and_refute_does_not_backfill(tmp_path: Path, capsys) -> None:
    root = make_repo(tmp_path)
    rows = []
    for group_index, path in enumerate(("a.py", "b.py", "c.py", "d.py")):
        base = 10 + group_index * 10
        rows.extend(
            [
                fixup_rows(original_pr=base + 1, fixup_pr=base + 2, shared_files=[path]),
                fixup_rows(original_pr=base + 3, fixup_pr=base + 4, shared_files=[path]),
            ]
        )
    write_fixup_parquet(root, rows, WEEK)

    code, drafter, verifier, _gh = run_pipeline(
        root / "corral.yaml",
        drafter_outputs=[candidate_json(), candidate_json(), candidate_json()],
        verifier_outputs=[REFUTE, CONFIRM, CONFIRM],
        dry_run=True,
    )
    assert code == 0
    assert drafter.calls == 3
    assert verifier.calls == 3
    text = capsys.readouterr().out
    assert "`d.py`: dropped: candidate cap (3) reached" in text
    assert "Candidates refuted by the verifier" in text


def test_capacity_circuit_defers_remaining_groups(tmp_path: Path, monkeypatch, capsys) -> None:
    root = make_repo(tmp_path)
    write_fixup_parquet(
        root,
        [
            fixup_rows(original_pr=11, fixup_pr=12, shared_files=["aaa.py"]),
            fixup_rows(original_pr=13, fixup_pr=14, shared_files=["aaa.py"]),
            fixup_rows(original_pr=21, fixup_pr=22, shared_files=["zzz.py"]),
            fixup_rows(original_pr=23, fixup_pr=24, shared_files=["zzz.py"]),
        ],
        WEEK,
    )
    sleeps: list[float] = []
    monkeypatch.setattr("time.sleep", sleeps.append)
    code, drafter, _v, _gh = run_pipeline(
        root / "corral.yaml",
        drafter_outputs=[SeatStatus.ERROR],
        verifier_outputs=[],
        dry_run=True,
    )
    assert code == 0
    assert drafter.calls == 4  # bounded retry budget on the first group
    assert len(sleeps) == 3
    text = capsys.readouterr().out
    assert "re-run recommended" in text
    assert "pass-level capacity circuit open" in text
    assert "`zzz.py`" in text
    assert "claude::" not in text.lower()


# -- issue sinks -------------------------------------------------------------


def severe_repo(base: Path, extra: str = "") -> tuple[Path, Path]:
    root = make_repo(base, "  severe_severities: [P1]\n" + extra)
    write_fixup_parquet(
        root,
        [
            fixup_rows(original_pr=11, fixup_pr=12, shared_files=["src/orders.py"]),
            fixup_rows(original_pr=13, fixup_pr=14, shared_files=["src/orders.py"]),
        ],
        WEEK,
    )
    return root, root / "corral.yaml"


def test_severity_issue_github_sink_files_issue(tmp_path: Path) -> None:
    root, config = severe_repo(
        tmp_path, "  issue_sink: github\n  github:\n    assignee: oncall-dev\n"
    )
    code, _d, _v, gh = run_pipeline(
        config,
        drafter_outputs=[candidate_json(severity="P1")],
        verifier_outputs=[CONFIRM],
    )
    assert code == 0
    assert len(gh.created_issues) == 1
    issue = gh.created_issues[0]
    assert issue["labels"] == ["agent-gotcha"]
    assert issue["assignee"] == "oncall-dev"
    assert "[agent-gotcha] P1 candidate" in issue["title"]
    assert "https://github.com/example/test-repo/issues/1" in summary_text(root)


def test_live_github_sink_proposals_never_write_targets_file_issues_or_commit(
    tmp_path: Path, monkeypatch
) -> None:
    """Adversarial contract: enabled proposals remain summary-only in live mode."""
    from corral.governance.proposals import parse_proposal_block, validate_proposal_contract
    from tests.test_retro_proposals import CONFIRM_JSON, MANIFEST_YAML, PROPOSAL_PAYLOAD

    root, config = standard_repo(
        tmp_path,
        "  issue_sink: github\n"
        "  severe_severities: [P1]\n"
        "  proposals:\n"
        "    enabled: true\n"
        "    target_globs: [docs/instructions/**]\n"
        "governance:\n"
        "  registry: instruction_rules.yaml\n"
        "  reviewer: platform-team\n"
        "  instruction_globs: [docs/instructions/**]\n"
        "  replay:\n"
        "    manifest: instruction_manifest.yaml\n",
    )
    (root / "instruction_manifest.yaml").write_text(MANIFEST_YAML, encoding="utf-8")
    (root / "instruction_rules.yaml").write_text("", encoding="utf-8")
    target = root / "docs" / "instructions" / "core.md"
    target.parent.mkdir(parents=True)
    target.write_text("# Core instructions\n", encoding="utf-8")
    target_before = target.read_bytes()
    registry_before = (root / "instruction_rules.yaml").read_bytes()

    def no_subprocess(*args, **kwargs):
        raise AssertionError("live retro proposal path must not invoke git/commit subprocesses")

    monkeypatch.setattr("corral.retro.cli.subprocess.run", no_subprocess)
    code, _drafter, _verifier, github = run_pipeline(
        config,
        drafter_outputs=[candidate_json(severity="P1"), json.dumps(PROPOSAL_PAYLOAD)],
        verifier_outputs=[CONFIRM, CONFIRM_JSON],
    )

    assert code == 0
    assert target.read_bytes() == target_before
    assert (root / "instruction_rules.yaml").read_bytes() == registry_before
    assert not list((root / "docs" / "instructions").glob("*.proposed*"))
    # The configured GitHub sink receives only the ordinary severe-gotcha issue;
    # the instruction proposal is not filed as an issue.
    assert len(github.created_issues) == 1
    assert "R-RETRO" not in github.created_issues[0]["body"]
    assert "proposals:" not in github.created_issues[0]["body"]

    rendered = summary_text(root)
    block, parse_errors = parse_proposal_block(rendered)
    assert parse_errors == [] and block is not None
    from corral.config import load_config

    assert validate_proposal_contract(block, load_config(config).governance.proposals) == []


@pytest.mark.parametrize("artifact", ["summary", "gotchas"])
def test_live_enabled_proposals_refuse_artifacts_that_alias_instruction_targets(
    tmp_path: Path, artifact: str, capsys
) -> None:
    extra = (
        ("  gotchas_path: docs/instructions/core.md\n" if artifact == "gotchas" else "")
        + "  proposals:\n"
        "    enabled: true\n"
        "    target_globs: [docs/instructions/**]\n"
        "governance:\n"
        "  reviewer: platform-team\n"
        "  instruction_globs: [docs/instructions/**]\n"
    )
    root, config = standard_repo(tmp_path, extra)
    target = root / "docs" / "instructions" / "core.md"
    target.parent.mkdir(parents=True)
    target.write_text("do not overwrite\n", encoding="utf-8")
    before = target.read_bytes()
    drafter = FakeSeatRunner([])
    verifier = FakeSeatRunner([])
    github = FakeGitHub()
    kwargs = {"output_summary": target} if artifact == "summary" else {}

    code = run_retro(
        make_args(config, **kwargs),
        github=github,
        runner_factory=runners_factory({"draft": drafter, "verify": verifier}),
    )
    assert code == 1
    assert target.read_bytes() == before
    assert drafter.calls == verifier.calls == 0
    assert github.created_issues == []
    assert "refusing to write a normal run artifact" in capsys.readouterr().err


def test_severity_issue_stdout_and_off_sinks_never_file(tmp_path: Path) -> None:
    root, config = severe_repo(tmp_path, "")  # default sink: stdout
    code, _d, _v, gh = run_pipeline(
        config,
        drafter_outputs=[candidate_json(severity="P1")],
        verifier_outputs=[CONFIRM],
    )
    assert code == 0
    assert gh.created_issues == []
    text = summary_text(root)
    assert "(not filed)" in text

    root2, config2 = severe_repo(tmp_path / "off", "  issue_sink: off\n")
    code, _d2, _v2, gh2 = run_pipeline(
        config2,
        drafter_outputs=[candidate_json(severity="P1")],
        verifier_outputs=[CONFIRM],
        github=FakeGitHub(),
    )
    assert code == 0
    assert gh2.created_issues == []
    assert "## Severe candidates" not in summary_text(root2)


def test_dry_run_never_files_issues_even_with_github_sink(tmp_path: Path, capsys) -> None:
    root, config = severe_repo(tmp_path, "  issue_sink: github\n")
    code, _d, _v, gh = run_pipeline(
        config,
        drafter_outputs=[candidate_json(severity="P1")],
        verifier_outputs=[CONFIRM],
        dry_run=True,
    )
    assert code == 0
    assert gh.created_issues == []
    assert "(not filed)" in capsys.readouterr().out


def test_dry_run_gh_command_log_contains_only_read_subcommands(
    tmp_path: Path, monkeypatch
) -> None:
    root, config = severe_repo(tmp_path, "  issue_sink: github\n")
    before = tree_snapshot(root)
    commands: list[list[str]] = []

    def fake_run(self, command, *, check):
        commands.append(command)
        stdout = "[]" if command[1:3] == ["issue", "list"] else ""
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(GhCliGitHub, "_run", fake_run)
    factory = runners_factory(
        {
            "draft": FakeSeatRunner([candidate_json(severity="P1")]),
            "verify": FakeSeatRunner([CONFIRM]),
        }
    )
    code = run_retro(
        make_args(config, dry_run=True),
        runner_factory=factory,
    )
    assert code == 0
    assert commands
    read_subcommands = {("pr", "list"), ("pr", "diff"), ("pr", "view"), ("issue", "list")}
    assert [command for command in commands if tuple(command[1:3]) not in read_subcommands] == []
    assert tree_snapshot(root) == before


# -- single-writer base check -------------------------------------------------


def test_base_moved_refuses_write_and_fresh_base_writes(tmp_path: Path) -> None:
    root, config = standard_repo(tmp_path)
    gotchas_path = root / "agent_memory" / "gotchas.json"

    def execute(resolver) -> int:
        factory = runners_factory(
            {
                "draft": FakeSeatRunner([candidate_json()]),
                "verify": FakeSeatRunner([CONFIRM]),
            }
        )
        return run_retro(
            make_args(config, base_ref="origin/main", expected_base="abc123"),
            github=FakeGitHub(),
            runner_factory=factory,
            resolve_ref=resolver,
        )

    assert execute(lambda ref: "someone-else-committed") == 1
    assert not gotchas_path.exists()
    assert not (root / "agent_telemetry" / f"retrospective_{WEEK}.md").exists()

    assert execute(lambda ref: "abc123") == 0
    assert gotchas_path.is_file()


def test_base_moved_refuses_issue_creation_too(tmp_path: Path) -> None:
    root, config = severe_repo(tmp_path, "  issue_sink: github\n")
    github = FakeGitHub()
    factory = runners_factory(
        {
            "draft": FakeSeatRunner([candidate_json(severity="P1")]),
            "verify": FakeSeatRunner([CONFIRM]),
        }
    )
    code = run_retro(
        make_args(config, base_ref="origin/main", expected_base="expected"),
        github=github,
        runner_factory=factory,
        resolve_ref=lambda ref: "moved",
    )
    assert code == 1
    assert github.created_issues == []
    assert not (root / "agent_memory" / "gotchas.json").exists()
    assert not (root / "agent_telemetry" / f"retrospective_{WEEK}.md").exists()


@pytest.mark.parametrize(
    ("base_ref", "expected_base"),
    [("origin/main", None), (None, "expected")],
)
def test_base_freshness_arguments_must_be_paired(
    tmp_path: Path, base_ref: str | None, expected_base: str | None
) -> None:
    root, config = standard_repo(tmp_path)
    drafter = FakeSeatRunner([candidate_json()])
    verifier = FakeSeatRunner([CONFIRM])
    code = run_retro(
        make_args(config, base_ref=base_ref, expected_base=expected_base),
        github=FakeGitHub(),
        runner_factory=runners_factory({"draft": drafter, "verify": verifier}),
    )
    assert code == 1
    assert drafter.calls == verifier.calls == 0
    assert not (root / "agent_memory" / "gotchas.json").exists()


def test_run_missing_repository_fails(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "seats.yaml").write_text(
        "schema_version: 1\nseats:\n  draft:\n    provider: a\n    model: m\n"
        "    auth_env: null\n    adapter: shell-command\n    options:\n"
        "      argv: [true]\n",
        encoding="utf-8",
    )
    (root / "corral.yaml").write_text(
        "seats_file: seats.yaml\nretro:\n  drafter_seat: draft\n",
        encoding="utf-8",
    )
    drafter = FakeSeatRunner([])
    code = run_retro(
        make_args(root / "corral.yaml"),
        github=FakeGitHub(),
        runner_factory=runners_factory({"draft": drafter}),
    )
    assert code == 1


# -- bridge security end-to-end ------------------------------------------------


def test_bridge_secret_never_reaches_prompts_or_outputs(tmp_path: Path, capsys) -> None:
    secret = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456"
    root = make_repo(tmp_path, "  bridge:\n    memory_roots: [memory-corpus]\n")
    corpus = root / "memory-corpus" / "proj" / "memory"
    for index, incident in enumerate(("INC-1", "INC-2"), start=1):
        path = corpus / f"note{index}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "---\ntype: feedback\nname: outage note\ndescription: lesson\n"
            f"incident_ref: {incident}\n---\n"
            f"Lesson from the incident: token {secret} leaked via scripts/orders.py\n",
            encoding="utf-8",
        )
    write_fixup_parquet(root, [], WEEK)  # parquet present but empty: bridge-only run
    code, drafter, _v, _gh = run_pipeline(
        root / "corral.yaml",
        drafter_outputs=[candidate_json()],
        verifier_outputs=[CONFIRM],
        dry_run=True,
    )
    assert code == 0
    assert drafter.calls == 1
    prompt = drafter.prompts[0]
    assert secret not in prompt
    assert "<redacted>" in prompt
    assert secret not in capsys.readouterr().out
    assert "memory:" in prompt  # sanitized bridge block reached the drafter


def test_every_source_scrub_pattern_is_blocked_end_to_end(tmp_path: Path, capsys) -> None:
    secrets = [
        "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456",
        "github_pat_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456",
        "sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456",
        "xoxb-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456",
        "AKIAABCDEFGHIJKLMNOP",
        "AIza" + "A" * 35,
        "eyJ" + "A" * 20 + "." + "B" * 20 + "." + "C" * 20,
        "glpat-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456",
        "hf_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456",
        "xai-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456",
        "pat_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456",
        "ya29.ABCDEFGHIJKLMNOPQRSTUVWXYZ123456",
        "Bearer ABCDEFGHIJKLMNOPQRSTUVWXYZ123456",
        "postgres://retro_user:retro_pass@db.internal/app",
        "ENV_ASSIGNMENT_SECRET_1234567890",
        "BLOCK_SECRET_ABCDEFGHIJKLMNOPQRSTUVWXYZ",
        "-----BEGIN RSA PRIVATE KEY-----",
    ]
    secret_payload = "\n".join(
        [
            "Regression credential incident in scripts/orders.py",
            *secrets[:12],
            secrets[12],
            secrets[13],
            f"API_KEY={secrets[14]}",
            "private_key: |",
            f"  {secrets[15]}",
            "-----BEGIN RSA PRIVATE KEY-----",
            "private-material",
            "-----END RSA PRIVATE KEY-----",
        ]
    )
    root = make_repo(
        tmp_path,
        "  bridge:\n"
        "    memory_roots: [memory-corpus]\n"
        "    run_artifact_roots: [run-audits]\n",
    )
    corpus = root / "memory-corpus" / "proj" / "memory"
    for index, body in enumerate((secret_payload, "Regression lesson in scripts/orders.py"), 1):
        path = corpus / f"note{index}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "---\ntype: feedback\nname: outage note\ndescription: regression lesson\n"
            f"incident_ref: MEM-{index}\n---\n{body}\n",
            encoding="utf-8",
        )

    runs = root / "run-audits"
    for index, body in enumerate((secret_payload, "Regression lesson in scripts/orders.py"), 1):
        run_dir = runs / f"run-{index}"
        run_dir.mkdir(parents=True)
        (run_dir / "final_report.md").write_text(
            f"{body}\nincident_ref: RUN-{index}\n",
            encoding="utf-8",
        )

    write_fixup_parquet(root, [], WEEK)
    code, drafter, verifier, _gh = run_pipeline(
        root / "corral.yaml",
        drafter_outputs=[candidate_json(), candidate_json()],
        verifier_outputs=[CONFIRM, CONFIRM],
    )
    assert code == 0
    captured = capsys.readouterr()
    outbound = "\n".join(
        [
            *drafter.prompts,
            *verifier.prompts,
            summary_text(root),
            (root / "agent_memory" / "gotchas.json").read_text(encoding="utf-8"),
            captured.out,
            captured.err,
        ]
    )
    for secret in secrets:
        assert secret not in outbound
    assert "<redacted>" in outbound


# -- small CLI/helper surfaces -------------------------------------------------


def test_week_window_and_fixup_parquet_selection(tmp_path: Path) -> None:
    monday, sunday = week_window("2026-W33")
    assert date.fromisoformat(monday).weekday() == 0
    assert date.fromisoformat(sunday).weekday() == 6
    assert (date.fromisoformat(sunday) - date.fromisoformat(monday)).days == 6
    now_monday, now_sunday = week_window(None)
    today = datetime.now(timezone.utc).date()
    assert date.fromisoformat(now_monday) <= today <= date.fromisoformat(now_sunday)

    (tmp_path / "agent_telemetry").mkdir()
    older = tmp_path / "agent_telemetry" / "fixup_2026-W31.parquet"
    current = tmp_path / "agent_telemetry" / "fixup_2026-W33.parquet"
    older.write_bytes(b"")
    # no week-specific file yet: newest glob match wins
    assert find_fixup_parquet(tmp_path, "agent_telemetry/fixup_*.parquet", "2026-W33") == older
    current.write_bytes(b"")
    assert find_fixup_parquet(tmp_path, "agent_telemetry/fixup_*.parquet", "2026-W33") == current
    assert find_fixup_parquet(tmp_path, "agent_telemetry/fixup_*.parquet", "2026-W32") == current
    assert find_fixup_parquet(tmp_path, "agent_telemetry/none_*.parquet", "2026-W33") is None


def test_revert_refinement_cli(tmp_path: Path, capsys) -> None:
    root = make_repo(tmp_path)
    ledger = root / "agent_memory" / "refinements.jsonl"
    capture = capture_refinement(
        type("P", (), {"target_file": "docs/g.md", "evidence_incidents": ("pr:1", "pr:2")})(),
        before_snapshot="old\n",
        after_snapshot="new\n",
        before_exists=True,
        edit_snapshots=[RefinementEditSnapshot("docs/g.md", "old\n", "new\n", True)],
    )
    record = materialize_records([capture], timestamp="2026-08-03T12:00:00Z")[0]
    append_records(ledger, [record])

    assert main(["retro", "revert-refinement", record.id, "--config", str(root / "corral.yaml")]) == 0
    out = capsys.readouterr().out
    assert "--- a/docs/g.md" in out and "-new" in out and "+old" in out

    assert main(["retro", "revert-refinement", "REF-missing", "--config", str(root / "corral.yaml")]) == 1
    assert "not found" in capsys.readouterr().err


def test_run_cli_requires_repository(tmp_path: Path, capsys) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "corral.yaml").write_text("retro: {}\n", encoding="utf-8")
    assert main(["retro", "run", "--config", str(root / "corral.yaml")]) == 1
    assert "retro.repository" in capsys.readouterr().err
