"""Loaders and JSON-Schema validation for agent-memory files.

Two schemas ship as package data:

- ``schemas/gotchas.schema.json`` — the gotcha registry consumed by
  ``corral preflight`` (default location ``agent_memory/gotchas.json``,
  configurable via ``preflight.gotchas``).
- ``schemas/refinements.schema.json`` — the instruction-refinement ledger
  written by retrospective tooling; a ledger file is either a single record
  object or a JSON array of records.

Validation uses the ``jsonschema`` package (``corral[memory]`` extra). The
loader itself stays stdlib-only so brief generation never needs the extra.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SCHEMAS_DIR = Path(__file__).resolve().parent / "schemas"
GOTCHAS_SCHEMA_NAME = "gotchas.schema.json"
REFINEMENTS_SCHEMA_NAME = "refinements.schema.json"


class MissingOptionalDependencyError(RuntimeError):
    """Raised when schema validation needs the ``corral[memory]`` extra."""


def schema_path(name: str) -> Path:
    path = SCHEMAS_DIR / name
    if not path.is_file():
        raise FileNotFoundError(f"unknown agent-memory schema: {name}")
    return path


def load_schema(name: str) -> dict[str, Any]:
    return json.loads(schema_path(name).read_text(encoding="utf-8"))


def load_gotchas(gotchas_path: Path) -> list[dict[str, Any]]:
    """Load gotcha records from the shared memory registry.

    Mirrors the preflight loader semantics: a missing file yields ``[]`` and
    non-object entries are dropped.
    """
    if not gotchas_path.exists():
        return []
    payload = json.loads(gotchas_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return []
    gotchas = payload.get("gotchas", [])
    if not isinstance(gotchas, list):
        return []
    return [entry for entry in gotchas if isinstance(entry, dict)]


def _validator(schema: dict[str, Any]) -> Any:
    try:
        from jsonschema import Draft202012Validator
    except ImportError as exc:
        raise MissingOptionalDependencyError(
            "schema validation requires the jsonschema package; "
            "install it with: pip install 'corral[memory]'"
        ) from exc
    return Draft202012Validator(schema)


def validate_payload(payload: Any, schema_name: str) -> list[str]:
    """Return human-readable validation errors (empty list means valid)."""
    schema = load_schema(schema_name)
    validator = _validator(schema)
    errors: list[str] = []
    for error in sorted(validator.iter_errors(payload), key=lambda e: list(e.absolute_path)):
        location = "/" + "/".join(str(part) for part in error.absolute_path)
        if location == "/":
            location = "<root>"
        errors.append(f"{location}: {error.message}")
    return errors


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def validate_gotchas_file(path: Path) -> list[str]:
    """Validate a gotcha-registry file against the gotchas schema."""
    return validate_payload(_read_json(path), GOTCHAS_SCHEMA_NAME)


def validate_refinements_file(path: Path) -> list[str]:
    """Validate a refinement-ledger file against the refinements schema.

    Accepts a single record object or a JSON array of records.
    """
    payload = _read_json(path)
    records = payload if isinstance(payload, list) else [payload]
    errors: list[str] = []
    for index, record in enumerate(records):
        prefix = f"[{index}] " if isinstance(payload, list) else ""
        errors.extend(prefix + message for message in validate_payload(record, REFINEMENTS_SCHEMA_NAME))
    return errors
