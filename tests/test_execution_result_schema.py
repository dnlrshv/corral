from copy import deepcopy

import pytest

from corral.execution import adapter, containment, result_schema
from .executor_brief_support import sample_brief


def sample_result():
    return {
        "schema": "corral-executor-result-v1",
        "status": "done",
        "summary": "Implemented sum.",
        "acceptance": [
            {
                "id": "AC1",
                "status": "met",
                "command": "unit",
                "evidence": "test_add passed",
            }
        ],
        "commands_run": [
            {
                "id": "unit",
                "argv": ["python", "-m", "pytest"],
                "cwd": ".",
                "exit_code": 0,
                "output_tail": "1 passed",
            }
        ],
        "files_changed": ["math_ops.py"],
        "open_questions": [],
        "blockers": [],
    }


def nodes(schema):
    yield schema
    for child in schema.get("properties", {}).values():
        yield from nodes(child)
    if "items" in schema:
        yield from nodes(schema["items"])


def test_default_schema_is_v1_and_uses_only_the_portable_keyword_subset():
    assert adapter.DEFAULT_RESULT_SCHEMA is result_schema.RESULT_SCHEMA_V1
    allowed = {"type", "properties", "required", "items", "enum", "description"}
    assert all(set(node) <= allowed for node in nodes(result_schema.RESULT_SCHEMA_V1))
    assert result_schema.RESULT_SCHEMA_V1["properties"]["schema"]["enum"] == [
        "corral-executor-result-v1"
    ]


def test_every_property_is_required_at_every_object_level():
    for node in nodes(result_schema.RESULT_SCHEMA_V1):
        if node.get("type") == "object":
            assert set(node["properties"]) == set(node["required"])


def test_codex_variant_closes_every_object_and_leaves_the_base_untouched():
    original = deepcopy(result_schema.RESULT_SCHEMA_V1)
    for node in nodes(result_schema.for_harness("codex")):
        if node.get("type") == "object":
            assert node["additionalProperties"] is False
    assert result_schema.RESULT_SCHEMA_V1 == original
    assert result_schema.for_harness("agy") == original


def test_assess_reports_met_not_met_not_verified_unreported_and_unknown():
    brief = sample_brief()
    brief["acceptance"] += [
        {"id": id, "text": "Criterion.", "verify": "unit"}
        for id in ("AC2", "AC3", "AC4")
    ]
    value = sample_result()
    value["acceptance"] += [
        {"id": id, "status": status, "command": "unit", "evidence": "Observed."}
        for id, status in (("AC2", "not-met"), ("AC3", "not-verified"), ("AC99", "met"))
    ]
    report = result_schema.assess(value, brief)
    assert report["schema_valid"] is True
    assert report["criteria"] == {
        "declared": ["AC1", "AC2", "AC3", "AC4"],
        "met": ["AC1", "AC99"],
        "not_met": ["AC2"],
        "not_verified": ["AC3"],
        "unreported": ["AC4"],
        "unknown": ["AC99"],
    }


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        "done",
        42,
        {"done": True},
        {"acceptance": [None, {"id": []}], "commands_run": [None, {"exit_code": []}]},
    ],
)
def test_assess_tolerates_legacy_free_form_and_non_object_results(value):
    report = result_schema.assess(value, None)
    assert report["schema_valid"] is False
    assert report["criteria"]["declared"] == []
    if not isinstance(value, dict):
        assert report["errors"] == ["structured completion is not an object"]


def test_assess_counts_commands_nonzero_exits_and_files():
    value = sample_result()
    value["commands_run"] += [
        {**value["commands_run"][0], "exit_code": 1},
        {**value["commands_run"][0], "exit_code": 0},
    ]
    value["open_questions"] = ["Which API?"]
    report = result_schema.assess(value, sample_brief())
    assert report["commands_run"] == 3 and report["nonzero_exits"] == 1
    assert (
        report["files_changed"] == 1
        and report["open_questions"] == 1
        and report["blockers"] == 0
    )


def test_nested_shape_errors_are_reported():
    value = sample_result()
    value["acceptance"][0]["status"] = "yes"
    value["commands_run"][0]["exit_code"] = True
    value["files_changed"] = [42]
    report = result_schema.assess(value, sample_brief())
    assert report["schema_valid"] is False
    assert len(report["errors"]) == 3


@pytest.mark.skipif(
    containment.sandbox_exec() is None,
    reason="real worker containment requires macOS sandbox-exec",
)
def test_native_result_carries_informational_result_assessment(tmp_path):
    from . import native_support as ns
    from .test_execution_adapter import SUCCESS_OPS

    env = ns.native_env(tmp_path)
    controller = env["controller"]
    task = controller.submit(
        "owner", "result-assessment", ns.native_spec(env, ops=SUCCESS_OPS)
    )
    status = controller.run("owner", task, execution_host=ns.FAKE_HOST)
    assert status["result"]["accepted"] is True
    assert status["result"]["brief"]["report"]["schema_valid"] is False
