"""Doc/skill proposal pass: normalize -> gate-validate -> plan -> summary.

Hermetic: fake seats, no network, no gh. The round-trip asserts the
human-review-only contract end to end.
"""

from __future__ import annotations

import json
from datetime import timezone, datetime
from pathlib import Path

import pytest

from corral.config import load_config
from corral.governance.proposals import parse_proposal_block, validate_proposal_contract
from corral.governance.registry import parse_registry
from corral.retro.proposals import models, normalize, plan
from corral.retro.summary import PROPOSAL_HUMAN_REVIEW_MARKER, render_summary
from corral.retro.types import EvidenceGroup, FixupPairContext

from .retro_support import SEATS_YAML, FakeSeatRunner, runners_factory

CONFIG_YAML = (
    "seats_file: seats.yaml\n"
    "retro:\n"
    "  drafter_seat: draft\n"
    "  verifier_seats: [verify]\n"
    "  repository: example/test-repo\n"
    "  confidence_threshold: 0.5\n"
    "  proposals:\n"
    "    enabled: true\n"
    "    max: 3\n"
    "    min_incidents: 2\n"
    '    target_globs: ["docs/instructions/**", "skills/**"]\n'
    "governance:\n"
    "  registry: instruction_rules.yaml\n"
    "  reviewer: platform-team\n"
    '  instruction_globs: ["docs/instructions/**"]\n'
    '  protected_paths: ["corral/governance/**"]\n'
    "  replay:\n"
    "    manifest: instruction_manifest.yaml\n"
)

MANIFEST_YAML = """schema_version: 1
token_estimate:
  method: chars_div_4
  chars_per_token: 4
  baselined_against_sha: "0000000000000000000000000000000000000000"
  baselined_on: "2026-08-10"
budgets:
  always_loaded_bundle_target_tokens: 5000
  always_loaded_bundle_hard_tokens: 8000
  always_loaded_single_file_hard_tokens: 8000
  trigger_loaded_unit_hard_tokens: 6000
  skill_body_hard_tokens: 800
  skill_count_warn: 3
  skill_count_hard: 5
  budget_debt_max_days: 14
profiles:
  example:
    entrypoint: core
    description: Test profile.
units:
  core:
    path: docs/instructions/core.md
    kind: always_loaded
bundle_ratchets: {}
skills: {}
budget_debt: []
"""


def make_repo(tmp_path: Path, *, registry_text: str = "") -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "seats.yaml").write_text(SEATS_YAML, encoding="utf-8")
    (root / "corral.yaml").write_text(CONFIG_YAML, encoding="utf-8")
    (root / "instruction_manifest.yaml").write_text(MANIFEST_YAML, encoding="utf-8")
    (root / "instruction_rules.yaml").write_text(registry_text, encoding="utf-8")
    instructions = root / "docs" / "instructions"
    instructions.mkdir(parents=True)
    (instructions / "core.md").write_text("# Core instructions\n", encoding="utf-8")
    return root


def make_group(*, key: str = "claude::src") -> EvidenceGroup:
    merged = datetime(2026, 8, 3, tzinfo=timezone.utc)

    def pair(original: int, fixup: int) -> FixupPairContext:
        return FixupPairContext(
            original_pr=original,
            original_author="agent",
            fixup_pr=fixup,
            fixup_author="human",
            days_between=2.0,
            shared_files=("src/orders.py",),
            agent="claude",
            area="src",
            original_title=f"original {original}",
            fixup_title=f"fix {fixup}",
        )

    return EvidenceGroup(key=key, agent="claude", area="src", pairs=(pair(101, 201), pair(102, 202)))


PROPOSAL_PAYLOAD = {
    "should_propose": True,
    "operation": "add_rule",
    "target_tier": "core",
    "target_file": "docs/instructions/core.md",
    "concern_key": "order-confirmation-flow",
    "modality": "MUST",
    "statement": (
        "Always confirm the order id before retrying a failed submit via the "
        "confirm_order endpoint"
    ),
    "anchor": "confirm_order endpoint",
    "selectors": {"paths": ["src/orders.py"]},
    "supersedes": [],
    "existing_rules_considered": [],
    "why_sharpen_is_insufficient": "no existing rule covers order confirmation",
    "confidence": 0.9,
    "rationale": "two fix-ups re-added the same confirmation call",
}


def make_norm_args(root: Path, *, registry_text: str = "", min_incidents: int = 2):
    config = load_config(root / "corral.yaml")
    registry = parse_registry(registry_text, config.governance)
    return {
        "base_registry": registry,
        "allocate_rule_id": normalize.make_rule_id_allocator(registry),
        "governance": config.governance,
        "target_globs": config.retro.proposals.target_globs,
        "reviewer": config.governance.reviewer,
        "min_incidents": min_incidents,
    }


# ---------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------


def test_tier_rank_orders_by_configured_ladder() -> None:
    tiers = ["executable", "core", "workflow_prompt", "topic_file", "gotcha", "skill", "wiki"]
    assert models.tier_rank("core", tiers) == 1
    assert models.tier_rank("wiki", tiers) == 6
    assert models.tier_rank("unknown-tier", tiers) == len(tiers)
    # A shorter adopter ladder re-ranks the same tiers.
    assert models.tier_rank("skill", ["core", "skill"]) == 1


def test_check_skill_eligibility_floors() -> None:
    ok, reason = models.check_skill_eligibility(completed_tasks=3, distinct_weeks=2, stable_trigger="x")
    assert ok and reason == "eligible"
    ok, reason = models.check_skill_eligibility(completed_tasks=2, distinct_weeks=2, stable_trigger="x")
    assert not ok and "completed tasks" in reason
    ok, reason = models.check_skill_eligibility(completed_tasks=3, distinct_weeks=1, stable_trigger="x")
    assert not ok and "distinct weeks" in reason
    ok, reason = models.check_skill_eligibility(completed_tasks=3, distinct_weeks=2, stable_trigger="  ")
    assert not ok and "trigger" in reason


# ---------------------------------------------------------------------------
# normalize
# ---------------------------------------------------------------------------


def test_normalize_declines_when_should_propose_false(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    args = make_norm_args(root)
    out = normalize.normalize_proposal({"should_propose": False}, make_group(), **args)
    assert out is None


def test_normalize_add_rule_happy_path(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    args = make_norm_args(root)
    proposal = normalize.normalize_proposal(dict(PROPOSAL_PAYLOAD), make_group(), **args)
    assert proposal is not None
    assert proposal.rule_id == "R-RETRO-0001"
    assert proposal.review_by == "platform-team"  # from governance.reviewer
    assert proposal.evidence_incidents == ["#101", "#102"]
    assert proposal.rule_text.startswith("- **MUST** ")


def test_normalize_rejects_short_anchor(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    args = make_norm_args(root)
    payload = dict(PROPOSAL_PAYLOAD, anchor="tiny")
    with pytest.raises(models.ProposalRejectedError, match="anchor too short"):
        normalize.normalize_proposal(payload, make_group(), **args)


def test_normalize_rejects_anchor_not_in_statement(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    args = make_norm_args(root)
    payload = dict(PROPOSAL_PAYLOAD, anchor="not present in the sentence")
    with pytest.raises(models.ProposalRejectedError, match="verbatim substring"):
        normalize.normalize_proposal(payload, make_group(), **args)


def test_normalize_rejects_unknown_operation_and_tier(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    args = make_norm_args(root)
    with pytest.raises(models.ProposalRejectedError, match="unsupported operation"):
        normalize.normalize_proposal(dict(PROPOSAL_PAYLOAD, operation="demote"), make_group(), **args)
    with pytest.raises(models.ProposalRejectedError, match="untargetable tier"):
        normalize.normalize_proposal(
            dict(PROPOSAL_PAYLOAD, target_tier="executable"), make_group(), **args
        )


def test_normalize_rejects_protected_path(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    args = make_norm_args(root)
    payload = dict(PROPOSAL_PAYLOAD, target_file="corral/governance/cli.py")
    with pytest.raises(models.ProposalRejectedError, match="protected governance path"):
        normalize.normalize_proposal(payload, make_group(), **args)


def test_normalize_rejects_off_ladder_target_and_empty_globs(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    args = make_norm_args(root)
    payload = dict(PROPOSAL_PAYLOAD, target_file="src/orders.py")
    with pytest.raises(models.ProposalRejectedError, match="not an editable instruction file"):
        normalize.normalize_proposal(payload, make_group(), **args)
    args["target_globs"] = []
    with pytest.raises(models.ProposalRejectedError, match="target_globs is empty"):
        normalize.normalize_proposal(dict(PROPOSAL_PAYLOAD), make_group(), **args)


def test_normalize_sharpen_uses_registry_and_rejects_noop(tmp_path: Path) -> None:
    registry_text = (
        "schema_version: 1\nrules:\n  R-RETRO-0001:\n"
        "    file: docs/instructions/core.md\n"
        "    anchor: old distinctive anchor\n"
        "    concern_key: order-confirmation-flow\n"
        "    modality: MUST\n    review_by: platform-team\n"
    )
    root = make_repo(tmp_path, registry_text=registry_text)
    args = make_norm_args(root, registry_text=registry_text)
    payload = {
        "should_propose": True,
        "operation": "sharpen",
        "sharpen_rule_id": "R-RETRO-0001",
        "target_tier": "core",
        "target_file": "ignored-for-sharpen.md",
        "concern_key": "ignored",
        "modality": "MUST",
        "statement": "Sharpened rule anchored on the new confirm_order endpoint",
        "anchor": "new confirm_order endpoint",
        "confidence": 0.9,
        "rationale": "sharpening",
    }
    proposal = normalize.normalize_proposal(payload, make_group(), **args)
    assert proposal is not None
    assert proposal.rule_id == "R-RETRO-0001"
    assert proposal.target_file == "docs/instructions/core.md"  # registry wins
    assert proposal.old_anchor == "old distinctive anchor"
    # No-op sharpen (same anchor) is rejected.
    payload = dict(
        payload,
        anchor="old distinctive anchor",
        statement="Sharpened rule still anchored on the old distinctive anchor",
    )
    with pytest.raises(models.ProposalRejectedError, match="must change the anchor"):
        normalize.normalize_proposal(payload, make_group(), **args)
    # Unknown sharpen target is rejected.
    payload = dict(
        payload,
        sharpen_rule_id="R-RETRO-0099",
        anchor="brand new anchor text",
        statement="Sharpened rule anchored on the brand new anchor text",
    )
    with pytest.raises(models.ProposalRejectedError, match="not in the registry"):
        normalize.normalize_proposal(payload, make_group(), **args)


def test_normalize_add_skill_validation(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    args = make_norm_args(root)
    payload = {
        "should_propose": True,
        "operation": "add_skill",
        "target_tier": "skill",
        "target_file": "skills/order-recovery/SKILL.md",
        "concern_key": "order-recovery",
        "modality": "READ",
        "statement": "",
        "anchor": "run the recovery checklist",
        "skill_slug": "order-recovery",
        "skill_body": "Steps: run the recovery checklist before touching orders.",
        "confidence": 0.9,
        "rationale": "repeated procedure",
    }
    proposal = normalize.normalize_proposal(payload, make_group(), **args)
    assert proposal is not None and proposal.target_file == "skills/order-recovery/SKILL.md"
    with pytest.raises(models.ProposalRejectedError, match="invalid skill slug"):
        normalize.normalize_proposal(dict(payload, skill_slug="Not A Slug"), make_group(), **args)


def test_normalize_enforces_min_incidents(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    args = make_norm_args(root, min_incidents=3)
    with pytest.raises(models.ProposalRejectedError, match="distinct root incident"):
        normalize.normalize_proposal(dict(PROPOSAL_PAYLOAD), make_group(), **args)


def test_normalize_requires_a_reviewer(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    args = make_norm_args(root)
    args["reviewer"] = None
    with pytest.raises(models.ProposalRejectedError, match="no reviewer available"):
        normalize.normalize_proposal(dict(PROPOSAL_PAYLOAD), make_group(), **args)
    # A model-supplied name never substitutes for missing configuration.
    with pytest.raises(models.ProposalRejectedError, match="no reviewer available"):
        normalize.normalize_proposal(
            dict(PROPOSAL_PAYLOAD, review_by="another-reviewer"), make_group(), **args
        )


def test_normalize_sharpen_obeys_target_globs_and_reviewer(tmp_path: Path) -> None:
    registry_text = (
        "schema_version: 1\nrules:\n  R-RETRO-0001:\n"
        "    file: docs/instructions/core.md\n"
        "    anchor: old distinctive anchor\n"
        "    concern_key: order-confirmation-flow\n"
        "    modality: MUST\n    review_by: platform-team\n"
    )
    root = make_repo(tmp_path, registry_text=registry_text)
    args = make_norm_args(root, registry_text=registry_text)
    payload = dict(
        PROPOSAL_PAYLOAD,
        operation="sharpen",
        sharpen_rule_id="R-RETRO-0001",
        statement="Sharpened guidance with a brand new anchor text",
        anchor="brand new anchor text",
    )
    args["target_globs"] = []
    with pytest.raises(models.ProposalRejectedError, match="target_globs is empty"):
        normalize.normalize_proposal(payload, make_group(), **args)

    args["target_globs"] = ["skills/**"]
    with pytest.raises(models.ProposalRejectedError, match="not an editable instruction file"):
        normalize.normalize_proposal(payload, make_group(), **args)

    args["target_globs"] = ["docs/instructions/**"]
    with pytest.raises(models.ProposalRejectedError, match="does not match configured"):
        normalize.normalize_proposal(
            dict(payload, review_by="invented-reviewer"), make_group(), **args
        )


def test_sharpen_first_violation_blocks_overlapping_add(tmp_path: Path) -> None:
    registry_text = (
        "schema_version: 1\nrules:\n  R-RETRO-0001:\n"
        "    file: docs/instructions/core.md\n"
        "    anchor: old distinctive anchor\n"
        "    concern_key: order-confirmation-flow\n"
        "    modality: MUST\n    review_by: platform-team\n"
        "    selectors:\n      paths: [src/orders.py]\n"
    )
    root = make_repo(tmp_path, registry_text=registry_text)
    args = make_norm_args(root, registry_text=registry_text)
    proposal = normalize.normalize_proposal(dict(PROPOSAL_PAYLOAD), make_group(), **args)
    assert proposal is not None
    violation = normalize.sharpen_first_violation(proposal, args["base_registry"])
    assert violation is not None and "sharpen-first" in violation
    supersedes = normalize.normalize_proposal(
        dict(PROPOSAL_PAYLOAD, supersedes=["R-RETRO-0001"]), make_group(), **args
    )
    assert supersedes is not None
    assert normalize.sharpen_first_violation(supersedes, args["base_registry"]) is None


def test_rule_id_allocator_continues_from_existing() -> None:
    allocate = normalize.make_rule_id_allocator({"R-RETRO-0007": {}, "R-OTHER-0001": {}})
    assert allocate() == "R-RETRO-0008"
    assert allocate() == "R-RETRO-0009"


# ---------------------------------------------------------------------------
# edits + registry text round-trips
# ---------------------------------------------------------------------------


def _proposal(**overrides) -> models.DocProposal:
    base = dict(
        operation="add_rule",
        rule_id="R-RETRO-0001",
        concern_key="order-confirmation-flow",
        target_tier="core",
        target_file="docs/instructions/core.md",
        modality="MUST",
        anchor="confirm_order endpoint",
        statement="Always confirm via the confirm_order endpoint",
        selectors={"paths": ["src/orders.py"]},
        review_by="platform-team",
        supersedes=[],
        existing_rules_considered=[],
        why_sharpen_is_insufficient=None,
        control_type="core",
        control_path="docs/instructions/core.md",
        evidence_incidents=["#101", "#102"],
        replay_cases=[],
        rationale="two fix-ups",
        confidence=0.9,
        evidence_key="claude::src",
    )
    base.update(overrides)
    return models.DocProposal(**base)


def test_apply_prose_edit_marker_block() -> None:
    proposal = _proposal()
    once = plan.apply_prose_edit("# Core\n", proposal)
    assert "## Retrospective-proposed rules (pending human review)" in once
    assert proposal.rule_text in once
    # A second edit lands INSIDE the managed block, before the end marker.
    second = _proposal(rule_id="R-RETRO-0002", statement="Also verify via confirm_order endpoint",
                       anchor="verify via confirm_order")
    twice = plan.apply_prose_edit(once, second)
    assert twice.index(second.rule_text) < twice.index("retro-proposed-rules:end")
    assert twice.count("retro-proposed-rules:begin") == 1


def test_apply_prose_edit_sharpen_replaces_and_errors() -> None:
    text = "# Core\n- **MUST** old distinctive anchor here\n"
    proposal = _proposal(
        operation="sharpen", old_anchor="old distinctive anchor",
        anchor="brand new anchor text", statement="Sharpened: brand new anchor text",
    )
    out = plan.apply_prose_edit(text, proposal)
    assert "old distinctive anchor" not in out
    assert proposal.rule_text in out
    missing = _proposal(operation="sharpen", old_anchor="not present anchor")
    with pytest.raises(models.ProposalRejectedError, match="not found"):
        plan.apply_prose_edit(text, missing)


def test_registry_text_round_trip(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    config = load_config(root / "corral.yaml")
    text = plan.ensure_registry_document("")
    assert text.startswith("schema_version: 1")
    proposal = _proposal()
    text = plan.apply_registry_edit(text, proposal)
    registry = parse_registry(text, config.governance)
    assert registry["R-RETRO-0001"]["anchor"] == "confirm_order endpoint"
    # sharpen updates the anchor line in place and stays parseable
    sharpened = _proposal(operation="sharpen", anchor="brand new anchor text")
    text = plan.apply_registry_edit(text, sharpened)
    registry = parse_registry(text, config.governance)
    assert registry["R-RETRO-0001"]["anchor"] == "brand new anchor text"
    with pytest.raises(models.ProposalRejectedError, match="could not locate anchor"):
        plan.update_registry_anchor(text, "R-RETRO-9999", "whatever anchor")


@pytest.mark.parametrize(
    "proposal",
    [
        _proposal(),
        _proposal(
            operation="sharpen",
            rule_id="R-RETRO-0002",
            old_anchor="old distinctive anchor",
        ),
        _proposal(
            operation="add_skill",
            rule_id="R-RETRO-0003",
            target_tier="skill",
            target_file="skills/order-recovery/SKILL.md",
            control_type="skill",
            control_path="skills/order-recovery/SKILL.md",
            skill_slug="order-recovery",
            skill_body="Run the confirm_order endpoint recovery checklist.",
        ),
    ],
    ids=["add-rule", "sharpen", "add-skill"],
)
def test_render_proposal_block_passes_gate_contract(
    tmp_path: Path, proposal: models.DocProposal
) -> None:
    root = make_repo(tmp_path)
    config = load_config(root / "corral.yaml")
    block = plan.render_proposal_block([proposal], config.governance)
    parsed, errors = parse_proposal_block(block)
    assert errors == [] and parsed is not None
    assert validate_proposal_contract(parsed, config.governance.proposals) == []
    assert plan.render_proposal_block([], config.governance) == ""


def test_budget_check_fail_closed_without_manifest(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    check = plan.make_budget_check(
        root=root,
        manifest_path=root / "missing_manifest.yaml",
        schema_path=root / "missing_schema.json",
        registry_path="instruction_rules.yaml",
    )
    ok, msgs = check([models.PlannedEdit(path="docs/instructions/core.md", new_text="x")])
    assert not ok and "could not load manifest" in msgs[0]


def test_budget_check_flags_over_budget_edit(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    from corral.governance.config import DEFAULT_MANIFEST_SCHEMA

    check = plan.make_budget_check(
        root=root,
        manifest_path=root / "instruction_manifest.yaml",
        schema_path=DEFAULT_MANIFEST_SCHEMA,
        registry_path="instruction_rules.yaml",
    )
    ok, _ = check([models.PlannedEdit(path="docs/instructions/core.md", new_text="small text")])
    assert ok
    huge = "x" * (8001 * 4)  # above the 8000-token single-file hard budget
    ok, msgs = check([models.PlannedEdit(path="docs/instructions/core.md", new_text=huge)])
    assert not ok and any("hard budget" in m for m in msgs)


# ---------------------------------------------------------------------------
# end-to-end pass (fake seats) + summary contract
# ---------------------------------------------------------------------------


def _run_pass(tmp_path: Path, *, drafter_outputs: list[str], verifier_output: str):
    root = make_repo(tmp_path)
    config = load_config(root / "corral.yaml")
    from corral.retro.seats import SeatRegistry

    seat_registry = SeatRegistry.from_config(config)
    runners = {
        "draft": FakeSeatRunner(drafter_outputs),
        "verify": FakeSeatRunner([verifier_output]),
    }

    class _GitHub:
        repo = "example/test-repo"

        def merged_prs(self, since, until):
            return []

        def pr_diff_excerpt(self, pr_number, *, max_chars):
            return "diff excerpt"

        def pr_review_excerpt(self, pr_number, *, max_chars):
            return "review excerpt"

        def open_issues(self, label):
            return []

        def create_issue(self, title, body, *, labels=(), assignee=None):
            return "https://example.invalid/issues/1"

    run = plan.draft_and_verify_proposals(
        [make_group()],
        github=_GitHub(),
        seat_registry=seat_registry,
        config=config,
        runner_factory=runners_factory(runners),
    )
    return root, config, run


CONFIRM_JSON = json.dumps({"verdict": "confirm", "reason": "supported by both diffs", "confidence": 0.9})


def test_round_trip_drafted_proposal_to_human_review_section(tmp_path: Path) -> None:
    root, config, run = _run_pass(
        tmp_path, drafter_outputs=[json.dumps(PROPOSAL_PAYLOAD)], verifier_output=CONFIRM_JSON
    )
    assert run.pass_failure is None
    assert len(run.accepted) == 1
    proposal = run.accepted[0]
    assert proposal.rule_id == "R-RETRO-0001"
    paths = {edit.path for edit in run.planned_edits}
    assert paths == {"docs/instructions/core.md", "instruction_rules.yaml"}

    # The rendered block must pass the gate's own parser + contract (C3 reuse).
    parsed, errors = parse_proposal_block(run.proposal_block)
    assert errors == [] and parsed is not None
    assert validate_proposal_contract(parsed, config.governance.proposals) == []

    # The planned edit inserts the managed marker block, never written to disk.
    core_edit = next(e for e in run.planned_edits if e.path == "docs/instructions/core.md")
    assert "retro-proposed-rules:begin" in core_edit.new_text
    assert (root / "docs/instructions/core.md").read_text() == "# Core instructions\n"

    summary_text = render_summary(
        since="2026-08-03", until="2026-08-09", total_groups=1, qualified_groups=1,
        dedup_skipped=0, llm_skipped=[], entries_with_verification=[], refuted=[],
        severity_issues=[], dry_run=False, verification_status="available",
        proposals_enabled=True, proposal_run=run,
    )
    assert PROPOSAL_HUMAN_REVIEW_MARKER in summary_text
    assert "R-RETRO-0001" in summary_text
    assert "`docs/instructions/core.md`" in summary_text
    assert "proposals:" in summary_text  # machine-readable block carried through


def test_round_trip_verifier_refute_records_skip(tmp_path: Path) -> None:
    refute_json = json.dumps({"verdict": "refute", "reason": "coincidental overlap", "confidence": 0.8})
    _, _, run = _run_pass(
        tmp_path, drafter_outputs=[json.dumps(PROPOSAL_PAYLOAD)], verifier_output=refute_json
    )
    assert run.accepted == [] and run.proposal_block == ""
    assert any("verifier refuted" in record["reason"] for record in run.skipped)


def test_round_trip_correction_retry_recovers_invalid_first_response(tmp_path: Path) -> None:
    bad_first = json.dumps(dict(PROPOSAL_PAYLOAD, anchor="short"))
    _, config, run = _run_pass(
        tmp_path,
        drafter_outputs=[bad_first, json.dumps(PROPOSAL_PAYLOAD)],
        verifier_output=CONFIRM_JSON,
    )
    assert len(run.accepted) == 1  # the one correction retry recovered the draft


def test_round_trip_invalid_registry_records_pass_failure(tmp_path: Path) -> None:
    root = make_repo(tmp_path, registry_text="schema_version: 2\nrules: {}\n")
    config = load_config(root / "corral.yaml")
    from corral.retro.seats import SeatRegistry

    run = plan.draft_and_verify_proposals(
        [make_group()],
        github=None,  # registry parse fails before any GitHub call
        seat_registry=SeatRegistry.from_config(config),
        config=config,
        runner_factory=runners_factory({"draft": FakeSeatRunner([]), "verify": FakeSeatRunner([])}),
    )
    assert run.pass_failure is not None and "registry failed to parse" in run.pass_failure


def test_summary_sections_disabled_and_enabled_no_run() -> None:
    disabled = render_summary(
        since="a", until="b", total_groups=0, qualified_groups=0, dedup_skipped=0,
        llm_skipped=[], entries_with_verification=[], refuted=[], severity_issues=[],
        dry_run=False, verification_status="available", proposals_enabled=False,
    )
    assert "retro.proposals.enabled: false" in disabled
    enabled = render_summary(
        since="a", until="b", total_groups=0, qualified_groups=0, dedup_skipped=0,
        llm_skipped=[], entries_with_verification=[], refuted=[], severity_issues=[],
        dry_run=False, verification_status="available", proposals_enabled=True,
        proposal_run=None,
    )
    assert "did not run this week" in enabled


def test_config_proposals_validation() -> None:
    base = CONFIG_YAML.replace("enabled: true", "enabled: false")
    with pytest.raises(ValueError, match="retro.proposals.max"):
        load_config_from_text(base.replace("max: 3", "max: 9"))
    with pytest.raises(ValueError, match="retro.proposals.min_incidents"):
        load_config_from_text(base.replace("min_incidents: 2", "min_incidents: 1"))


@pytest.mark.parametrize(
    ("config_text", "message"),
    [
        (
            CONFIG_YAML.replace(
                '    target_globs: ["docs/instructions/**", "skills/**"]\n', ""
            ),
            "retro.proposals.target_globs is empty",
        ),
        (CONFIG_YAML.replace("  reviewer: platform-team\n", ""), "governance.reviewer"),
        (
            CONFIG_YAML.replace(
                "  reviewer: platform-team\n",
                "  reviewer: platform-team\n  proposals:\n    reviewers: [other-team]\n",
            ),
            "not in governance.proposals.reviewers",
        ),
    ],
)
def test_enabled_pass_withholds_output_on_missing_or_inconsistent_config(
    tmp_path: Path, config_text: str, message: str
) -> None:
    root = make_repo(tmp_path)
    (root / "corral.yaml").write_text(config_text, encoding="utf-8")
    config = load_config(root / "corral.yaml")
    from corral.retro.seats import SeatRegistry

    drafter = FakeSeatRunner([json.dumps(PROPOSAL_PAYLOAD)])
    run = plan.draft_and_verify_proposals(
        [make_group()],
        github=None,
        seat_registry=SeatRegistry.from_config(config),
        config=config,
        runner_factory=runners_factory(
            {"draft": drafter, "verify": FakeSeatRunner([CONFIRM_JSON])}
        ),
    )
    assert run.accepted == [] and run.proposal_block == "" and run.planned_edits == []
    assert run.pass_failure is not None and message in run.pass_failure
    assert drafter.calls == 0


def load_config_from_text(text: str):
    import tempfile

    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        (root / "corral.yaml").write_text(text, encoding="utf-8")
        return load_config(root / "corral.yaml")
