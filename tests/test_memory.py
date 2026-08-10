"""Agent-memory schema, loader, and `corral memory validate` tests.

Schema-validation cases skip cleanly when the optional ``jsonschema`` extra
(``corral[memory]``) is not installed; loader and schema-data checks always
run.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from corral.cli import main
from corral.memory import registry

FIXTURE_GOTCHAS_PATH = Path(__file__).parent / "fixtures" / "gotchas.json"

VALID_REFINEMENT = {
    "id": "REF-20260101T000000Z-001",
    "timestamp": "2026-01-01T00:00:00Z",
    "target_path": "src/api/orders.py",
    "before_snapshot": "threshold = 100",
    "after_snapshot": "threshold = 150",
    "before_exists": True,
    "edit_snapshots": [
        {
            "target_path": "src/api/orders.py",
            "before_snapshot": "threshold = 100",
            "after_snapshot": "threshold = 150",
            "before_exists": True,
        }
    ],
    "evidence_refs": ["session/2026-01-01/abc", "ci/run/123"],
    "status": "pending_human_review",
}


def jsonschema_or_skip() -> None:
    pytest.importorskip("jsonschema")


# --- schema data -----------------------------------------------------------


def test_gotchas_schema_id_points_at_corral() -> None:
    schema = registry.load_schema(registry.GOTCHAS_SCHEMA_NAME)
    assert schema["$id"].startswith("https://github.com/dnlrshv/corral/")
    assert "trading" not in json.dumps(schema).lower()
    serialized = json.dumps(schema)
    assert "payments-config" in serialized or "surface" in serialized.lower()


def test_refinements_schema_loads() -> None:
    schema = registry.load_schema(registry.REFINEMENTS_SCHEMA_NAME)
    assert schema["$id"].startswith("https://github.com/dnlrshv/corral/")
    assert schema["$defs"]["EditSnapshot"]["type"] == "object"


def test_unknown_schema_name_rejected() -> None:
    with pytest.raises(FileNotFoundError):
        registry.schema_path("nope.schema.json")


# --- loader ----------------------------------------------------------------


def test_load_gotchas_fixture() -> None:
    gotchas = registry.load_gotchas(FIXTURE_GOTCHAS_PATH)
    assert [entry["id"] for entry in gotchas] == [
        "G-2025-001",
        "G-2025-002",
        "G-2025-003",
        "G-2025-004",
        "G-2025-005",
    ]


def test_load_gotchas_missing_file(tmp_path: Path) -> None:
    assert registry.load_gotchas(tmp_path / "missing.json") == []


# --- validation (requires jsonschema) ---------------------------------------


def test_fixture_gotchas_validate_against_schema() -> None:
    jsonschema_or_skip()
    payload = json.loads(FIXTURE_GOTCHAS_PATH.read_text())
    assert registry.validate_payload(payload, registry.GOTCHAS_SCHEMA_NAME) == []


@pytest.mark.parametrize(
    "mutate",
    [
        lambda entry: entry.update(id="G-25-1"),  # bad id pattern
        lambda entry: entry.pop("rule"),  # missing required field
        lambda entry: entry.update(extra="nope"),  # additionalProperties
        lambda entry: entry.update(control_type="vibes"),  # bad enum
        lambda entry: entry.update(source_prs=["12"]),  # wrong item type
        lambda entry: entry.update(rule=""),  # minLength
    ],
    ids=["bad-id", "missing-rule", "extra-property", "bad-control-type", "bad-source-prs", "empty-rule"],
)
def test_gotcha_schema_rejects_invalid_entries(mutate) -> None:
    jsonschema_or_skip()
    payload = json.loads(FIXTURE_GOTCHAS_PATH.read_text())
    mutate(payload["gotchas"][0])
    errors = registry.validate_payload(payload, registry.GOTCHAS_SCHEMA_NAME)
    assert errors


def test_gotchas_top_level_shape_rejected() -> None:
    jsonschema_or_skip()
    assert registry.validate_payload({"gotchas": "nope"}, registry.GOTCHAS_SCHEMA_NAME)
    assert registry.validate_payload({"other": []}, registry.GOTCHAS_SCHEMA_NAME)


def test_refinement_schema_accept_and_reject() -> None:
    jsonschema_or_skip()
    assert registry.validate_payload(VALID_REFINEMENT, registry.REFINEMENTS_SCHEMA_NAME) == []

    bad_status = copy.deepcopy(VALID_REFINEMENT)
    bad_status["status"] = "approved"
    assert registry.validate_payload(bad_status, registry.REFINEMENTS_SCHEMA_NAME)

    weak_evidence = copy.deepcopy(VALID_REFINEMENT)
    weak_evidence["evidence_refs"] = ["session/only-one"]
    assert registry.validate_payload(weak_evidence, registry.REFINEMENTS_SCHEMA_NAME)

    absolute_path = copy.deepcopy(VALID_REFINEMENT)
    absolute_path["target_path"] = "/etc/passwd"
    assert registry.validate_payload(absolute_path, registry.REFINEMENTS_SCHEMA_NAME)

    traversal = copy.deepcopy(VALID_REFINEMENT)
    traversal["target_path"] = "src/../../etc/passwd"
    assert registry.validate_payload(traversal, registry.REFINEMENTS_SCHEMA_NAME)


# --- CLI --------------------------------------------------------------------


def test_memory_validate_cli_accepts_fixture(tmp_path: Path, capsys) -> None:
    jsonschema_or_skip()
    rc = main(["memory", "validate", "--gotchas", str(FIXTURE_GOTCHAS_PATH)])
    assert rc == 0
    assert "valid" in capsys.readouterr().out


def test_memory_validate_cli_rejects_invalid(tmp_path: Path, capsys) -> None:
    jsonschema_or_skip()
    bad = tmp_path / "gotchas.json"
    payload = json.loads(FIXTURE_GOTCHAS_PATH.read_text())
    payload["gotchas"][0]["id"] = "not-a-gotcha-id"
    bad.write_text(json.dumps(payload))

    rc = main(["memory", "validate", "--gotchas", str(bad)])

    assert rc == 1
    assert "INVALID" in capsys.readouterr().out


def test_memory_validate_cli_refinements_ledger(tmp_path: Path, capsys) -> None:
    jsonschema_or_skip()
    ledger = tmp_path / "refinements.json"
    ledger.write_text(json.dumps([VALID_REFINEMENT]))

    rc = main(
        [
            "memory",
            "validate",
            "--gotchas",
            str(FIXTURE_GOTCHAS_PATH),
            "--refinements",
            str(ledger),
        ]
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert "gotcha registry valid" in out
    assert "refinement ledger valid" in out


def test_memory_validate_cli_missing_default_registry_notes_and_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    jsonschema_or_skip()
    monkeypatch.chdir(tmp_path)
    rc = main(["memory", "validate"])
    assert rc == 0
    assert "nothing to validate" in capsys.readouterr().out


def test_memory_validate_cli_rejects_non_json(tmp_path: Path, capsys) -> None:
    jsonschema_or_skip()
    bad = tmp_path / "gotchas.json"
    bad.write_text("{not json")
    rc = main(["memory", "validate", "--gotchas", str(bad)])
    assert rc == 1
    assert "not valid JSON" in capsys.readouterr().err
