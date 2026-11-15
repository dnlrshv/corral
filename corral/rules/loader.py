"""Side-effect-free rule layers shared by execution and instruction sync."""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, field
from importlib.resources import files
from pathlib import Path
from typing import Mapping

import yaml

from corral.redaction import check_outbound_safe

_ID = r"^[a-z0-9][a-z0-9._-]*$"
RULES_FILE_SCHEMA_V1 = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "corral-rules-v1", "type": "object",
    "required": ["schema_version", "rules"],
    "properties": {
        "schema_version": {"const": 1},
        "rules": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["id", "text", "reason"],
            "properties": {
                "id": {"type": "string", "pattern": _ID},
                "text": {"type": "string", "minLength": 1},
                "reason": {"type": "string", "minLength": 1},
                "enabled": {"type": "boolean"},
                "supersedes": {"type": "array", "items": {"type": "string", "pattern": _ID}},
                "applies_to": {"type": "object", "additionalProperties": False,
                    "properties": {key: {"type": "array", "items": {"type": "string"}}
                                   for key in ("repos", "tiers", "phases", "harnesses")}},
            },
        }},
    },
}


@dataclass(frozen=True)
class Applicability:
    repos: tuple[str, ...] = ()
    tiers: tuple[str, ...] = ()
    phases: tuple[str, ...] = ()
    harnesses: tuple[str, ...] = ()


@dataclass(frozen=True)
class RuleRecord:
    id: str
    text: str
    reason: str
    applies_to: Applicability
    source: str
    enabled: bool = True
    supersedes: tuple[str, ...] = ()


@dataclass(frozen=True)
class LoadedLayer:
    source: str
    path: Path | None
    sha256: str | None
    rules: tuple[RuleRecord, ...]
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Layers:
    packaged: LoadedLayer
    repo: LoadedLayer | None
    overlay: LoadedLayer | None

    def merged(self) -> tuple[RuleRecord, ...]:
        merged = {}
        for layer in (self.packaged, self.repo, self.overlay):
            for rule in layer.rules if layer else ():
                if not rule.enabled:
                    merged.pop(rule.id, None)
                    continue
                for replaced in rule.supersedes:
                    if replaced != rule.id:
                        merged.pop(replaced, None)
                merged[rule.id] = rule
        return tuple(merged.values())

    def applicable(self, *, repo: str | None, workspace: str | None, tier: str,
                   phase: str, harness: str | None) -> tuple[RuleRecord, ...]:
        def matches(rule):
            a = rule.applies_to
            return ((not a.repos or any(fnmatch.fnmatchcase(name, glob)
                     for name in (repo, workspace) if name for glob in a.repos))
                    and (not a.tiers or tier in a.tiers)
                    and (not a.phases or phase in a.phases)
                    and (not a.harnesses or harness in a.harnesses))
        return tuple(rule for rule in self.merged() if matches(rule))

    def digest(self) -> str:
        # Same canonical representation as execution.store.digest, without importing execution.
        raw = json.dumps([asdict(r) for r in self.merged()], sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()


def _strings(value):
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _load(raw: bytes, *, label: str, source: str, path: Path | None) -> LoadedLayer:
    try:
        doc = yaml.safe_load(raw)
    except (yaml.YAMLError, UnicodeError) as exc:
        raise ValueError(f"{label}: malformed rules file") from exc
    if (not isinstance(doc, dict) or type(doc.get("schema_version")) is not int
            or doc["schema_version"] != 1 or not isinstance(doc.get("rules"), list)):
        raise ValueError(f"{label}: expected schema_version: 1 and a rules list")
    rules, seen = [], set()
    keys = RULES_FILE_SCHEMA_V1["properties"]["rules"]["items"]["properties"]
    for index, row in enumerate(doc["rules"]):
        error = f"{label} rule {index}"
        if not isinstance(row, dict) or set(row) - keys.keys():
            raise ValueError(f"{error}: unknown record key or non-mapping record")
        if not isinstance(row.get("id"), str) or not re.fullmatch(_ID, row["id"]):
            raise ValueError(f"{error}: invalid id")
        if row["id"] in seen:
            raise ValueError(f"{error}: duplicate id {row['id']}")
        seen.add(row["id"])
        for key in ("text", "reason"):
            if not isinstance(row.get(key), str) or not row[key].strip():
                raise ValueError(f"{error}: non-empty {key} required")
            if check_outbound_safe(row[key]):
                raise PermissionError(f"credential-shaped rule text refused: {label} rule {row['id']}")
        applies = row.get("applies_to", {})
        if (not isinstance(applies, dict) or set(applies) - Applicability.__dataclass_fields__.keys()
                or any(not _strings(v) for v in applies.values())):
            raise ValueError(f"{error}: invalid applies_to")
        if type(row.get("enabled", True)) is not bool:
            raise ValueError(f"{error}: enabled must be boolean")
        supersedes = row.get("supersedes", [])
        if not _strings(supersedes) or any(not re.fullmatch(_ID, v) for v in supersedes):
            raise ValueError(f"{error}: invalid supersedes")
        rules.append(RuleRecord(row["id"], row["text"], row["reason"],
                     Applicability(**{k: tuple(v) for k, v in applies.items()}), source,
                     row.get("enabled", True), tuple(supersedes)))
    return LoadedLayer(source, path, hashlib.sha256(raw).hexdigest() if path else None,
                       tuple(rules), {k: v for k, v in doc.items() if k not in ("schema_version", "rules")})


def load_packaged() -> LoadedLayer:
    return _load(files("corral.rules").joinpath("defaults.yaml").read_bytes(),
                 label="packaged defaults", source="packaged", path=None)


def load_rules_file(path: Path, *, source: str, workspace: Path | None = None) -> LoadedLayer:
    path = Path(path)
    if source == "overlay" and workspace is not None and path.resolve().is_relative_to(Path(workspace).resolve()):
        raise PermissionError("overlay must not live inside a repository workspace")
    if source == "repo" and workspace is not None:
        # Import at the point of use: importing this public package must not load execution.
        from corral.execution.workspace import safe_path
        root = Path(workspace).resolve()
        try:
            relative = path.absolute().relative_to(root)
        except ValueError as exc:
            raise PermissionError(f"{path}: path escapes workspace") from exc
        path = safe_path(root, str(relative))
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise ValueError(f"{path}: declared rules file does not exist") from exc
    return _load(raw, label=str(path), source=source, path=path)


def overlay_path(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    if env.get("CORRAL_OVERLAY_FILE"):
        return Path(env["CORRAL_OVERLAY_FILE"]).expanduser()
    return Path(env.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "corral" / "overlay.yaml"


def load_layers(*, workspace: Path | None, repo_rules_file: str | None,
                env: Mapping[str, str] | None = None) -> Layers:
    repo = None
    if repo_rules_file is not None:
        if workspace is None:
            raise ValueError("repository rules require a workspace")
        from corral.execution.workspace import safe_path
        path = safe_path(workspace, repo_rules_file)
        repo = load_rules_file(path, source="repo", workspace=workspace)
    path = overlay_path(env)
    if workspace is not None and path.resolve().is_relative_to(Path(workspace).resolve()):
        raise PermissionError("overlay must not live inside a repository workspace")
    overlay = load_rules_file(path, source="overlay", workspace=workspace) if path.exists() else None
    return Layers(load_packaged(), repo, overlay)
