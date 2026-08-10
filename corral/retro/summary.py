"""Pure Markdown rendering for the weekly retrospective.

Verifier provenance comes from the seat results (provider/model of the seat
that actually answered); no vendor names are hardcoded. Labels, titles, and
branch conventions never appear here -- the summary is PR-body content only.
"""

from __future__ import annotations

from typing import Any, Protocol

from corral.retro.mining import GotchaCandidate
from corral.retro.verification import CandidateVerification


class VerifiedCandidateView(Protocol):
    candidate: GotchaCandidate
    original_rule: str
    verification: CandidateVerification


def _verifier_seat_label(outcome: CandidateVerification) -> str:
    if outcome.verifier_provider:
        return f"{outcome.verifier_provider}/{outcome.verifier_model}"
    return "verifier"


def _verification_label(outcome: CandidateVerification) -> str:
    if outcome.verdict == "CONFIRM":
        return f"confirmed by {_verifier_seat_label(outcome)}"
    if outcome.verdict == "UNVERIFIED":
        return f"unverified ({outcome.unverified_reason or 'verifier unavailable'})"
    return f"refuted by {_verifier_seat_label(outcome)}"


def _capacity_skips(skipped: list[dict[str, str]]) -> list[dict[str, str]]:
    return [
        record for record in skipped if record.get("kind") in {"capacity", "capacity_deferred"}
    ]


def _render_capacity_banner(skipped: list[dict[str, str]], *, label: str) -> list[str]:
    """Render a distinct transient-capacity banner with a rerun recommendation."""
    capacity_affected = _capacity_skips(skipped)
    if not capacity_affected:
        return []
    exhausted = sum(record.get("kind") == "capacity" for record in capacity_affected)
    deferred = len(capacity_affected) - exhausted
    if deferred:
        message = (
            f"**{len(capacity_affected)} {label} affected by transient seat errors "
            f"({exhausted} exhausted retries; {deferred} not attempted after the "
            "pass-level capacity circuit opened) — re-run recommended.**"
        )
    else:
        message = (
            f"**{exhausted} {label} failed on transient seat errors after retries — "
            "re-run recommended.**"
        )
    return [
        message,
        "",
    ]


def _public_group_key(key: str) -> str:
    """Drop the internal agent/provider prefix from a mining group key."""
    return key.split("::", 1)[-1]


#: Marker contract for the doc/skill proposal section: verified proposals are
#: NEVER auto-applied; a human reviews and applies them in a follow-up change.
PROPOSAL_HUMAN_REVIEW_MARKER = "HUMAN REVIEW ONLY — never auto-applied"


def render_summary(
    *,
    since: str,
    until: str,
    total_groups: int,
    qualified_groups: int,
    dedup_skipped: int,
    llm_skipped: list[dict[str, str]],
    entries_with_verification: list[tuple[dict[str, Any], VerifiedCandidateView]],
    refuted: list[VerifiedCandidateView],
    severity_issues: list[tuple[GotchaCandidate, str, str | None]],
    dry_run: bool,
    verification_status: str,
    proposals_enabled: bool = False,
    proposal_run: "DocProposalRunView | None" = None,
) -> str:
    lines = [
        f"# Agent Retrospective — {since} to {until}",
        "",
        f"- Evidence groups found: {total_groups}",
        f"- Groups meeting the >=2-distinct-root-incident bar: {qualified_groups}",
        f"- Groups skipped as duplicates of existing gotchas/open issues: {dedup_skipped}",
        f"- Independent verification: {verification_status}",
        f"- Candidates drafted and accepted (confirmed or unverified): {len(entries_with_verification)}",
        f"- Candidates refuted by the verifier (excluded from the gotcha registry): {len(refuted)}",
        f"- Mode: {'DRY RUN (nothing written)' if dry_run else 'live'}",
        "",
    ]
    lines += _render_capacity_banner(llm_skipped, label="gotcha candidate group(s)")
    if entries_with_verification:
        lines += ["## New candidate gotchas", ""]
        for entry, verified in entries_with_verification:
            lines += [
                f"### `{entry['id']}`",
                entry["rule"],
                f"- verification: {_verification_label(verified.verification)}",
            ]
            if verified.candidate.rule != verified.original_rule:
                lines.append(f"- original wording (pre-sharpening): {verified.original_rule}")
            lines += [
                f"- workflow_kinds: {entry['workflow_kinds']}",
                f"- repo_paths: {entry['repo_paths']}",
                f"- surface_ids: {entry['surface_ids']}",
                f"- source_prs: {entry['source_prs']}",
                f"- source_refs: {entry.get('source_refs', [])}",
                f"- control_type: {entry['control_type']}",
                f"- expires: {entry['expires']}",
                "",
            ]
    elif not _capacity_skips(llm_skipped):
        lines += [
            "_Zero proposals this week is a successful outcome — this job is "
            "not scored by how many rules it produces. It means no repeated agent "
            "mistake pattern cleared the >=2-distinct-root-incident evidence bar this "
            "week, not that mining failed._",
            "",
        ]
    if refuted:
        lines += ["## Refuted candidates (excluded from the gotcha registry)", ""]
        for verified in refuted:
            lines += [
                f"- **{verified.candidate.rule}**",
                f"  - {_verifier_seat_label(verified.verification)} reasoning: "
                f"{verified.verification.reasoning or '(none given)'}",
                f"  - source_prs: {verified.candidate.source_prs}",
                "",
            ]
    if severity_issues:
        lines += ["## Severe candidates (immediate review issue)", ""]
        for candidate, gotcha_id, issue_url in severity_issues:
            lines.append(
                f"- `{gotcha_id}` ({candidate.severity}): {issue_url or '(not filed)'}"
            )
        lines.append("")
    if llm_skipped:
        lines += ["## Skipped / dropped candidates", ""]
        for record in llm_skipped:
            lines.append(f"- `{_public_group_key(record['key'])}`: {record['reason']}")
        lines.append("")

    lines += _render_proposal_section(proposals_enabled, proposal_run)
    lines += ["_Generated by `corral retro run`._", ""]
    return "\n".join(lines)


class DocProposalRunView(Protocol):
    accepted: list[Any]
    planned_edits: list[Any]
    skipped: list[dict[str, str]]
    proposal_block: str
    pass_failure: str | None


def _render_proposal_section(
    enabled: bool, run: "DocProposalRunView | None"
) -> list[str]:
    lines = ["## Instruction-file (doc/skill) proposals", ""]
    if not enabled:
        lines += [
            "_Instruction-file proposal pass is disabled "
            "(`retro.proposals.enabled: false`)._",
            "",
        ]
        return lines
    if run is None:
        lines += ["_Doc/skill proposal pass did not run this week._", ""]
        return lines
    lines += [
        f"> **{PROPOSAL_HUMAN_REVIEW_MARKER}.** Nothing in this section is written "
        "by `corral retro run`; every edit below is a proposed commit plan that a "
        "human must review and apply in a follow-up change.",
        "",
    ]
    if run.pass_failure:
        lines += [f"**Proposal pass failure:** {run.pass_failure}", ""]
    if run.accepted:
        lines += [f"### Accepted proposals ({len(run.accepted)})", ""]
        for proposal in run.accepted:
            lines += [
                f"#### `{proposal.rule_id}` ({proposal.operation}) — "
                f"tier `{proposal.target_tier}`, file `{proposal.target_file}`",
            ]
            if proposal.operation == "add_skill":
                lines.append(f"skill slug: `{proposal.skill_slug}`")
            else:
                lines.append(f"rule line: {proposal.rule_text}")
            if proposal.operation == "sharpen":
                lines.append(
                    f"sharpening `{proposal.rule_id}` (old anchor: {proposal.old_anchor!r})"
                )
            lines += [
                f"- concern_key: `{proposal.concern_key}` | modality: {proposal.modality}",
                f"- evidence (distinct root incidents): {proposal.evidence_incidents}",
                f"- rationale: {proposal.rationale}",
                f"- confidence: {proposal.confidence:.2f} | review_by: {proposal.review_by}",
                "",
            ]
        if run.planned_edits:
            lines += ["### Planned commit plan (human-review-only)", ""]
            for edit in run.planned_edits:
                kind = "new file" if edit.is_new_file else "update"
                lines.append(f"- `{edit.path}` — {kind} ({len(edit.new_text)} chars)")
            lines.append("")
        if run.proposal_block:
            lines += [
                "Machine-readable proposal block (carries through the "
                "instruction-governance gate when a human opens the follow-up PR):",
                "",
                run.proposal_block,
                "",
            ]
    else:
        lines += [
            "_Zero doc/skill proposals this week is a successful outcome — no "
            "repeated agent mistake pattern cleared every bar (evidence, "
            "verification, sharpen-first, budget fit)._",
            "",
        ]
    if run.skipped:
        lines += ["### Skipped / dropped proposal groups", ""]
        for record in run.skipped:
            lines.append(f"- `{_public_group_key(record['key'])}`: {record['reason']}")
        lines.append("")
    return lines


__all__ = ["PROPOSAL_HUMAN_REVIEW_MARKER", "render_summary"]
