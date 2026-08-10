"""Instruction manifest data model and validation helpers.

The model intentionally preserves the source manifest's fields and stable
token-accounting graph: always-loaded and trigger-loaded units, profiles,
skills, ratchets, and expiring budget debt.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from ..config import DEFAULT_MANIFEST_SCHEMA

ALWAYS_LOADED = "always_loaded"
TRIGGER_LOADED = "trigger_loaded"


@dataclass(frozen=True)
class Budgets:
    always_loaded_bundle_target_tokens: int
    always_loaded_bundle_hard_tokens: int
    always_loaded_single_file_hard_tokens: int
    trigger_loaded_unit_hard_tokens: int
    skill_body_hard_tokens: int
    skill_count_warn: int
    skill_count_hard: int
    budget_debt_max_days: int


@dataclass(frozen=True)
class Unit:
    id: str
    path: str
    kind: str
    always_loads: tuple[str, ...]
    ratchet_ceiling_tokens: int | None
    ratchet_reason: str | None


@dataclass(frozen=True)
class Profile:
    id: str
    entrypoint: str
    description: str


@dataclass(frozen=True)
class BundleRatchet:
    profile_id: str
    ceiling_tokens: int
    reason: str


@dataclass(frozen=True)
class Skill:
    id: str
    path: str


@dataclass(frozen=True)
class BudgetDebt:
    unit: str
    owner: str
    reason: str
    granted_on: date
    expires_on: date


@dataclass(frozen=True)
class Manifest:
    budgets: Budgets
    profiles: dict[str, Profile]
    units: dict[str, Unit]
    bundle_ratchets: dict[str, BundleRatchet]
    skills: dict[str, Skill]
    budget_debt: tuple[BudgetDebt, ...]


@dataclass(frozen=True)
class Finding:
    severity: str
    message: str


def _schema_errors(raw: Any, schema: dict[str, Any]) -> list[str]:
    """Use jsonschema when installed, with a dependency-free basic fallback."""
    try:
        import jsonschema
    except ImportError:
        required = schema.get("required", [])
        if not isinstance(raw, dict):
            return ["$: manifest root must be a mapping"]
        return [f"$: {key!r} is a required property" for key in required if key not in raw]
    validator_cls = jsonschema.validators.validator_for(schema)
    validator_cls.check_schema(schema)
    return [
        f"{error.json_path}: {error.message}"
        for error in sorted(validator_cls(schema).iter_errors(raw), key=lambda item: item.json_path)
    ]


def load_manifest(
    manifest_path: Path, schema_path: Path = DEFAULT_MANIFEST_SCHEMA
) -> Manifest:
    raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    errors = _schema_errors(raw, schema)
    if errors:
        raise ValueError("; ".join(errors))

    budgets = Budgets(**raw["budgets"])
    units = _load_units(raw)
    _validate_always_load_refs(units)
    profiles = _load_profiles(raw, units)
    bundle_ratchets = _load_bundle_ratchets(raw, profiles)
    skills = {sid: Skill(id=sid, path=value["path"]) for sid, value in raw["skills"].items()}
    debts = _load_budget_debt(raw, budgets, units, profiles, skills)
    return Manifest(
        budgets=budgets,
        profiles=profiles,
        units=units,
        bundle_ratchets=bundle_ratchets,
        skills=skills,
        budget_debt=tuple(debts),
    )


def _load_units(raw: dict[str, Any]) -> dict[str, Unit]:
    return {
        uid: Unit(
            id=uid,
            path=value["path"],
            kind=value["kind"],
            always_loads=tuple(value.get("always_loads", [])),
            ratchet_ceiling_tokens=value.get("ratchet_ceiling_tokens"),
            ratchet_reason=value.get("ratchet_reason"),
        )
        for uid, value in raw["units"].items()
    }


def _validate_always_load_refs(units: dict[str, Unit]) -> None:
    for uid, unit in units.items():
        for next_id in unit.always_loads:
            if next_id not in units:
                raise ValueError(
                    f"unit {uid!r}: always_loads references unknown unit {next_id!r}"
                )


def _load_profiles(raw: dict[str, Any], units: dict[str, Unit]) -> dict[str, Profile]:
    profiles: dict[str, Profile] = {}
    for pid, value in raw["profiles"].items():
        entrypoint = value["entrypoint"]
        if entrypoint not in units:
            raise ValueError(
                f"profile {pid!r}: entrypoint {entrypoint!r} is not a declared unit"
            )
        if units[entrypoint].kind != ALWAYS_LOADED:
            raise ValueError(
                f"profile {pid!r}: entrypoint {entrypoint!r} must be "
                f"kind={ALWAYS_LOADED!r}"
            )
        profiles[pid] = Profile(pid, entrypoint, value.get("description", ""))
    return profiles


def _load_bundle_ratchets(
    raw: dict[str, Any], profiles: dict[str, Profile]
) -> dict[str, BundleRatchet]:
    result: dict[str, BundleRatchet] = {}
    for pid, value in (raw.get("bundle_ratchets") or {}).items():
        if pid not in profiles:
            raise ValueError(f"bundle_ratchets: {pid!r} is not a declared profile")
        result[pid] = BundleRatchet(pid, value["ceiling_tokens"], value["reason"])
    return result


def _load_budget_debt(
    raw: dict[str, Any],
    budgets: Budgets,
    units: dict[str, Unit],
    profiles: dict[str, Profile],
    skills: dict[str, Skill],
) -> list[BudgetDebt]:
    known = set(units) | set(skills) | {f"bundle:{pid}" for pid in profiles}
    debts: list[BudgetDebt] = []
    for entry in raw["budget_debt"]:
        unit_ref = entry["unit"]
        if unit_ref not in known:
            raise ValueError(
                f"budget_debt: unit {unit_ref!r} is not a declared unit, skill, "
                "or 'bundle:<profile_id>'"
            )
        granted = date.fromisoformat(entry["granted_on"])
        expires = date.fromisoformat(entry["expires_on"])
        if expires < granted:
            raise ValueError(f"budget_debt for {unit_ref!r}: expires_on before granted_on")
        window = (expires - granted).days
        if window > budgets.budget_debt_max_days:
            raise ValueError(
                f"budget_debt for {unit_ref!r}: expiry window {window}d exceeds max "
                f"{budgets.budget_debt_max_days}d"
            )
        debts.append(
            BudgetDebt(
                unit=unit_ref,
                owner=entry["owner"],
                reason=entry["reason"],
                granted_on=granted,
                expires_on=expires,
            )
        )
    return debts
