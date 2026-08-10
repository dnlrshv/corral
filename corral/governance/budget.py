"""Instruction-manifest and configurable per-tier token-budget linter."""

from __future__ import annotations

import math
from datetime import date
from pathlib import Path

from .manifest.evaluator import (
    evaluate_bundle,
    evaluate_debt,
    evaluate_skills,
    evaluate_unit,
    resolve_bundle,
)
from .manifest.model import Finding, Manifest

CHARS_PER_TOKEN = 4


def token_estimate_tokens(text: str) -> int:
    """Pinned deterministic proxy: ``ceil(len(text) / 4)``."""
    return math.ceil(len(text) / CHARS_PER_TOKEN) if text else 0


def read_unit_text(root: Path, relative: str) -> str:
    target = root / relative
    if not target.is_file():
        raise FileNotFoundError(f"instruction unit path not found: {relative}")
    return target.read_text(encoding="utf-8")


def evaluate_token_ceiling(
    tier: str,
    tokens: int,
    token_ceilings: dict[str, int],
    label: str,
) -> list[Finding]:
    """Apply an adopter-defined ceiling for one named tier/kind."""
    ceiling = token_ceilings.get(tier)
    if ceiling is None or tokens <= ceiling:
        return []
    return [
        Finding(
            "FAIL",
            f"{label}: {tokens} tokens > configured {tier!r} tier ceiling {ceiling}.",
        )
    ]


def lint_manifest(
    manifest: Manifest,
    root: Path,
    *,
    as_of: date,
    token_ceilings: dict[str, int] | None = None,
) -> list[Finding]:
    """Evaluate source manifest budgets plus optional config tier ceilings."""
    ceilings = token_ceilings or {}
    tokens_by_unit = {
        uid: token_estimate_tokens(read_unit_text(root, unit.path))
        for uid, unit in manifest.units.items()
    }
    tokens_by_skill = {
        sid: token_estimate_tokens(read_unit_text(root, skill.path))
        for sid, skill in manifest.skills.items()
    }
    debt_findings, active_debt = evaluate_debt(manifest.budget_debt, as_of)
    findings = list(debt_findings)
    for uid, unit in manifest.units.items():
        findings.extend(evaluate_unit(unit, tokens_by_unit[uid], manifest.budgets, active_debt))
        findings.extend(
            evaluate_token_ceiling(unit.kind, tokens_by_unit[uid], ceilings, unit.path)
        )
    for profile in manifest.profiles.values():
        bundle_ids = resolve_bundle(profile, manifest.units)
        findings.extend(
            evaluate_bundle(
                profile,
                bundle_ids,
                tokens_by_unit,
                manifest.budgets,
                manifest.bundle_ratchets,
                active_debt,
            )
        )
        total = sum(tokens_by_unit[uid] for uid in bundle_ids)
        bundle_tier = (
            f"bundle:{profile.id}"
            if f"bundle:{profile.id}" in ceilings
            else "bundle"
        )
        findings.extend(
            evaluate_token_ceiling(
                bundle_tier, total, ceilings, f"profile={profile.id} bundle"
            )
        )
    findings.extend(evaluate_skills(manifest.skills, tokens_by_skill, manifest.budgets))
    for sid, skill in manifest.skills.items():
        findings.extend(
            evaluate_token_ceiling("skill", tokens_by_skill[sid], ceilings, skill.path)
        )
    return findings
