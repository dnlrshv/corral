"""Normative registry diffs and the PR-body proposal contract."""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Callable
from typing import Any

import yaml

from .config import GovernanceConfig, ProposalConfig
from .registry import Finding, selectors_overlap

NORMATIVE_FIELDS = ("file", "anchor", "concern_key", "modality")
NORMATIVE_MARKER_RE = re.compile(
    r"(?i)(?:\bmust not\b|\bmust\b|\bnever\b|\balways\b|\bdo not\b|\bdon't\b"
    r"|\bask\b[^.]{0,40}\bbefore\b|\bread\b[^.]{0,60}\bfirst\b)"
)
FENCE_RE = re.compile(r"```[ \t]*ya?ml[ \t]*\n(.*?)```", re.DOTALL | re.IGNORECASE)


def path_matches(path: str, pattern: str) -> bool:
    """Match exact paths and component-aware globs (``**`` is recursive)."""
    if pattern.endswith("/**"):
        prefix = pattern[:-3].rstrip("/")
        return path == prefix or path.startswith(prefix + "/")
    if pattern.endswith("/"):
        return path.startswith(pattern)
    if not any(char in pattern for char in "*?["):
        return path == pattern
    path_parts = path.split("/")
    pattern_parts = pattern.split("/")
    if "**" not in pattern_parts:
        if "/" not in pattern:
            return fnmatch.fnmatchcase(path_parts[-1], pattern)
        return len(path_parts) == len(pattern_parts) and all(
            fnmatch.fnmatchcase(part, glob)
            for part, glob in zip(path_parts, pattern_parts, strict=True)
        )
    # ``fnmatch`` gives ** the desired cross-directory behavior. A leading
    # ``**/`` should also match a root-level path.
    return fnmatch.fnmatchcase(path, pattern) or (
        pattern.startswith("**/") and fnmatch.fnmatchcase(path, pattern[3:])
    )


def is_instruction_file(path: str, globs: list[str]) -> bool:
    return any(path_matches(path, pattern) for pattern in globs)


def parse_proposal_block(pr_body: str) -> tuple[dict[str, Any] | None, list[str]]:
    candidates: list[dict[str, Any]] = []
    errors: list[str] = []
    for match in FENCE_RE.finditer(pr_body or ""):
        try:
            parsed = yaml.safe_load(match.group(1))
        except yaml.YAMLError as exc:
            errors.append(f"a fenced yaml block failed to parse: {exc}")
            continue
        if isinstance(parsed, dict) and "proposals" in parsed:
            candidates.append(parsed)
    if not candidates:
        return None, errors
    if len(candidates) > 1:
        errors.append(
            f"found {len(candidates)} proposal blocks in the PR body; exactly one is allowed"
        )
        return None, errors
    return candidates[0], errors


def validate_proposal_contract(
    block: dict[str, Any], config: ProposalConfig | None = None
) -> list[str]:
    """Validate one parsed proposal block against the proposal contract.

    Verdict-faithful to the source contract (same required fields, max-3 cap,
    DISTINCT root-incident evidence, add_rule/sharpen extras) with strictly
    stricter type checks that never accept input the source fails: rule_ids
    and supersedes must be lists of strings, evidence must be a list, and an
    optional configured reviewer allow-list is enforced (empty = any value).
    """
    cfg = config or ProposalConfig()
    errors: list[str] = []
    proposals = block.get("proposals")
    if not isinstance(proposals, list) or not proposals:
        return ["proposal block: 'proposals' must be a non-empty list"]
    if len(proposals) > cfg.max:
        errors.append(
            f"proposal block: {len(proposals)} proposals exceeds the cap of {cfg.max}"
        )
    operations = set(cfg.operations)
    tiers = set(cfg.tiers)
    reviewers = set(cfg.reviewers)
    seen_rule_ids: set[str] = set()
    for index, proposal in enumerate(proposals):
        tag = f"proposal[{index}]"
        if not isinstance(proposal, dict):
            errors.append(f"{tag}: must be a mapping")
            continue
        operation = proposal.get("operation")
        if operation not in operations:
            errors.append(f"{tag}: operation {operation!r} not in {sorted(operations)}")
        tier = proposal.get("target_tier")
        if tier not in tiers:
            errors.append(f"{tag}: target_tier {tier!r} not in {sorted(tiers)}")
        if not proposal.get("concern_key"):
            errors.append(f"{tag}: missing concern_key")
        review_by = proposal.get("review_by")
        if not review_by:
            errors.append(f"{tag}: missing review_by")
        elif reviewers and review_by not in reviewers:
            errors.append(f"{tag}: review_by {review_by!r} not in configured reviewers")

        rule_ids = proposal.get("rule_ids")
        if not isinstance(rule_ids, list) or not rule_ids or not all(
            isinstance(rule_id, str) for rule_id in rule_ids
        ):
            errors.append(f"{tag}: rule_ids must be a non-empty list of strings")
            rule_ids = []
        for rule_id in rule_ids:
            if rule_id in seen_rule_ids:
                errors.append(
                    f"{tag}: rule_id {rule_id!r} appears in more than one proposal "
                    "(a rule must map to exactly one proposal)"
                )
            seen_rule_ids.add(rule_id)

        evidence = proposal.get("evidence") or []
        if not isinstance(evidence, list):
            errors.append(f"{tag}: evidence must be a list")
            evidence = []
        if operation in ("add_rule", "sharpen") and not evidence:
            errors.append(f"{tag}: operation {operation!r} requires at least one evidence item")
        incidents: list[str] = []
        for item in evidence:
            if not isinstance(item, dict) or not item.get("root_incident"):
                errors.append(f"{tag}: each evidence item needs a 'root_incident'")
            else:
                incidents.append(str(item["root_incident"]))
        duplicates = {value for value in incidents if incidents.count(value) > 1}
        if duplicates:
            errors.append(
                f"{tag}: duplicate root_incident evidence {sorted(duplicates)} -- evidence "
                "must reference DISTINCT root incidents"
            )

        supersedes = proposal.get("supersedes")
        if supersedes is not None and (
            not isinstance(supersedes, list)
            or not all(isinstance(rule_id, str) for rule_id in supersedes)
        ):
            errors.append(f"{tag}: supersedes must be a list of rule IDs")
        if operation == "add_rule":
            if not proposal.get("why_sharpen_is_insufficient"):
                errors.append(
                    f"{tag}: operation 'add_rule' requires 'why_sharpen_is_insufficient'"
                )
            if "existing_rules_considered" not in proposal:
                errors.append(
                    f"{tag}: operation 'add_rule' requires 'existing_rules_considered'"
                )
        if operation in ("add_rule", "sharpen"):
            control = proposal.get("control")
            if not isinstance(control, dict) or not control.get("type"):
                errors.append(
                    f"{tag}: operation {operation!r} requires a control with a 'type'"
                )
            elif control.get("type") not in tiers:
                errors.append(
                    f"{tag}: control.type {control.get('type')!r} not in {sorted(tiers)}"
                )
    return errors


def proposal_supersedes(block: dict[str, Any], rule_id: str) -> set[str]:
    for proposal in block.get("proposals", []):
        if isinstance(proposal, dict) and rule_id in (proposal.get("rule_ids") or []):
            supersedes = proposal.get("supersedes") or []
            return set(supersedes) if isinstance(supersedes, list) else set()
    return set()


def normative_signature(rule: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(rule.get(field) for field in NORMATIVE_FIELDS)


def diff_registries(
    base: dict[str, dict[str, Any]], head: dict[str, dict[str, Any]]
) -> tuple[set[str], set[str], set[str]]:
    added = set(head) - set(base)
    removed = set(base) - set(head)
    changed = {
        rule_id
        for rule_id in set(base) & set(head)
        if normative_signature(base[rule_id]) != normative_signature(head[rule_id])
    }
    return added, changed, removed


def check_path_guard(changed_paths: list[str], protected_paths: list[str]) -> list[Finding]:
    hits = sorted(
        path
        for path in changed_paths
        if any(path_matches(path, pattern) for pattern in protected_paths)
    )
    if not hits:
        return []
    return [
        Finding(
            "FAIL",
            "path-guard",
            "a governance-lane PR (proposing/changing instruction rules) may not modify "
            f"the gate that judges it: {', '.join(hits)}",
        )
    ]


def looks_normative(line: str) -> bool:
    if not line:
        return False
    structured = line.startswith(("-", "*", "#", ">", "**"))
    return bool(structured and NORMATIVE_MARKER_RE.search(line))


def detect_unregistered_normative_additions(
    added_by_file: dict[str, list[str]],
    head_registry: dict[str, dict[str, Any]],
    config: GovernanceConfig,
) -> list[Finding]:
    findings: list[Finding] = []
    anchors_by_file: dict[str, list[str]] = {}
    for rule in head_registry.values():
        anchors_by_file.setdefault(rule["file"], []).append(rule["anchor"])
    for path, lines in added_by_file.items():
        if not is_instruction_file(path, config.instruction_globs):
            continue
        anchors = anchors_by_file.get(path, [])
        for line in lines:
            stripped = line.strip()
            if not looks_normative(stripped) or any(anchor in line for anchor in anchors):
                continue
            findings.append(
                Finding(
                    "FAIL",
                    "registry-entry",
                    f"{path}: new normative directive is not in the rule registry "
                    f"(add an entry to {config.registry} + a proposal block): "
                    f"{stripped[:100]!r}",
                )
            )
    return findings


def check_governance(
    *,
    base_registry: dict[str, dict[str, Any]],
    head_registry: dict[str, dict[str, Any]],
    base_read_file: Callable[[str], str | None],
    changed_paths: list[str],
    added_by_file: dict[str, list[str]],
    proposal_block: dict[str, Any] | None,
    proposal_errors: list[str],
    config: GovernanceConfig | None = None,
) -> list[Finding]:
    """Apply new-rule, near-duplicate, proposal mapping, and path-guard checks."""
    cfg = config or GovernanceConfig()
    findings: list[Finding] = []
    added, changed, removed = diff_registries(base_registry, head_registry)

    genuinely_new: set[str] = set()
    for rule_id in added:
        base_text = base_read_file(head_registry[rule_id]["file"]) or ""
        if head_registry[rule_id]["anchor"] not in base_text:
            genuinely_new.add(rule_id)

    rules_requiring_proposal = genuinely_new | changed | removed
    lane_active = bool(rules_requiring_proposal) or proposal_block is not None
    findings.extend(detect_unregistered_normative_additions(added_by_file, head_registry, cfg))
    if lane_active:
        findings.extend(check_path_guard(changed_paths, cfg.protected_paths))

    if rules_requiring_proposal and proposal_block is None:
        findings.append(
            Finding(
                "FAIL",
                "proposal",
                "this PR changes normative rules "
                f"({', '.join(sorted(rules_requiring_proposal))}) but the PR body has no "
                "machine-readable proposal block (```yaml ... proposals: ...```)",
            )
        )
    findings.extend(Finding("FAIL", "proposal", error) for error in proposal_errors)

    if proposal_block is not None:
        findings.extend(
            Finding("FAIL", "proposal", error)
            for error in validate_proposal_contract(proposal_block, cfg.proposals)
        )
        proposal_ids: set[str] = set()
        for proposal in proposal_block.get("proposals", []):
            if isinstance(proposal, dict) and isinstance(proposal.get("rule_ids"), list):
                proposal_ids.update(proposal["rule_ids"])
        missing = rules_requiring_proposal - proposal_ids
        extra = proposal_ids - rules_requiring_proposal
        if missing:
            findings.append(
                Finding(
                    "FAIL",
                    "proposal",
                    f"changed normative rules not covered by any proposal: {sorted(missing)}",
                )
            )
        if extra:
            findings.append(
                Finding(
                    "FAIL",
                    "proposal",
                    f"proposal references rule_ids that this PR does not change: {sorted(extra)}",
                )
            )

    for rule_id in sorted(genuinely_new):
        new_rule = head_registry[rule_id]
        for other_id, other in base_registry.items():
            if other.get("concern_key") != new_rule.get("concern_key"):
                continue
            if not selectors_overlap(new_rule.get("selectors", {}), other.get("selectors", {})):
                continue
            declared = proposal_supersedes(proposal_block, rule_id) if proposal_block else set()
            if other_id not in declared:
                findings.append(
                    Finding(
                        "FAIL",
                        "supersede",
                        f"{rule_id} shares concern_key {new_rule.get('concern_key')!r} and an "
                        f"overlapping selector with existing rule {other_id}; sharpen it or "
                        f"declare `supersedes: [{other_id}]` in its proposal instead of "
                        "adding a near-duplicate",
                    )
                )
    return findings
