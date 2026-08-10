"""Trial-application, budget fit, rendering, and orchestration for doc/skill
proposals.

C3 reuse points (no duplicated governance logic):

* ``render_proposal_block`` self-validates through the gate's OWN
  ``corral.governance.proposals.parse_proposal_block`` +
  ``validate_proposal_contract``.
* ``sharpen_first_violation`` (normalize module) uses the gate's
  ``selectors_overlap``.
* ``make_budget_check`` runs the real ``corral.governance.manifest`` evaluator
  (+ ``corral.governance.budget.token_estimate_tokens``) over PROSPECTIVE text.
* Registry parsing uses ``corral.governance.registry.parse_registry``.

``corral retro run`` renders accepted proposals human-review-only; NOTHING in
this module writes instruction files to disk.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

from corral.governance.budget import token_estimate_tokens
from corral.governance.config import DEFAULT_MANIFEST_SCHEMA, GovernanceConfig
from corral.governance.manifest.evaluator import (
    evaluate_bundle,
    evaluate_skills,
    evaluate_unit,
    resolve_bundle,
)
from corral.governance.manifest.model import Finding, load_manifest
from corral.governance.proposals import parse_proposal_block, validate_proposal_contract
from corral.governance.registry import parse_registry
from corral.retro import doc_verification, evidence, retry
from corral.retro.bridge.readers import group_repo_paths, render_group_evidence
from corral.retro.github import GitHubClient
from corral.retro.providers import runner_for_seat
from corral.retro.seats import SeatRegistry
from corral.retro.types import EvidenceGroup

from . import draft
from .models import (
    DEFAULT_MAX_DOC_PROPOSALS,
    DEFAULT_MIN_ROOT_INCIDENTS,
    DocProposal,
    DocProposalRun,
    PlannedEdit,
    ProposalRejectedError,
    check_skill_eligibility,
)
from .normalize import make_rule_id_allocator, normalize_proposal, sharpen_first_violation

BudgetCheck = Callable[[Sequence[PlannedEdit]], "tuple[bool, list[str]]"]

#: Managed insertion markers. New prose rules land inside this block so the
#: diff is contained, deterministic, and trivially revertible by the human
#: reviewer.
_MARKER_BEGIN = (
    "<!-- retro-proposed-rules:begin (weekly retrospective — human review before merge) -->"
)
_MARKER_END = "<!-- retro-proposed-rules:end -->"
_MARKER_HEADING = "## Retrospective-proposed rules (pending human review)"

#: Independent-verifier contract (source semantics, no private references).
_VERIFIER_CONTRACT = (
    "- Evidence must show a REPEATED agent mistake (>=2 distinct root incidents), "
    "not a one-off.\n"
    "- Sharpen an existing rule rather than add a near-duplicate; adding is only "
    "valid if no existing rule covers the concern.\n"
    "- Placement must be as high on the configured tier ladder as applicable.\n"
    "- The rule must be concrete and actionable, not narrative or aspirational."
)

_REGISTRY_HEADER = "schema_version: 1\nrules:\n"


# ---------------------------------------------------------------------------
# prose + skill + registry editing (deterministic, text-based)
# ---------------------------------------------------------------------------


def _ensure_trailing_newline(text: str) -> str:
    return text if text.endswith("\n") else text + "\n"


def ensure_registry_document(registry_text: str) -> str:
    """Seed the registry document header when the repo has no registry yet.

    corral registries carry ``schema_version: 1`` + a ``rules:`` mapping (the
    gate's ``parse_registry`` requires both), so an accepted first proposal
    must render into a valid document.
    """
    return registry_text if registry_text.strip() else _REGISTRY_HEADER


def apply_prose_edit(current_text: str | None, proposal: DocProposal) -> str:
    """Apply a proposal's prose edit to ``current_text`` and return the new text.

    ``add_rule`` inserts the rule line inside the managed marker block (created
    at EOF if absent). ``sharpen`` replaces the line carrying the rule's old
    anchor.
    """
    text = current_text or ""
    if proposal.operation == "sharpen":
        return _apply_sharpen(text, proposal)
    return _insert_into_marker_block(text, proposal.rule_text)


def _apply_sharpen(text: str, proposal: DocProposal) -> str:
    assert proposal.old_anchor is not None
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if proposal.old_anchor in line:
            newline = "\n" if line.endswith("\n") else ""
            lines[i] = proposal.rule_text + newline
            return "".join(lines)
    raise ProposalRejectedError(
        f"sharpen anchor {proposal.old_anchor!r} not found in {proposal.target_file!r}"
    )


def _insert_into_marker_block(text: str, rule_line: str) -> str:
    if _MARKER_END in text:
        return text.replace(_MARKER_END, f"{rule_line}\n{_MARKER_END}", 1)
    base = _ensure_trailing_newline(text) if text else ""
    block = f"\n{_MARKER_HEADING}\n\n{_MARKER_BEGIN}\n{rule_line}\n{_MARKER_END}\n"
    return base + block


def build_skill_file(proposal: DocProposal) -> str:
    """Minimal procedural SKILL.md body for an ``add_skill`` proposal."""
    return (
        f"# {proposal.skill_slug}\n\n"
        "> Proposed by the weekly retrospective — pending human review.\n\n"
        f"**Concern:** {proposal.concern_key}\n\n"
        f"## Procedure\n\n{proposal.skill_body}\n"
    )


def registry_entry(proposal: DocProposal) -> dict[str, Any]:
    """The registry mapping value for this proposal's rule."""
    entry: dict[str, Any] = {
        "file": proposal.target_file,
        "anchor": proposal.anchor,
        "concern_key": proposal.concern_key,
        "modality": proposal.modality,
    }
    entry["selectors"] = proposal.selectors or {}
    entry["review_by"] = proposal.review_by
    entry["note"] = "proposed by weekly retrospective; pending human review"
    return entry


def _render_registry_entry_yaml(rule_id: str, entry: Mapping[str, Any]) -> str:
    dumped = yaml.safe_dump({rule_id: dict(entry)}, sort_keys=False, default_flow_style=False)
    return "".join(
        "  " + line if line.strip() else line for line in dumped.splitlines(keepends=True)
    )


def append_registry_entry(registry_text: str, rule_id: str, entry: Mapping[str, Any]) -> str:
    block = _render_registry_entry_yaml(rule_id, entry)
    return _ensure_trailing_newline(registry_text) + block


def update_registry_anchor(registry_text: str, rule_id: str, new_anchor: str) -> str:
    """Replace the ``anchor:`` line inside ``rule_id``'s block (for sharpen)."""
    lines = registry_text.splitlines(keepends=True)
    key_re = re.compile(rf"^  {re.escape(rule_id)}:\s*$")
    next_key_re = re.compile(r"^  [A-Za-z]")
    anchor_re = re.compile(r"^    anchor:\s")
    in_block = False
    quoted = yaml.safe_dump({"anchor": new_anchor}, default_flow_style=False).strip()
    for i, line in enumerate(lines):
        if key_re.match(line):
            in_block = True
            continue
        if in_block and next_key_re.match(line):
            break
        if in_block and anchor_re.match(line):
            newline = "\n" if line.endswith("\n") else ""
            lines[i] = "    " + quoted + newline
            return "".join(lines)
    raise ProposalRejectedError(f"could not locate anchor line for {rule_id!r} in the registry")


def apply_registry_edit(registry_text: str, proposal: DocProposal) -> str:
    if proposal.operation == "sharpen":
        return update_registry_anchor(registry_text, proposal.rule_id, proposal.anchor)
    return append_registry_entry(registry_text, proposal.rule_id, registry_entry(proposal))


# ---------------------------------------------------------------------------
# proposal-block rendering (self-validated against the real gate parser)
# ---------------------------------------------------------------------------


def _proposal_to_block_entry(p: DocProposal) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "operation": p.operation,
        "rule_ids": [p.rule_id],
        "concern_key": p.concern_key,
        "target_tier": p.target_tier,
        "selectors": p.selectors or {},
        "evidence": [{"root_incident": inc} for inc in p.evidence_incidents],
        "control": {"type": p.control_type, "path": p.control_path},
        "review_by": p.review_by,
        "supersedes": p.supersedes,
        "replay_cases": p.replay_cases,
    }
    if p.operation == "add_rule":
        entry["existing_rules_considered"] = p.existing_rules_considered
        entry["why_sharpen_is_insufficient"] = (
            p.why_sharpen_is_insufficient or "no existing rule covered this concern"
        )
    return entry


def render_proposal_block(proposals: Sequence[DocProposal], governance: GovernanceConfig) -> str:
    """Render the fenced ``yaml`` proposal block for the weekly PR body.

    Self-validates the rendered block through the gate's OWN
    ``parse_proposal_block`` + ``validate_proposal_contract`` (C3 reuse) before
    returning; a validation failure is a rendering bug and raises.
    """
    if not proposals:
        return ""
    block = {"proposals": [_proposal_to_block_entry(p) for p in proposals]}
    body = yaml.safe_dump(block, sort_keys=False, default_flow_style=False, allow_unicode=True)
    fenced = f"```yaml\n{body}```"
    parsed, parse_errors = parse_proposal_block(fenced)
    if parse_errors or parsed is None:
        raise ValueError(f"rendered proposal block did not parse: {parse_errors}")
    contract_errors = validate_proposal_contract(parsed, governance.proposals)
    if contract_errors:
        raise ValueError(f"rendered proposal block violates the contract: {contract_errors}")
    return fenced


# ---------------------------------------------------------------------------
# budget check (reuse the real manifest evaluator over prospective text)
# ---------------------------------------------------------------------------


def make_budget_check(
    *, root: Path, manifest_path: Path, schema_path: Path, registry_path: str
) -> BudgetCheck:
    """Build a budget check that reuses the real governance manifest evaluator
    over the PROSPECTIVE (edited) file text, without touching disk.

    Any manifest/loader problem degrades to "cannot verify" == FAIL-CLOSED
    (over-budget), so a proposal is dropped rather than merged unchecked. The
    retro NEVER self-grants budget debt. A brand-new file that is neither a
    manifest unit nor the registry is treated as a new skill file and checked
    against the skill body/count hard caps directly.
    """
    root_p = Path(root)

    def _read(rel: str) -> str:
        f = root_p / rel
        return f.read_text(encoding="utf-8") if f.is_file() else ""

    def budget_ok(edits: Sequence[PlannedEdit]) -> tuple[bool, list[str]]:
        try:
            manifest = load_manifest(manifest_path, schema_path)
        except Exception as exc:  # cannot verify -> fail closed
            return False, [f"budget check could not load manifest: {exc}"]
        edited = {e.path: e.new_text for e in edits}
        tokens_by_unit = {
            uid: token_estimate_tokens(edited.get(u.path, _read(u.path)))
            for uid, u in manifest.units.items()
        }
        tokens_by_skill = {
            sid: token_estimate_tokens(edited.get(s.path, _read(s.path)))
            for sid, s in manifest.skills.items()
        }
        no_debt: set[str] = set()  # retro never self-grants budget_debt
        findings: list[Finding] = []
        for uid, unit in manifest.units.items():
            findings.extend(evaluate_unit(unit, tokens_by_unit[uid], manifest.budgets, no_debt))
        try:
            for profile in manifest.profiles.values():
                bundle_ids = resolve_bundle(profile, manifest.units)
                findings.extend(
                    evaluate_bundle(
                        profile,
                        bundle_ids,
                        tokens_by_unit,
                        manifest.budgets,
                        manifest.bundle_ratchets,
                        no_debt,
                    )
                )
        except ValueError as exc:
            return False, [f"budget check graph error: {exc}"]
        findings.extend(evaluate_skills(manifest.skills, tokens_by_skill, manifest.budgets))

        # A brand-new skill file is not yet a manifest unit; check its body cap
        # and the skill-count hard cap directly.
        known_paths = {u.path for u in manifest.units.values()} | {
            s.path for s in manifest.skills.values()
        }
        new_skill_edits = [
            e
            for e in edits
            if e.is_new_file and e.path != registry_path and e.path not in known_paths
        ]
        for e in new_skill_edits:
            body_tokens = token_estimate_tokens(e.new_text)
            if body_tokens > manifest.budgets.skill_body_hard_tokens:
                findings.append(
                    Finding(
                        "FAIL",
                        f"new skill {e.path} body {body_tokens} tokens exceeds hard "
                        f"budget {manifest.budgets.skill_body_hard_tokens}",
                    )
                )
        projected_skill_count = len(manifest.skills) + len(new_skill_edits)
        if projected_skill_count > manifest.budgets.skill_count_hard:
            findings.append(
                Finding(
                    "FAIL",
                    f"skill count {projected_skill_count} exceeds hard cap "
                    f"{manifest.budgets.skill_count_hard}",
                )
            )
        fails = [f for f in findings if f.severity == "FAIL"]
        return (not fails), [f.message for f in fails]

    return budget_ok


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------


def _distinct_incident_weeks(group: EvidenceGroup) -> int:
    """Distinct calendar weeks this group's evidence spans.

    The fix-up contexts carry ``days_between`` but no absolute dates, so a
    single weekly window cannot prove multi-week persistence. We therefore
    report 1 (single window) until multi-week telemetry supplies real week
    spans -- this intentionally keeps skill proposals gated off (skills need a
    demonstrated >=2-week track record, not a single week's coincidence).
    """
    return 1


def _drafter_complete(
    seat_registry: SeatRegistry,
    config: object,
    *,
    runner_factory: Callable[..., Any] | None = None,
) -> Callable[[str], str]:
    """Drafter callable: the configured seat's complete wrapped in bounded retry."""
    retro = getattr(config, "retro")
    seat = seat_registry.require(retro.drafter_seat)
    runner = (runner_factory or runner_for_seat)(seat)

    def complete(prompt: str) -> str:
        return retry.call_with_retry(
            lambda p: runner.complete(
                seat, p, timeout=retro.drafting_timeout_s, max_tokens=retro.max_tokens
            ),
            prompt,
            context="doc-proposal drafting",
        )

    return complete


def _overlaps_area(rule: Mapping[str, Any], group: EvidenceGroup) -> bool:
    """Heuristic: surface existing rules whose selector paths touch the group's
    shared files, so the drafter is nudged to sharpen rather than add."""
    files = group_repo_paths(group)
    paths = (rule.get("selectors") or {}).get("paths") or []
    return any(f.startswith(p) or p.startswith(f) for f in files for p in paths)


def _render_subject(p: DocProposal) -> str:
    lines = [
        f"operation: {p.operation}",
        f"target_tier: {p.target_tier} | target_file: {p.target_file}",
        f"concern_key: {p.concern_key} | modality: {p.modality}",
    ]
    if p.operation == "add_skill":
        lines.append(f"skill body:\n{p.skill_body}")
    else:
        lines.append(f"rule line: {p.rule_text}")
    if p.operation == "sharpen":
        lines.append(f"replacing rule {p.rule_id} (old anchor: {p.old_anchor!r})")
    lines.append(f"rationale: {p.rationale}")
    return "\n".join(lines)


def _planned_edits(
    working_files: Mapping[str, str],
    new_file_paths: set[str],
    proposal: DocProposal,
    registry_path: str,
    registry_text: str,
    registry_is_new: bool,
) -> list[PlannedEdit]:
    is_new = proposal.operation == "add_skill" or proposal.target_file in new_file_paths
    edits = [
        PlannedEdit(
            path=path,
            new_text=text,
            is_new_file=(path in new_file_paths or (path == proposal.target_file and is_new)),
        )
        for path, text in working_files.items()
    ]
    edits.append(
        PlannedEdit(path=registry_path, new_text=registry_text, is_new_file=registry_is_new)
    )
    return edits


def _final_planned_edits(
    working_files: Mapping[str, str],
    registry_path: str,
    registry_text: str,
    registry_is_new: bool,
    accepted: Sequence[DocProposal],
) -> list[PlannedEdit]:
    if not accepted:
        return []
    new_paths = {p.target_file for p in accepted if p.operation == "add_skill"}
    edits = [
        PlannedEdit(path=path, new_text=text, is_new_file=path in new_paths)
        for path, text in sorted(working_files.items())
    ]
    edits.append(
        PlannedEdit(path=registry_path, new_text=registry_text, is_new_file=registry_is_new)
    )
    return edits


def draft_and_verify_proposals(
    groups: Sequence[EvidenceGroup],
    *,
    github: GitHubClient,
    seat_registry: SeatRegistry,
    config: object,
    runner_factory: Callable[..., Any] | None = None,
) -> DocProposalRun:
    """Draft, independently verify, gate, and render doc/skill proposals.

    Consumes the SAME survivor evidence groups the gotcha pass used (most-
    evidenced first). Returns accepted proposals, the cumulative planned edits
    (prose/skill files + registry, human-review-only), the rendered proposal
    block, and a skip log. NEVER writes any instruction file.
    """
    retro = getattr(config, "retro")
    governance = getattr(config, "governance")
    root = getattr(config, "root")
    props = retro.proposals
    target_globs = [pattern.strip() for pattern in props.target_globs if pattern.strip()]
    max_proposals = min(
        props.max or DEFAULT_MAX_DOC_PROPOSALS,
        governance.proposals.max,
        DEFAULT_MAX_DOC_PROPOSALS,
    )
    min_incidents = props.min_incidents or DEFAULT_MIN_ROOT_INCIDENTS

    run = DocProposalRun()
    reviewer = (governance.reviewer or "").strip()
    if not target_globs:
        run.pass_failure = (
            "retro.proposals.target_globs is empty; no instruction-file proposals "
            "were drafted or emitted"
        )
        return run
    if not reviewer:
        run.pass_failure = (
            "governance.reviewer is not configured; no instruction-file proposals "
            "were drafted or emitted"
        )
        return run
    allowed_reviewers = set(governance.proposals.reviewers)
    if allowed_reviewers and reviewer not in allowed_reviewers:
        run.pass_failure = (
            f"governance.reviewer {reviewer!r} is not in "
            "governance.proposals.reviewers; no instruction-file proposals were "
            "drafted or emitted"
        )
        return run
    registry_path = governance.registry
    registry_absent = not (root / registry_path).is_file()
    registry_text = (
        "" if registry_absent else (root / registry_path).read_text(encoding="utf-8")
    )
    try:
        base_registry = parse_registry(registry_text, governance)
    except ValueError as exc:
        run.pass_failure = f"instruction registry failed to parse: {exc}"
        return run
    run.new_registry_text = ensure_registry_document(registry_text)

    allocate = make_rule_id_allocator(base_registry)
    complete = _drafter_complete(seat_registry, config, runner_factory=runner_factory)
    budget_ok = make_budget_check(
        root=root,
        manifest_path=root / governance.replay.manifest,
        schema_path=DEFAULT_MANIFEST_SCHEMA,
        registry_path=registry_path,
    )

    def read_instruction_file(path: str) -> str | None:
        target = root / path
        return target.read_text(encoding="utf-8") if target.is_file() else None

    working_files: dict[str, str] = {}
    file_exists: dict[str, bool] = {}
    seen_paths: set[str] = set()
    working_registry = run.new_registry_text
    working_registry_is_new = registry_absent

    for group_index, group in enumerate(groups):
        if len(run.accepted) >= max_proposals:
            run.skipped.append(
                {
                    "key": group.key,
                    "reason": f"dropped: doc-proposal cap ({max_proposals}) reached",
                }
            )
            continue

        excerpts = evidence.fetch_pr_excerpts(github, group.pr_numbers)
        candidate_rules = [
            {"id": rid, **r} for rid, r in base_registry.items() if _overlaps_area(r, group)
        ]
        prompt = draft.build_doc_proposal_prompt(
            group, excerpts, candidate_rules=candidate_rules, tiers=governance.proposals.tiers
        )
        try:
            proposal = draft.draft_and_normalize_with_retry(
                prompt,
                group_key=group.key,
                drafter_complete=complete,
                normalize_payload=lambda payload: normalize_proposal(
                    payload,
                    group,
                    base_registry=base_registry,
                    allocate_rule_id=allocate,
                    governance=governance,
                    target_globs=target_globs,
                    reviewer=reviewer,
                    min_incidents=min_incidents,
                ),
                validation_errors=(ValueError, ProposalRejectedError),
                final_error_type=ProposalRejectedError,
            )
        except retry.RetriableLLMError as exc:
            run.skipped.append(
                {
                    "key": group.key,
                    "reason": f"transient seat errors after retries: {exc}",
                    "kind": "capacity",
                }
            )
            run.skipped.extend(
                {
                    "key": deferred.key,
                    "reason": (
                        "not attempted after an earlier group exhausted retries "
                        "(pass-level capacity circuit open)"
                    ),
                    "kind": "capacity_deferred",
                }
                for deferred in groups[group_index + 1 :]
            )
            break
        except retry.NonRetriableLLMError as exc:
            run.pass_failure = str(exc)
            break
        except ProposalRejectedError as exc:
            run.skipped.append({"key": group.key, "reason": str(exc)})
            continue
        if proposal is None:
            run.skipped.append({"key": group.key, "reason": "drafter declined (no gap found)"})
            continue

        if proposal.confidence < retro.confidence_threshold:
            run.skipped.append(
                {
                    "key": group.key,
                    "reason": (
                        f"confidence {proposal.confidence:.2f} below threshold "
                        f"{retro.confidence_threshold:.2f}"
                    ),
                }
            )
            continue

        # sharpen-first (cheap, deterministic) before spending a verify call.
        violation = sharpen_first_violation(proposal, base_registry)
        if violation:
            run.skipped.append({"key": group.key, "reason": violation})
            continue

        if proposal.operation == "add_skill":
            ok, why = check_skill_eligibility(
                completed_tasks=group.evidence_count,
                distinct_weeks=_distinct_incident_weeks(group),
                stable_trigger=proposal.concern_key,
            )
            if not ok:
                run.skipped.append({"key": group.key, "reason": why})
                continue

        # independent verification (fail-closed for instruction edits):
        # refute => drop.
        result = doc_verification.verify(
            subject=_render_subject(proposal),
            evidence=render_group_evidence(group, excerpts),
            contract=_VERIFIER_CONTRACT,
            registry=seat_registry,
            config=config,
            runner_factory=runner_factory,
        )
        if not result.confirmed:
            run.skipped.append(
                {
                    "key": group.key,
                    "reason": f"verifier refuted: {result.reason or '(no reason)'}",
                }
            )
            continue

        # trial-apply against the cumulative working state, then budget-check.
        trial_files = dict(working_files)
        path = proposal.target_file
        if path not in trial_files:
            existing_text = read_instruction_file(path)
            if proposal.operation == "add_skill" and existing_text is not None:
                run.skipped.append(
                    {
                        "key": group.key,
                        "reason": f"add_skill target already exists; refusing to overwrite {path}",
                    }
                )
                continue
            trial_files[path] = existing_text or ""
            file_exists[path] = existing_text is not None
        elif proposal.operation == "add_skill":
            run.skipped.append(
                {
                    "key": group.key,
                    "reason": f"add_skill target already selected this run; refusing duplicate {path}",
                }
            )
            continue
        try:
            trial_files[path] = (
                build_skill_file(proposal)
                if proposal.operation == "add_skill"
                else apply_prose_edit(trial_files[path], proposal)
            )
            trial_registry = apply_registry_edit(working_registry, proposal)
        except ProposalRejectedError as exc:
            run.skipped.append({"key": group.key, "reason": str(exc)})
            continue

        edits = _planned_edits(
            trial_files,
            seen_paths,
            proposal,
            registry_path,
            trial_registry,
            working_registry_is_new,
        )
        ok, msgs = budget_ok(edits)
        if not ok:
            run.skipped.append(
                {
                    "key": group.key,
                    "reason": f"dropped: over token budget, no compensating trim ({'; '.join(msgs)})",
                }
            )
            continue

        # accept: commit the trial into the working state (in-memory only).
        working_files = trial_files
        working_registry = trial_registry
        working_registry_is_new = False
        if proposal.operation == "add_skill":
            seen_paths.add(path)  # new file
        run.accepted.append(proposal)

    run.new_registry_text = working_registry
    run.planned_edits = _final_planned_edits(
        working_files,
        registry_path,
        working_registry,
        working_registry_is_new,
        run.accepted,
    )
    try:
        run.proposal_block = render_proposal_block(run.accepted, governance)
    except ValueError as exc:
        # A rendering bug must not silently drop accepted proposals from the
        # summary; record it and keep the accepted list visible.
        run.pass_failure = f"proposal-block rendering failed: {exc}"
    return run


__all__ = [
    "BudgetCheck",
    "apply_prose_edit",
    "apply_registry_edit",
    "append_registry_entry",
    "build_skill_file",
    "draft_and_verify_proposals",
    "ensure_registry_document",
    "make_budget_check",
    "registry_entry",
    "render_proposal_block",
    "update_registry_anchor",
]
