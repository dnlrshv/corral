"""Deterministic staleness model, surfaces-resolver fix, report, and CLI.

The golden-file scenario (44 synthetic sessions, 6 rules) exercises every
verdict: RETAIN, DEMOTE, MONITOR, INSUFFICIENT_DATA, EXEMPT.
"""

from __future__ import annotations

import argparse
from datetime import timezone, date, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from corral.config import load_config
from corral.governance.config import GovernanceConfig, governance_config_from_mapping
from corral.governance.proposals import parse_proposal_block, validate_proposal_contract
from corral.governance.staleness import model, report, sources
from corral.hooks.surface_check import Surface

AS_OF = date(2026, 7, 6)

CONFIG_YAML = (
    "seats_file: seats.yaml\n"
    "retro:\n"
    "  repository: example/test-repo\n"
    "governance:\n"
    "  registry: instruction_rules.yaml\n"
    "  reviewer: platform-team\n"
    "  staleness:\n"
    '    demote_target_glob: "docs/archive/**"\n'
)

REGISTRY_YAML = """schema_version: 1
rules:
  R-TEST-001:
    file: docs/instructions/core.md
    anchor: api convention anchor text
    concern_key: api-conventions
    modality: MUST
    review_by: platform-team
    selectors:
      paths: [src/api/]
  R-TEST-002:
    file: docs/instructions/legacy.md
    anchor: legacy reporting anchor text
    concern_key: legacy-reporting
    modality: READ
    review_by: platform-team
    selectors:
      paths: [legacy/]
  R-TEST-003:
    file: docs/instructions/pm.md
    anchor: project management anchor text
    concern_key: pm-conventions
    modality: MUST
    review_by: platform-team
    selectors:
      workflows: [pm]
  R-TEST-004:
    file: docs/instructions/rare.md
    anchor: rarely relevant anchor text
    concern_key: rare-concern
    modality: ASK
    review_by: platform-team
    selectors:
      surfaces: [plain-area]
  R-TEST-005:
    file: docs/instructions/payments.md
    anchor: payments console anchor text
    concern_key: payments-console-access
    modality: MUST NOT
    review_by: platform-team
    selectors:
      surfaces: [payments-console]
  R-TEST-006:
    file: docs/instructions/merge.md
    anchor: merge workflow anchor text
    concern_key: merge-workflow
    modality: READ
    review_by: platform-team
    selectors:
      workflows: [pr-merge]
"""

SURFACES_YAML = """surfaces:
  payments-console:
    description: Payments console (human review required)
    paths: [payments/console.py]
    needs_human: true
  plain-area:
    description: Rarely touched area
    paths: [rare/]
    needs_human: false
"""


class FakeGitHub:
    """GitHubClient double resolving merged-PR file sets."""

    def __init__(self, files_by_pr: dict[int, list[str]]) -> None:
        self.repo = "example/test-repo"
        self.files_by_pr = files_by_pr
        self.since_until: list[tuple[str, str]] = []

    def merged_prs(self, since: str, until: str) -> list[dict]:
        self.since_until.append((since, until))
        return [
            {"number": number, "files": [{"path": path} for path in files]}
            for number, files in sorted(self.files_by_pr.items())
        ]

    def pr_diff_excerpt(self, pr_number: int, *, max_chars: int) -> str:
        return ""

    def pr_review_excerpt(self, pr_number: int, *, max_chars: int) -> str:
        return ""

    def open_issues(self, label: str) -> list[dict]:
        return []

    def create_issue(self, title, body, *, labels=(), assignee=None) -> str:
        raise AssertionError("issue filing is not expected in this test")


def make_repo(tmp_path: Path, *, registry_yaml: str = REGISTRY_YAML) -> Path:
    root = tmp_path / "repo"
    root.mkdir(parents=True, exist_ok=True)
    (root / "corral.yaml").write_text(CONFIG_YAML, encoding="utf-8")
    (root / "instruction_rules.yaml").write_text(registry_yaml, encoding="utf-8")
    (root / "surfaces.yaml").write_text(SURFACES_YAML, encoding="utf-8")
    return root


def session_rows() -> tuple[list[dict], dict[int, list[str]]]:
    """45 sessions: 27 with resolved path data, 18 without.

    The 27-with-data count is deliberately below ``min_sessions`` (30) so
    path/surface-scoped rules land INSUFFICIENT_DATA, while workflow-scoped
    rules (denominator = all sessions) can still DEMOTE/MONITOR -- the full
    verdict matrix in one deterministic scenario.
    """
    rows: list[dict] = []
    files_by_pr: dict[int, list[str]] = {}
    api_kinds = ["fix-issue", "pr-review"]
    for i in range(22):
        when = datetime(2026, 5, 1, tzinfo=timezone.utc) + _days(i)
        pr = 1001 + i
        rows.append(_row(f"s-api-{i}", api_kinds[i % 2], pr, when))
        files_by_pr[pr] = ["src/api/orders.py"]
    for i, day in enumerate([datetime(2026, 2, 15), datetime(2026, 3, 1), datetime(2026, 3, 20)]):
        pr = 3001 + i
        rows.append(_row(f"s-legacy-{i}", "pr-merge", pr, day.replace(tzinfo=timezone.utc)))
        files_by_pr[pr] = ["legacy/reporting.py"]
    for i in range(2):
        when = datetime(2026, 6, 10, tzinfo=timezone.utc) + _days(i)
        pr = 4001 + i
        rows.append(_row(f"s-merge-{i}", "pr-merge", pr, when))
        files_by_pr[pr] = ["docs/notes.md"]
    for i in range(18):  # unmerged/unresolvable PRs -> no path data
        when = datetime(2026, 5, 20, tzinfo=timezone.utc) + _days(i)
        rows.append(_row(f"s-nodata-{i}", api_kinds[i % 2], None, when))
    return rows, files_by_pr


def _days(n: int):
    from datetime import timedelta

    return timedelta(days=n)


def _row(session_id: str, workflow_kind: str, pr_number: int, when: datetime) -> dict:
    return {
        "session_id": session_id,
        "workflow_kind": workflow_kind,
        "pr_number": pr_number,
        "started_at": when,
        "ended_at": when,
    }


def write_rollup(root: Path, rows: list[dict]) -> None:
    telemetry_dir = root / "agent_telemetry"
    telemetry_dir.mkdir(exist_ok=True)
    table = pa.table(
        {
            "session_id": [r["session_id"] for r in rows],
            "workflow_kind": [r["workflow_kind"] for r in rows],
            "pr_number": pa.array(
                [r["pr_number"] if r["pr_number"] is not None else None for r in rows],
                type=pa.int64(),
            ),
            "started_at": pa.array(
                [r["started_at"] for r in rows], type=pa.timestamp("us", tz="UTC")
            ),
            "ended_at": pa.array(
                [r["ended_at"] for r in rows], type=pa.timestamp("us", tz="UTC")
            ),
        }
    )
    pq.write_table(table, telemetry_dir / "rollup_2026-W27.parquet")


def run_full(tmp_path: Path, *, dry_run: bool = True) -> tuple[object, FakeGitHub]:
    root = make_repo(tmp_path)
    rows, files_by_pr = session_rows()
    write_rollup(root, rows)
    config = load_config(root / "corral.yaml")
    github = FakeGitHub(files_by_pr)
    run = sources.run_staleness(as_of=AS_OF, config=config, github=github, dry_run=dry_run)
    return run, github


# ---------------------------------------------------------------------------
# model: workflow-kind normalization + honest denominators
# ---------------------------------------------------------------------------


def test_normalize_workflow_kind_canonical_and_heuristic() -> None:
    assert model.normalize_workflow_kind("fix-issue") == "fix-issue"
    assert model.normalize_workflow_kind("PR Review") == "pr-review"
    assert model.normalize_workflow_kind("auto_merge_lane") == "pr-merge"
    assert model.normalize_workflow_kind("weekly triage job") == "pr-triage"
    assert model.normalize_workflow_kind("claude: fix issue") == "fix-issue"
    assert model.normalize_workflow_kind("provider project management") == "pm"
    assert model.normalize_workflow_kind("") == "unknown"
    assert model.normalize_workflow_kind(None) == "unknown"
    assert model.normalize_workflow_kind("data pipeline") == "unknown"


def _rule(selectors: model.Selectors, *, rule_id: str = "R-TEST-001") -> model.Rule:
    return model.Rule(
        rule_id=rule_id,
        file="docs/instructions/core.md",
        anchor="distinctive anchor text",
        concern_key="concern",
        modality="MUST",
        selectors=selectors,
        review_by="platform-team",
    )


def test_evaluate_session_honest_denominator() -> None:
    universal = _rule(model.Selectors())
    path_only = _rule(model.Selectors(paths=("src/api/",)))
    wf_only = _rule(model.Selectors(workflows=("fix-issue",)))
    pathless = model.Session("s1", "fix-issue", AS_OF, touched_paths=None)
    with_paths = model.Session(
        "s2", "pr-review", AS_OF, touched_paths=frozenset({"src/api/orders.py"})
    )
    assert model.evaluate_session(universal, pathless) is True
    # Path-less session: workflow rules evaluable, path rules excluded (None).
    assert model.evaluate_session(wf_only, pathless) is True
    assert model.evaluate_session(path_only, pathless) is None
    # Path session: prefix match decides.
    assert model.evaluate_session(path_only, with_paths) is True
    assert model.evaluate_session(wf_only, with_paths) is False


def test_surface_resolver_session_match_direction() -> None:
    """A surface selector matches sessions touching the surface's PATHS."""
    surfaces = [
        Surface(
            name="payments-console",
            description="",
            paths=["payments/console.py"],
            line_ranges=[],
            needs_human=True,
        )
    ]
    surface_paths, known = sources.build_surface_resolver(surfaces)
    assert known == frozenset({"payments-console"})
    rule = _rule(model.Selectors(surfaces=("payments-console",)))
    touching = model.Session(
        "s1", "fix-issue", AS_OF, touched_paths=frozenset({"payments/console.py"})
    )
    other = model.Session(
        "s2", "fix-issue", AS_OF, touched_paths=frozenset({"src/elsewhere.py"})
    )
    assert model.evaluate_session(rule, touching, surface_paths) is True
    assert model.evaluate_session(rule, other, surface_paths) is False
    # Without the resolved mapping, surface evaluation fails loudly.
    with pytest.raises(ValueError, match="resolved surface-path mapping"):
        model.evaluate_session(rule, touching, None)


def test_surface_resolver_exemption_direction() -> None:
    """A rule selecting a needs_human surface ID is exempt."""
    surfaces = [
        Surface(
            name="payments-console",
            description="",
            paths=["payments/console.py"],
            line_ranges=[],
            needs_human=True,
        ),
        Surface(name="plain", description="", paths=["plain.py"], line_ranges=[], needs_human=False),
    ]
    ctx = sources.load_exemption_context(surfaces)
    assert ctx.needs_human_surface_ids == frozenset({"payments-console"})
    assert ctx.needs_human_surface_paths == ("payments/console.py",)
    by_surface = _rule(model.Selectors(surfaces=("payments-console",)))
    by_path = _rule(model.Selectors(paths=("payments/console.py",)))
    by_plain = _rule(model.Selectors(surfaces=("plain",)))
    assert model.classify_exemption(by_surface, ctx) is not None
    assert model.classify_exemption(by_path, ctx) is not None
    assert model.classify_exemption(by_plain, ctx) is None


def test_surface_resolver_overlap_and_empty_paths_share_one_resolution() -> None:
    surfaces = [
        Surface("alpha", "", ["shared/path.py"], [], False),
        Surface("human", "", ["shared/path.py"], [], True),
        Surface("empty-human", "", [], [], True),
    ]
    surface_paths, known = sources.build_surface_resolver(surfaces)
    ctx = sources.load_exemption_context(surfaces, surface_paths=surface_paths)

    assert known == frozenset({"alpha", "human", "empty-human"})
    assert surface_paths["empty-human"] == frozenset()
    assert ctx.needs_human_surface_paths == tuple(sorted(surface_paths["human"]))
    assert ctx.needs_human_surface_ids == frozenset({"human", "empty-human"})

    touching = model.Session(
        "s", "fix-issue", AS_OF, touched_paths=frozenset({"shared/path.py"})
    )
    assert model.evaluate_session(
        _rule(model.Selectors(surfaces=("alpha",))), touching, surface_paths
    ) is True
    assert model.evaluate_session(
        _rule(model.Selectors(surfaces=("human",))), touching, surface_paths
    ) is True
    empty_rule = _rule(model.Selectors(surfaces=("empty-human",)))
    assert model.evaluate_session(empty_rule, touching, surface_paths) is False
    assert model.classify_exemption(empty_rule, ctx) is not None


def test_unknown_surface_id_is_a_validation_error() -> None:
    surfaces = [
        Surface(name="known-surface", description="", paths=["a.py"], line_ranges=[], needs_human=False)
    ]
    with pytest.raises(sources.UnknownSurfaceError, match="ghost-surface"):
        sources.resolve_surface_paths(surfaces, ["known-surface", "ghost-surface"])
    rules = {"R-1": _rule(model.Selectors(surfaces=("ghost-surface",)))}
    assert model.validate_rule_surface_ids(rules, frozenset({"known-surface"})) == ["ghost-surface"]
    with pytest.raises(ValueError, match="unknown surface ids"):
        model.analyze(
            rules,
            [],
            as_of=AS_OF,
            cfg=GovernanceConfig().staleness,
            exemption_ctx=model.ExemptionContext(frozenset(), ()),
            surface_paths={"known-surface": frozenset({"a.py"})},
        )
    # Exemption membership cannot mask an unknown ID: the shared validation
    # happens before either exemption or session evaluation.
    with pytest.raises(ValueError, match="unknown surface ids"):
        model.analyze(
            rules,
            [],
            as_of=AS_OF,
            cfg=GovernanceConfig().staleness,
            exemption_ctx=model.ExemptionContext(frozenset({"ghost-surface"}), ()),
            surface_paths={"known-surface": frozenset({"a.py"})},
        )


# ---------------------------------------------------------------------------
# model: verdict matrix
# ---------------------------------------------------------------------------


def _sessions_matrix() -> list[model.Session]:
    sessions = []
    for i in range(8):
        sessions.append(
            model.Session(
                f"ret-{i}",
                "fix-issue" if i % 2 else "pr-review",
                AS_OF - _days(10 + i),
                frozenset({"src/api/orders.py"}),
            )
        )
    for i in range(40):
        sessions.append(
            model.Session(
                f"fill-{i}", "pr-merge", AS_OF - _days(20 + i), frozenset({"unrelated/file.py"})
            )
        )
    for i in range(2):
        sessions.append(
            model.Session(
                f"old-{i}", "pr-merge", AS_OF - _days(120 + i), frozenset({"legacy/reporting.py"})
            )
        )
    return sessions


def _cfg(**overrides):
    cfg = GovernanceConfig().staleness
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


NO_EXEMPT = model.ExemptionContext(frozenset(), ())
EMPTY_SURFACES: dict[str, frozenset[str]] = {}


def test_verdict_retain_requires_rate_and_breadth() -> None:
    rule = _rule(model.Selectors(paths=("src/api/",)))
    sessions = _sessions_matrix()
    verdict = model.classify_rule(
        rule, sessions, as_of=AS_OF, cfg=_cfg(), exemption_ctx=NO_EXEMPT, surface_paths=EMPTY_SURFACES
    )
    # 8/50 = 16% < 20% default retain rate -> not retained despite breadth.
    assert verdict.verdict == model.MONITOR
    lowered = model.classify_rule(
        rule, sessions, as_of=AS_OF, cfg=_cfg(retain_rate=0.10),
        exemption_ctx=NO_EXEMPT, surface_paths=EMPTY_SURFACES,
    )
    assert lowered.verdict == model.RETAIN
    # Breadth guard: one workflow kind only -> not retained even at high rate.
    narrow_sessions = [
        model.Session(f"a-{i}", "fix-issue", AS_OF - _days(i), frozenset({"src/api/x.py"}))
        for i in range(10)
    ]
    narrow = model.classify_rule(
        rule, narrow_sessions, as_of=AS_OF, cfg=_cfg(retain_workflow_count=2),
        exemption_ctx=NO_EXEMPT, surface_paths=EMPTY_SURFACES,
    )
    assert narrow.verdict != model.RETAIN


def test_verdict_demote_needs_floor_and_samples() -> None:
    legacy = _rule(model.Selectors(paths=("legacy/",)))
    verdict = model.classify_rule(
        legacy, _sessions_matrix(), as_of=AS_OF, cfg=_cfg(min_sessions=2),
        exemption_ctx=NO_EXEMPT, surface_paths=EMPTY_SURFACES,
    )
    # 2/50 = 4% < 10% over the long window, n=50 >= 2 -> DEMOTE.
    assert verdict.verdict == model.DEMOTE
    # Same data but a 30-session floor -> INSUFFICIENT_DATA (only 50 sessions
    # total; shrink the matrix to trip the floor).
    small_matrix = _sessions_matrix()[:12]  # 8 api + 4 fillers
    insufficient = model.classify_rule(
        legacy, small_matrix, as_of=AS_OF, cfg=_cfg(),
        exemption_ctx=NO_EXEMPT, surface_paths=EMPTY_SURFACES,
    )
    assert insufficient.verdict == model.INSUFFICIENT_DATA


def test_verdict_monitor_band_between_thresholds() -> None:
    rule = _rule(model.Selectors(paths=("legacy/",)))
    sessions = [
        model.Session(f"s-{i}", "pr-merge", AS_OF - _days(i), frozenset({"legacy/reporting.py"}))
        for i in range(15)
    ] + [
        model.Session(f"n-{i}", "pr-merge", AS_OF - _days(i), frozenset({"unrelated/x.py"}))
        for i in range(85)
    ]
    verdict = model.classify_rule(
        rule, sessions, as_of=AS_OF, cfg=_cfg(),
        exemption_ctx=NO_EXEMPT, surface_paths=EMPTY_SURFACES,
    )
    # 15/100 = 15%: above the 10% demote floor, below the 20% retain ceiling.
    assert verdict.verdict == model.MONITOR


def test_recent_retain_protects_against_long_window_demote() -> None:
    """Hysteresis: a rule retained on the recent window is never demoted even
    when the long window says low."""
    rule = _rule(model.Selectors(paths=("src/api/",), workflows=("fix-issue",)))
    sessions = [
        model.Session(f"hot-{i}", "fix-issue", AS_OF - _days(i), frozenset({"src/api/x.py"}))
        for i in range(12)
    ] + [
        model.Session(f"cold-{i}", "pr-merge", AS_OF - _days(95 + i), frozenset({"unrelated/y.py"}))
        for i in range(88)
    ]
    verdict = model.classify_rule(
        rule, sessions, as_of=AS_OF, cfg=_cfg(retain_workflow_count=1),
        exemption_ctx=NO_EXEMPT, surface_paths=EMPTY_SURFACES,
    )
    assert verdict.verdict == model.RETAIN


def test_exempt_rule_is_never_demoted() -> None:
    rule = _rule(model.Selectors(paths=("legacy/",)))
    ctx = model.ExemptionContext(frozenset(), ("legacy/reporting.py",))
    verdict = model.classify_rule(
        rule, _sessions_matrix(), as_of=AS_OF, cfg=_cfg(min_sessions=2),
        exemption_ctx=ctx, surface_paths=EMPTY_SURFACES,
    )
    assert verdict.verdict == model.EXEMPT
    assert verdict.exemption_reason is not None


# ---------------------------------------------------------------------------
# full pipeline + report
# ---------------------------------------------------------------------------


def test_full_pipeline_verdict_matrix(tmp_path: Path) -> None:
    run, github = run_full(tmp_path)
    verdicts = {v.rule_id: v.verdict for v in run.result.verdicts}
    assert verdicts == {
        "R-TEST-001": model.RETAIN,
        "R-TEST-002": model.INSUFFICIENT_DATA,
        "R-TEST-003": model.DEMOTE,
        "R-TEST-004": model.INSUFFICIENT_DATA,
        "R-TEST-005": model.EXEMPT,
        "R-TEST-006": model.MONITOR,
    }
    assert run.result.total_sessions_long == 45
    assert run.result.sessions_with_path_data_long == 27
    # 27 of 45 sessions carry resolved path data -> honest coverage 0.6.
    assert run.result.coverage_fraction_long == pytest.approx(27 / 45)
    # PR resolution went through the GitHubClient protocol with the window.
    assert github.since_until == [("2026-01-07", "2026-07-06")]


def test_report_golden_file(tmp_path: Path) -> None:
    run, _ = run_full(tmp_path)
    golden = Path(__file__).parent / "fixtures" / "staleness_report_golden.md"
    assert run.report_markdown == golden.read_text(encoding="utf-8")

    # Parse the COMPLETE report, not an extracted/reimplemented YAML fragment.
    block, parse_errors = parse_proposal_block(run.report_markdown)
    assert parse_errors == [] and block is not None
    assert validate_proposal_contract(block, load_config(tmp_path / "repo/corral.yaml").governance.proposals) == []


def test_report_consolidates_multiple_candidates_into_one_real_gate_block() -> None:
    cfg = GovernanceConfig()
    cfg.reviewer = "platform-team"
    cfg.staleness.demote_target_glob = "docs/archive/**"
    verdicts = []
    for index in range(2):
        rule = _rule(
            model.Selectors(workflows=("pm",)), rule_id=f"R-TEST-10{index}"
        )
        verdicts.append(
            model.RuleVerdict(
                rule=rule,
                verdict=model.DEMOTE,
                exemption_reason=None,
                retain_window=model.WindowStats(window_days=90),
                demote_window=model.WindowStats(window_days=180, denominator=40),
            )
        )
    result = model.AnalysisResult(
        as_of=AS_OF,
        quarter="2026-Q3",
        total_sessions_long=40,
        total_sessions_recent=20,
        sessions_with_path_data_long=0,
        workflow_kind_counts={"pm": 40},
        verdicts=verdicts,
    )
    markdown = report.render_report_markdown(
        result, cfg=cfg, repo="example/test-repo", dry_run=False
    )
    assert markdown.count("```yaml") == 1
    block, parse_errors = parse_proposal_block(markdown)
    assert parse_errors == [] and block is not None
    assert len(block["proposals"]) == 2
    assert validate_proposal_contract(block, cfg.proposals) == []


def test_executable_pointer_without_reviewer_is_explicitly_withheld() -> None:
    cfg = GovernanceConfig()
    rule = _rule(model.Selectors(paths=("src/",)))
    verdict = model.RuleVerdict(
        rule=rule,
        verdict=model.MONITOR,
        exemption_reason=None,
        retain_window=model.WindowStats(window_days=90),
        demote_window=model.WindowStats(window_days=180, denominator=40, numerator=8),
        executable_control={"control_type": "lint", "control_path": "tools/check.py"},
    )
    result = model.AnalysisResult(
        AS_OF, "2026-Q3", 40, 20, 40, {"fix-issue": 40}, [verdict]
    )
    markdown = report.render_report_markdown(
        result, cfg=cfg, repo="example/test-repo", dry_run=False
    )
    assert "governance.reviewer` is not configured" in markdown
    assert "```yaml" not in markdown


def test_report_sparse_telemetry_honesty(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    rows, files_by_pr = session_rows()
    write_rollup(root, rows[:5])  # far below min_sessions=30
    config = load_config(root / "corral.yaml")
    run = sources.run_staleness(
        as_of=AS_OF, config=config, github=FakeGitHub(files_by_pr), dry_run=True
    )
    assert "Sparse telemetry" in run.report_markdown
    assert "min_sessions" in run.report_markdown
    # Sparse data must not produce demotion verdicts.
    assert run.result.demotion_candidates == []


def test_report_withholds_blocks_without_target_or_reviewer(tmp_path: Path) -> None:
    run, _ = run_full(tmp_path)
    assert "proposals:" in run.report_markdown  # configured -> blocks emitted
    # No demote_target_glob: candidates listed, blocks withheld.
    root = make_repo(tmp_path / "no-target")
    (root / "corral.yaml").write_text(
        CONFIG_YAML.replace('    demote_target_glob: "docs/archive/**"\n', ""), encoding="utf-8"
    )
    (root / "instruction_rules.yaml").write_text(REGISTRY_YAML, encoding="utf-8")
    (root / "surfaces.yaml").write_text(SURFACES_YAML, encoding="utf-8")
    rows, files_by_pr = session_rows()
    write_rollup(root, rows)
    config = load_config(root / "corral.yaml")
    run = sources.run_staleness(
        as_of=AS_OF, config=config, github=FakeGitHub(files_by_pr), dry_run=True
    )
    assert "demote_target_glob` is not configured" in run.report_markdown
    assert "operation: demote" not in run.report_markdown


def test_demotion_proposal_passes_gate_contract() -> None:
    cfg = GovernanceConfig()
    cfg.reviewer = "platform-team"
    cfg.staleness.demote_target_glob = "docs/archive/**"
    rule = _rule(model.Selectors(paths=("legacy/",)))
    verdict = model.RuleVerdict(
        rule=rule,
        verdict=model.DEMOTE,
        exemption_reason=None,
        retain_window=model.WindowStats(window_days=90),
        demote_window=model.WindowStats(window_days=180, denominator=44, numerator=3),
    )
    proposal = report.render_demotion_proposal(verdict, cfg=cfg, reviewer="platform-team")
    assert proposal["target_tier"] == "wiki"
    assert report.validate_proposal_through_gate(proposal, cfg.proposals) == []


# ---------------------------------------------------------------------------
# CLI (write-safety discipline)
# ---------------------------------------------------------------------------


def _staleness_args(tmp_path: Path, **overrides) -> argparse.Namespace:
    defaults = dict(
        config=tmp_path / "repo" / "corral.yaml",
        root=None,
        as_of=AS_OF,
        output=None,
        issue_sink="stdout",
        dry_run=False,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_cli_dry_run_prints_and_writes_nothing(tmp_path: Path, capsys, monkeypatch) -> None:
    root = make_repo(tmp_path)
    rows, files_by_pr = session_rows()
    write_rollup(root, rows)
    github = FakeGitHub(files_by_pr)
    monkeypatch.setattr(
        "corral.governance.cli._staleness_github_client", lambda config: github
    )
    from corral.governance.cli import run_staleness_command

    exit_code = run_staleness_command(_staleness_args(tmp_path, dry_run=True))
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "Instruction Staleness Report" in out
    assert "DRY RUN: report not written, no issues filed" in out
    assert not list(root.rglob("instruction_staleness_*.md"))


def test_cli_output_writes_file(tmp_path: Path, capsys, monkeypatch) -> None:
    root = make_repo(tmp_path)
    rows, files_by_pr = session_rows()
    write_rollup(root, rows)
    monkeypatch.setattr(
        "corral.governance.cli._staleness_github_client",
        lambda config: FakeGitHub(files_by_pr),
    )
    from corral.governance.cli import run_staleness_command

    exit_code = run_staleness_command(
        _staleness_args(tmp_path, output=Path("out/staleness.md"))
    )
    assert exit_code == 0
    written = root / "out" / "staleness.md"
    assert written.is_file()
    assert "Instruction Staleness Report" in written.read_text()


def test_cli_output_refuses_instruction_and_proposal_destinations(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    root = make_repo(tmp_path)
    rows, files_by_pr = session_rows()
    write_rollup(root, rows)
    monkeypatch.setattr(
        "corral.governance.cli._staleness_github_client",
        lambda config: FakeGitHub(files_by_pr),
    )
    from corral.governance.cli import run_staleness_command

    registry = root / "instruction_rules.yaml"
    before = registry.read_bytes()
    assert run_staleness_command(
        _staleness_args(tmp_path, output=Path("instruction_rules.yaml"))
    ) == 2
    assert registry.read_bytes() == before
    assert "refusing to write the report" in capsys.readouterr().err

    assert run_staleness_command(
        _staleness_args(tmp_path, output=Path("docs/archive/report.md"))
    ) == 2
    assert not (root / "docs/archive/report.md").exists()


def test_cli_issue_sink_github_files_demotion_issues(tmp_path: Path, capsys, monkeypatch) -> None:
    root = make_repo(tmp_path)
    rows, files_by_pr = session_rows()
    write_rollup(root, rows)

    class FilingGitHub(FakeGitHub):
        def __init__(self, files_by_pr):
            super().__init__(files_by_pr)
            self.filed: list[tuple[str, str]] = []

        def create_issue(self, title, body, *, labels=(), assignee=None) -> str:
            self.filed.append((title, body))
            return f"https://github.com/example/test-repo/issues/{len(self.filed)}"

    github = FilingGitHub(files_by_pr)
    monkeypatch.setattr("corral.governance.cli._staleness_github_client", lambda config: github)
    from corral.governance.cli import run_staleness_command

    exit_code = run_staleness_command(_staleness_args(tmp_path, issue_sink="github"))
    assert exit_code == 0
    assert len(github.filed) == 1  # only R-TEST-003 cleared the demotion bar
    titles = [title for title, _ in github.filed]
    assert titles == ["[instruction-governance] Demote R-TEST-003 (staleness 2026-Q3)"]
    for _, body in github.filed:
        assert "proposals:" in body  # validated proposal block carried in the body


def test_cli_github_sink_never_files_a_gate_invalid_proposal(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    root = make_repo(tmp_path)
    config_text = (root / "corral.yaml").read_text(encoding="utf-8").replace(
        "  staleness:\n", "  proposals:\n    operations: [sharpen]\n  staleness:\n"
    )
    (root / "corral.yaml").write_text(config_text, encoding="utf-8")
    rows, files_by_pr = session_rows()
    write_rollup(root, rows)

    class FilingGitHub(FakeGitHub):
        def __init__(self, files_by_pr):
            super().__init__(files_by_pr)
            self.filed = []

        def create_issue(self, title, body, *, labels=(), assignee=None):
            self.filed.append((title, body))
            return "https://example.invalid/issues/1"

    github = FilingGitHub(files_by_pr)
    monkeypatch.setattr("corral.governance.cli._staleness_github_client", lambda config: github)
    from corral.governance.cli import run_staleness_command

    assert run_staleness_command(_staleness_args(tmp_path, issue_sink="github")) == 0
    assert github.filed == []
    assert "proposal failed the governance contract" in capsys.readouterr().out


def test_cli_unknown_surface_id_exits_two(tmp_path: Path, monkeypatch) -> None:
    registry = REGISTRY_YAML.replace(
        "      surfaces: [payments-console]", "      surfaces: [ghost-surface]"
    )
    root = make_repo(tmp_path, registry_yaml=registry)
    rows, files_by_pr = session_rows()
    write_rollup(root, rows)
    monkeypatch.setattr(
        "corral.governance.cli._staleness_github_client",
        lambda config: FakeGitHub(files_by_pr),
    )
    from corral.governance.cli import run_staleness_command

    exit_code = run_staleness_command(_staleness_args(tmp_path))
    assert exit_code == 2


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


def test_staleness_config_defaults_and_validation() -> None:
    cfg = governance_config_from_mapping({})
    s = cfg.staleness
    assert (s.retain_rate, s.retain_days, s.retain_workflow_count) == (0.20, 90, 2)
    assert (s.demote_rate, s.demote_days, s.min_sessions) == (0.10, 180, 30)
    assert s.demote_target_glob is None
    assert cfg.reviewer is None

    cfg = governance_config_from_mapping(
        {"reviewer": "platform-team", "staleness": {"retain_days": 30, "demote_days": 60}}
    )
    assert cfg.reviewer == "platform-team"
    assert cfg.staleness.retain_days == 30

    with pytest.raises(ValueError, match="demote_days"):
        governance_config_from_mapping({"staleness": {"demote_days": 10}})
    with pytest.raises(ValueError, match="demote_rate"):
        governance_config_from_mapping({"staleness": {"demote_rate": 0.5}})
    with pytest.raises(ValueError, match="min_sessions"):
        governance_config_from_mapping({"staleness": {"min_sessions": 0}})
    with pytest.raises(ValueError, match="reviewer"):
        governance_config_from_mapping({"reviewer": "   "})
