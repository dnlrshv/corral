"""Markdown report + machine-readable proposal-block rendering.

The proposal blocks rendered here are the SAME machine-readable YAML contract
validated by ``corral.governance.proposals`` -- a demotion proposal carries
``operation: demote`` and an executable-control pointer carries
``operation: sharpen`` / ``target_tier: executable``. Both are run through the
real governance parser (belt-and-suspenders) before anything is filed, so a
proposal this report emits is guaranteed to pass the gate a human's follow-up
PR will face.

This module NEVER edits instruction files and NEVER auto-merges. Demoted
material is proposed for the adopter-configured destination
(``governance.staleness.demote_target_glob``) and must be marked non-normative
by the human who actions the proposal.
"""

from __future__ import annotations

from typing import Any

import yaml

from ..config import GovernanceConfig, ProposalConfig
from ..proposals import parse_proposal_block, validate_proposal_contract
from .model import (
    DEMOTE,
    EXEMPT,
    INSUFFICIENT_DATA,
    MONITOR,
    RETAIN,
    AnalysisResult,
    RuleVerdict,
)

#: Demotion proposals carry the wiki tier label (corral's own tier vocabulary
#: for non-normative demoted material); the concrete destination is the
#: adopter-configured ``demote_target_glob``.
DEMOTION_TARGET_TIER = "wiki"

#: Below this path-coverage fraction the report prints an explicit thin-data
#: note (display-only honesty cue; never a gate).
THIN_COVERAGE_DISPLAY_THRESHOLD = 0.5


def _selectors_to_dict(verdict: RuleVerdict) -> dict[str, Any]:
    sel = verdict.rule.selectors
    out: dict[str, Any] = {}
    if sel.paths:
        out["paths"] = list(sel.paths)
    if sel.workflows:
        out["workflows"] = list(sel.workflows)
    if sel.surfaces:
        out["surfaces"] = list(sel.surfaces)
    return out


def render_demotion_proposal(
    verdict: RuleVerdict, *, cfg: GovernanceConfig, reviewer: str
) -> dict[str, Any]:
    """A single ``operation: demote`` proposal dict.

    ``reviewer`` must be resolved by the caller (``governance.reviewer``); the
    gate contract requires a non-empty ``review_by``.
    """
    s = cfg.staleness
    rule = verdict.rule
    destination = (
        f"Move the prose under `{s.demote_target_glob}` marked NON-NORMATIVE"
        if s.demote_target_glob
        else "Move the prose to the configured demotion target marked NON-NORMATIVE"
    )
    proposal: dict[str, Any] = {
        "operation": "demote",
        "rule_ids": [rule.rule_id],
        "concern_key": rule.concern_key,
        "target_tier": DEMOTION_TARGET_TIER,
        "review_by": reviewer,
        "note": (
            f"Applicability {verdict.demote_window.applicability:.1%} over "
            f"{s.demote_days}d (n={verdict.demote_window.denominator} evaluable "
            f"sessions), below the {s.demote_rate:.0%} demotion floor and not "
            f"retained. {destination} and drop the registry entry. Deterministic "
            f"applicability -- not self-reported."
        ),
    }
    selectors = _selectors_to_dict(verdict)
    if selectors:
        proposal["selectors"] = selectors
    return proposal


def render_executable_pointer_proposal(
    verdict: RuleVerdict, *, reviewer: str
) -> dict[str, Any]:
    """A single ``operation: sharpen`` proposal: replace prose with a pointer
    to the executable control that now owns enforcement."""
    rule = verdict.rule
    control = verdict.executable_control or {}
    root_incident = _root_incident_for(control)
    return {
        "operation": "sharpen",
        "rule_ids": [rule.rule_id],
        "concern_key": rule.concern_key,
        "target_tier": "executable",
        "evidence": [{"root_incident": root_incident}],
        "control": {
            "type": "executable",
            "path": control.get("control_path") or "",
        },
        "review_by": reviewer,
        "note": (
            f"control_type={control.get('control_type')!r} is an executable control "
            f"({control.get('control_path')}); the prose duplicates enforcement. Sharpen "
            f"the entry to a one-line pointer at the control."
        ),
    }


def _root_incident_for(control: dict[str, Any]) -> str:
    cpr = control.get("control_pr")
    if cpr:
        return f"#{cpr}"
    srcs = control.get("source_prs") or []
    if srcs:
        return f"#{srcs[0]}"
    return "#unknown"


def render_proposal_block_yaml(proposals: list[dict[str, Any]]) -> str:
    """Render a fenced ```yaml proposals: ...``` block the governance gate parses."""
    body = yaml.safe_dump({"proposals": proposals}, sort_keys=False, default_flow_style=False)
    return f"```yaml\n{body}```"


def validate_proposal_through_gate(
    proposal: dict[str, Any], proposal_config: ProposalConfig | None = None
) -> list[str]:
    """Render the proposal as a PR-body block and run it through the REAL
    governance parser + contract validator (never a re-implementation)."""
    return validate_proposals_through_gate([proposal], proposal_config)


def validate_proposals_through_gate(
    proposals: list[dict[str, Any]], proposal_config: ProposalConfig | None = None
) -> list[str]:
    """Validate one complete rendered block through the real C3 gate."""
    body = f"Report-generated proposal.\n\n{render_proposal_block_yaml(proposals)}\n"
    block, parse_errors = parse_proposal_block(body)
    if block is None:
        return parse_errors or ["proposal block did not parse"]
    return parse_errors + validate_proposal_contract(block, proposal_config)


def _pct(x: float) -> str:
    return f"{x:.1%}"


def render_report_markdown(
    result: AnalysisResult,
    *,
    cfg: GovernanceConfig,
    repo: str,
    dry_run: bool,
) -> str:
    r = result
    s = cfg.staleness
    cov = r.coverage_fraction_long
    reviewer = (cfg.reviewer or "").strip()

    # The C3 parser accepts exactly ONE proposal block per PR body. Build one
    # consolidated block for the whole report, prefer an executable-pointer
    # sharpen over demotion when the same rule qualifies for both, and enforce
    # the configured gate cap before rendering anything.
    proposal_status: dict[tuple[str, str], str] = {}
    candidates: list[tuple[str, RuleVerdict, dict[str, Any]]] = []
    if reviewer:
        for verdict in sorted(r.executable_pointer_candidates, key=lambda item: item.rule_id):
            proposal = render_executable_pointer_proposal(verdict, reviewer=reviewer)
            errors = validate_proposal_through_gate(proposal, cfg.proposals)
            if errors:
                proposal_status[("executable", verdict.rule_id)] = (
                    f"proposal withheld (governance contract errors: {errors})"
                )
            else:
                candidates.append(("executable", verdict, proposal))

    executable_rule_ids = {
        verdict.rule_id
        for kind, verdict, _proposal in candidates
        if kind == "executable"
    }
    if s.demote_target_glob and reviewer:
        for verdict in sorted(r.demotion_candidates, key=lambda item: item.rule_id):
            if verdict.rule_id in executable_rule_ids:
                proposal_status[("demote", verdict.rule_id)] = (
                    "demotion proposal omitted because the executable-control "
                    "sharpen proposal takes precedence"
                )
                continue
            proposal = render_demotion_proposal(verdict, cfg=cfg, reviewer=reviewer)
            errors = validate_proposal_through_gate(proposal, cfg.proposals)
            if errors:
                proposal_status[("demote", verdict.rule_id)] = (
                    f"proposal withheld (governance contract errors: {errors})"
                )
            else:
                candidates.append(("demote", verdict, proposal))

    selected: list[dict[str, Any]] = []
    selected_keys: list[tuple[str, str]] = []
    for kind, verdict, proposal in candidates:
        key = (kind, verdict.rule_id)
        if len(selected) >= cfg.proposals.max:
            proposal_status[key] = (
                f"proposal withheld by governance.proposals.max={cfg.proposals.max}"
            )
            continue
        selected.append(proposal)
        selected_keys.append(key)
        proposal_status[key] = "included in the consolidated proposal block below"

    consolidated_block = ""
    if selected:
        aggregate_errors = validate_proposals_through_gate(selected, cfg.proposals)
        if aggregate_errors:
            for key in selected_keys:
                proposal_status[key] = (
                    f"proposal withheld (aggregate governance contract errors: {aggregate_errors})"
                )
        else:
            consolidated_block = render_proposal_block_yaml(selected)

    lines: list[str] = [
        f"# Instruction Staleness Report — {r.quarter}",
        "",
        f"- Generated: as-of `{r.as_of.isoformat()}` (repo `{repo}`)",
        f"- Mode: {'DRY RUN (nothing written or filed)' if dry_run else 'live'}",
        f"- Windows: retain `{s.retain_days}d` "
        f"(>= {_pct(s.retain_rate)} across >= {s.retain_workflow_count} workflow kinds), "
        f"demote `{s.demote_days}d` (<{_pct(s.demote_rate)}, "
        f"min {s.min_sessions} evaluable sessions)",
        "",
        "Applicability is **deterministic** — derived from each session's normalized "
        "`workflow_kind` and its touched paths (from merged-PR diffs), intersected with "
        "each rule's registry selectors (surface selectors resolved through the surfaces "
        "registry). Self-reported rule citations are never used.",
        "",
        "## Data coverage",
        "",
        f"- Sessions in {s.demote_days}d window: **{r.total_sessions_long}** "
        f"(in {s.retain_days}d: {r.total_sessions_recent})",
        f"- Sessions with resolved path data: **{r.sessions_with_path_data_long}** "
        f"-> coverage fraction **{_pct(cov)}**",
        f"- Workflow-kind distribution ({s.demote_days}d): "
        + (", ".join(f"`{k}`={v}" for k, v in r.workflow_kind_counts.items()) or "_none_"),
        "",
    ]

    if r.total_sessions_long < s.min_sessions:
        lines += [
            f"> **Sparse telemetry.** Only {r.total_sessions_long} session(s) in the "
            f"{s.demote_days}d window, below the `min_sessions` floor of "
            f"{s.min_sessions}. No demotion verdict can be backed by enough evaluable "
            "data this quarter; rules read `INSUFFICIENT_DATA` rather than being "
            "actioned. Zero demotions is a successful outcome.",
            "",
        ]

    if cov < THIN_COVERAGE_DISPLAY_THRESHOLD:
        lines += [
            "> **Data-honesty note.** Path coverage is thin this window. Path/surface-scoped "
            "rules can only be evaluated against sessions with resolved path data; a session "
            "lacking path data counts in the denominator **only** for workflow-scoped rules. "
            "Most rules will therefore read `INSUFFICIENT_DATA` — a valid outcome. **Zero "
            "demotions is success**; this job is never scored by the number of demotions it "
            "produces.",
            "",
        ]

    # Verdict tally
    tally: dict[str, int] = {}
    for v in r.verdicts:
        tally[v.verdict] = tally.get(v.verdict, 0) + 1
    lines += [
        "## Verdict summary",
        "",
        "| Verdict | Rules |",
        "|---------|-------|",
    ]
    for verdict in (RETAIN, MONITOR, DEMOTE, INSUFFICIENT_DATA, EXEMPT):
        lines.append(f"| {verdict} | {tally.get(verdict, 0)} |")
    lines.append("")

    # Applicability distribution (all rules)
    lines += [
        "## Applicability distribution",
        "",
        f"| Rule | Concern | Modality | {s.retain_days}d appl (n) | wf-kinds | "
        f"{s.demote_days}d appl (n) | Verdict |",
        "|------|---------|----------|-----------------|----------|------------------|---------|",
    ]
    for v in r.verdicts:
        rw, dw = v.retain_window, v.demote_window
        lines.append(
            f"| `{v.rule_id}` | {v.rule.concern_key} | {v.rule.modality} | "
            f"{_pct(rw.applicability)} ({rw.denominator}) | {len(rw.workflow_kinds)} | "
            f"{_pct(dw.applicability)} ({dw.denominator}) | {v.verdict} |"
        )
    lines.append("")

    # Demotion candidates + proposal blocks
    demotions = r.demotion_candidates
    lines += ["## Demotion candidates", ""]
    if not demotions:
        lines += [
            "_None. No non-exempt rule fell below the demotion floor with sufficient "
            "evaluable data this window._",
            "",
        ]
    else:
        lines.append(
            f"{len(demotions)} rule(s) flagged. Eligible entries are carried in the "
            "single machine-readable block below (a human actions it in a follow-up PR the "
            "instruction-governance gate validates). This report itself edits no "
            "instruction files and merges nothing.\n"
        )
        if not s.demote_target_glob:
            lines += [
                "> `governance.staleness.demote_target_glob` is not configured, so no "
                "demotion proposal blocks are emitted. Configure the destination glob "
                "for demoted non-normative prose and re-run.",
                "",
            ]
        if not reviewer:
            lines += [
                "> `governance.reviewer` is not configured, so no demotion proposal "
                "blocks are emitted (the gate contract requires a reviewer).",
                "",
            ]
        for v in demotions:
            lines += [
                f"### `{v.rule_id}` — {v.rule.file}",
                f"> anchor: `{v.rule.anchor}`",
                "",
            ]
            status = proposal_status.get(("demote", v.rule_id))
            if status:
                lines += [f"_{status}._", ""]

    # Executable-control pointer observations
    exec_ptrs = r.executable_pointer_candidates
    lines += ["## Executable-control pointer candidates", ""]
    if not exec_ptrs:
        lines += ["_None among registered rules._", ""]
    else:
        lines.append(
            "These registered gotchas are enforced by an executable control; the prose "
            "can be replaced by a pointer. Proposed via `operation: sharpen` "
            "(observation — action is a human follow-up, not auto-filed):\n"
        )
        if not reviewer:
            lines += [
                "> `governance.reviewer` is not configured, so no executable-pointer "
                "proposal entries are emitted (the gate contract requires a reviewer).",
                "",
            ]
        for v in exec_ptrs:
            control = v.executable_control or {}
            lines += [
                f"### `{v.rule_id}` — control_type `{control.get('control_type')}`",
                f"> control: `{control.get('control_path')}`",
                "",
            ]
            status = proposal_status.get(("executable", v.rule_id))
            if status:
                lines += [f"_{status}._", ""]

    lines += ["## Machine-readable proposal block", ""]
    if consolidated_block:
        lines += [
            "The complete block below was parsed and validated as one PR-body block by the "
            "real instruction-governance gate. A human may carry it into a follow-up PR.",
            "",
            consolidated_block,
            "",
        ]
    else:
        lines += ["_None emitted._", ""]

    # Exempt rules
    lines += ["## Exempt rules — never demoted", ""]
    exempt = r.by_verdict(EXEMPT)
    if not exempt:
        lines += ["_None._", ""]
    else:
        lines += ["| Rule | Concern | Exemption reason |", "|------|---------|------------------|"]
        for v in exempt:
            lines.append(f"| `{v.rule_id}` | {v.rule.concern_key} | {v.exemption_reason} |")
        lines.append("")

    # Skills / data gaps
    lines += [
        "## Skill archival",
        "",
        "_Not computable. Per-skill invocation is not captured in the current agent-telemetry "
        "rollups (no skill-usage column), so a zero-invocation archival signal cannot be "
        "derived deterministically. Reported as a data gap rather than emitting false archival "
        "proposals for every skill — instrumenting skill invocation is a prerequisite follow-up._",
        "",
        "---",
        "",
        "_Generated by `corral governance staleness`. Zero demotions is a successful "
        "outcome; this job is scored on precision, never on volume of proposals produced._",
        "",
    ]
    return "\n".join(lines)


__all__ = [
    "DEMOTION_TARGET_TIER",
    "THIN_COVERAGE_DISPLAY_THRESHOLD",
    "render_demotion_proposal",
    "render_executable_pointer_proposal",
    "render_proposal_block_yaml",
    "render_report_markdown",
    "validate_proposal_through_gate",
    "validate_proposals_through_gate",
]
