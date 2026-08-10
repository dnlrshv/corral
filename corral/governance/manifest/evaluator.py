"""Faithful evaluation helpers for instruction-manifest budget linting."""

from __future__ import annotations

from datetime import date

from .model import (
    ALWAYS_LOADED,
    BudgetDebt,
    Budgets,
    BundleRatchet,
    Finding,
    Profile,
    Skill,
    Unit,
)

FIX_OPTIONS = (
    "shrink the content, split it into a smaller unit, demote non-normative "
    "material to a reference, or add a budget_debt exception (owner + reason + "
    "expiry <= budgets.budget_debt_max_days days)"
)


def resolve_bundle(profile: Profile, units: dict[str, Unit]) -> list[str]:
    seen: list[str] = []
    seen_set: set[str] = set()
    visiting: set[str] = set()

    def walk(unit_id: str) -> None:
        if unit_id in seen_set:
            return
        if unit_id in visiting:
            raise ValueError(f"cycle detected in always_loads graph at {unit_id!r}")
        unit = units[unit_id]
        if unit.kind != ALWAYS_LOADED:
            raise ValueError(
                f"unit {unit_id!r} is reachable via always_loads but is "
                f"kind={unit.kind!r}, not {ALWAYS_LOADED!r}"
            )
        visiting.add(unit_id)
        for next_id in unit.always_loads:
            walk(next_id)
        visiting.discard(unit_id)
        seen.append(unit_id)
        seen_set.add(unit_id)

    walk(profile.entrypoint)
    return seen


def unit_hard_budget(unit: Unit, budgets: Budgets) -> int:
    return (
        budgets.always_loaded_single_file_hard_tokens
        if unit.kind == ALWAYS_LOADED
        else budgets.trigger_loaded_unit_hard_tokens
    )


def evaluate_unit(
    unit: Unit, tokens: int, budgets: Budgets, active_debt: set[str]
) -> list[Finding]:
    hard = unit_hard_budget(unit, budgets)
    if unit.ratchet_ceiling_tokens is not None:
        if tokens <= unit.ratchet_ceiling_tokens:
            return [
                Finding(
                    "RATCHET",
                    f"{unit.path}: {tokens} tokens (ratcheted; ceiling "
                    f"{unit.ratchet_ceiling_tokens}, normal hard budget {hard}).",
                )
            ]
        if unit.id in active_debt:
            return [
                Finding(
                    "RATCHET",
                    f"{unit.path}: {tokens} tokens > ratchet ceiling "
                    f"{unit.ratchet_ceiling_tokens} but covered by an active "
                    "budget_debt exception.",
                )
            ]
        return [
            Finding(
                "FAIL",
                f"{unit.path}: {tokens} tokens > ratchet ceiling "
                f"{unit.ratchet_ceiling_tokens} (hard budget {hard}). The ratchet may "
                f"only shrink, never grow. Fix: {FIX_OPTIONS}.",
            )
        ]
    if tokens <= hard:
        return []
    if unit.id in active_debt:
        return [
            Finding(
                "RATCHET",
                f"{unit.path}: {tokens} tokens > hard budget {hard} but covered by an "
                "active budget_debt exception.",
            )
        ]
    return [
        Finding(
            "FAIL",
            f"{unit.path}: {tokens} tokens > hard budget {hard} ({unit.kind}). "
            f"Fix: {FIX_OPTIONS}, or -- if this is pre-existing debt being deliberately "
            "grandfathered -- add a ratchet_ceiling_tokens entry recording the current size.",
        )
    ]


def evaluate_bundle(
    profile: Profile,
    unit_ids: list[str],
    tokens_by_unit: dict[str, int],
    budgets: Budgets,
    bundle_ratchets: dict[str, BundleRatchet],
    active_debt: set[str],
) -> list[Finding]:
    total = sum(tokens_by_unit[unit_id] for unit_id in unit_ids)
    label = f"profile={profile.id} bundle=[{'+'.join(unit_ids)}]"
    hard = budgets.always_loaded_bundle_hard_tokens
    target = budgets.always_loaded_bundle_target_tokens
    ratchet = bundle_ratchets.get(profile.id)
    debt_key = f"bundle:{profile.id}"
    if ratchet is not None:
        if total <= ratchet.ceiling_tokens:
            return [
                Finding(
                    "RATCHET",
                    f"{label}: {total} tokens (bundle ratcheted; ceiling "
                    f"{ratchet.ceiling_tokens}, normal hard budget {hard}).",
                )
            ]
        if debt_key in active_debt:
            return [
                Finding(
                    "RATCHET",
                    f"{label}: {total} tokens > bundle ratchet ceiling "
                    f"{ratchet.ceiling_tokens} but covered by an active budget_debt exception.",
                )
            ]
        return [
            Finding(
                "FAIL",
                f"{label}: {total} tokens > bundle ratchet ceiling "
                f"{ratchet.ceiling_tokens} (hard {hard}). The ratchet may only shrink. "
                "Fix: shrink a member unit, split it into a trigger-loaded topic file, "
                "demote to a reference, or add a budget_debt exception.",
            )
        ]
    if total > hard:
        if debt_key in active_debt:
            return [
                Finding(
                    "RATCHET",
                    f"{label}: {total} tokens > hard bundle budget {hard} but covered by "
                    "an active budget_debt exception.",
                )
            ]
        return [
            Finding(
                "FAIL",
                f"{label}: {total} tokens > hard bundle budget {hard}. Fix: shrink a "
                "member unit, split into a trigger-loaded topic file, demote to a reference, "
                "add a budget_debt exception, or record a bundle_ratchets ceiling if this "
                "is deliberate pre-existing debt.",
            )
        ]
    if total > target:
        return [
            Finding(
                "WARN",
                f"{label}: {total} tokens > target {target} (still under hard budget {hard}).",
            )
        ]
    return []


def evaluate_skills(
    skills: dict[str, Skill], tokens_by_skill: dict[str, int], budgets: Budgets
) -> list[Finding]:
    findings: list[Finding] = []
    count = len(skills)
    if count > budgets.skill_count_hard:
        findings.append(
            Finding("FAIL", f"repo-local skill count {count} > hard cap {budgets.skill_count_hard}.")
        )
    elif count > budgets.skill_count_warn:
        findings.append(
            Finding(
                "WARN",
                f"repo-local skill count {count} > warn threshold {budgets.skill_count_warn} "
                "(one-in-one-out policy applies above the warn threshold).",
            )
        )
    for skill in skills.values():
        tokens = tokens_by_skill[skill.id]
        if tokens > budgets.skill_body_hard_tokens:
            findings.append(
                Finding(
                    "FAIL",
                    f"{skill.path}: {tokens} tokens > skill body hard budget "
                    f"{budgets.skill_body_hard_tokens}. Fix: shrink the skill body or move "
                    "detail to a document loaded on demand.",
                )
            )
    return findings


def evaluate_debt(
    debts: tuple[BudgetDebt, ...], as_of: date
) -> tuple[list[Finding], set[str]]:
    findings: list[Finding] = []
    active: set[str] = set()
    for debt in debts:
        if debt.expires_on < as_of:
            findings.append(
                Finding(
                    "FAIL",
                    f"budget_debt for {debt.unit} (owner={debt.owner}) expired on "
                    f"{debt.expires_on.isoformat()}: {debt.reason}. Renew with a new entry "
                    "or resolve the underlying budget violation.",
                )
            )
        else:
            active.add(debt.unit)
    return findings, active
