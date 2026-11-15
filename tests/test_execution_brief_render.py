import json
import re
from copy import deepcopy

from corral.execution import executor_brief_render as renderer, result_schema
from corral.execution.executor_brief import digest
from .executor_brief_support import sample_brief


def render(**kw):
    args = dict(
        brief=sample_brief(),
        objective="Implement addition.",
        rules=[],
        prompt_extras=None,
        amendment_history=[],
        schema_delivery="harness-flag",
        schema_file_hint="{schema_file}",
        result_file_hint="{result_file}",
        result_schema=result_schema.RESULT_SCHEMA_V1,
        legacy_notice=None,
    )
    return renderer.render(**{**args, **kw})


def test_sections_appear_in_the_fixed_order_with_numbered_criteria():
    text = render()
    headings = [
        "# Task",
        "## Goal",
        "## Why",
        "## Sources, in priority order",
        "## Acceptance criteria",
        "## Verification commands",
        "## Constraints",
        "## Scope",
        "## If something is unclear",
        "## Result",
    ]
    assert [text.index(h) for h in headings] == sorted(text.index(h) for h in headings)
    assert "AC1. Addition returns the sum." in text
    assert "1. `math_ops.py` (authoritative)" in text


def test_each_criterion_names_its_verifying_command_and_optional_test():
    brief = sample_brief()
    brief["acceptance"].append(
        {"id": "AC2", "text": "A second criterion.", "verify": "unit"}
    )
    brief["commands"][0].update(
        argv=["python", "-c", "print('hello world')"],
        env=["TEST_MODE"],
        evidence="The assertion passes.",
    )
    text = render(brief=brief)
    assert text.count("Verified by command `unit`.") == 2
    assert text.count("Test: tests/test_math_ops.py::test_add") == 1
    assert "needs: TEST_MODE" in text and "evidence: The assertion passes." in text
    assert "in `.`" in text and "runs: workspace" in text


def test_constraints_carry_reasons_and_rule_ids_in_merged_order():
    rules = [
        {"id": "rule.one", "text": "Keep input stable.", "reason": "Callers share it."},
        {"id": "rule.two", "text": "Report checks.", "reason": "Evidence is reviewed."},
    ]
    text = render(rules=rules)
    constraints = text.split("## Constraints")[1].split("## Scope")[0]
    assert all(
        " — because " in line
        for line in constraints.splitlines()
        if line.startswith("- ")
    )
    assert (
        constraints.index("Keep the add signature.")
        < constraints.index("[rule.one]")
        < constraints.index("[rule.two]")
    )


def test_harness_flag_delivery_omits_the_inline_schema_and_inline_delivery_includes_it():
    assert "```json" not in render()
    assert (
        "The harness collects it against the result schema it was launched with."
        in render()
    )
    inline = render(schema_delivery="inline")
    assert (
        json.loads(inline.split("```json\n")[1].split("```")[0])
        == result_schema.RESULT_SCHEMA_V1
    )
    assert "{schema_file}" not in inline and "{result_file}" not in inline


def test_register_has_no_pressure_language_or_numeric_output_caps():
    text = render()
    assert not re.search(
        r"\b(MUST|NEVER|ALWAYS|CRITICAL|IMPORTANT)\b|at most \d+ (words|sentences)|non-negotiable|you tend to",
        text,
    )


def test_legacy_mode_renders_the_objective_verbatim_then_rules_then_result():
    objective = "  Keep this objective exactly.\nA second line.  "
    text = render(
        brief=None, objective=objective, legacy_notice="No criteria declared."
    )
    assert text.startswith("# Task\n\n" + objective + "\n\nNo criteria declared.")
    assert text.index("## Constraints") < text.index("## Result")
    assert (
        "one entry per acceptance criterion you can identify in the task, or an empty list"
        in text
    )


def test_amendment_history_keeps_the_header_and_compacts_brief_amendments():
    brief = sample_brief()
    brief["why"] = "Unique context excluded from compact history."
    history = [
        {
            "amendment_id": "new",
            "sequence": 1,
            "value": {"brief": brief, "mode": "interactive"},
        }
    ]
    original = deepcopy(history)
    text = render(amendment_history=history)
    assert renderer.HISTORY_HEADER in text
    value = json.loads(text.split(renderer.HISTORY_HEADER)[1].strip()[2:])["value"]
    assert value["brief"] == {"goal": brief["goal"], "acceptance": ["AC1"]}
    assert value["brief_digest"] == digest(brief) and value["mode"] == "interactive"
    assert brief["why"] not in text and history == original


def test_render_is_deterministic_for_equal_inputs():
    assert render() == render(brief=deepcopy(sample_brief()))


def test_render_never_substitutes_route_placeholders_in_brief_text():
    brief = sample_brief("Preserve {workspace} and {schema_file} literally.")
    assert brief["goal"] in render(brief=brief)


def test_continuation_context_is_verbatim_and_optional():
    extra = "  A checkpoint with whitespace.\n"
    assert "## Continuation context\n\n" + extra in render(prompt_extras=extra)
    assert "## Continuation context" not in render()


def test_cli_render_uses_repository_rules_and_delivery_flag(
    tmp_path, monkeypatch, capsys
):
    from corral.cli import main

    monkeypatch.setenv("CORRAL_OVERLAY_FILE", str(tmp_path.parent / "absent-overlay"))
    (tmp_path / "corral.yaml").write_text("executor_brief: {rules_file: rules.yaml}")
    (tmp_path / "rules.yaml").write_text(
        "schema_version: 1\nrules:\n- id: project.test\n  text: Report checks.\n  reason: Evidence is reviewed.\n"
    )
    path = tmp_path / "brief.json"
    path.write_text(json.dumps(sample_brief()))
    assert (
        main(
            [
                "executor-brief",
                "render",
                str(path),
                "--root",
                str(tmp_path),
                "--inline-schema",
            ]
        )
        == 0
    )
    text = capsys.readouterr().out
    assert "[project.test]" in text and "```json" in text


def test_cli_render_reports_malformed_nearest_config(tmp_path, monkeypatch, capsys):
    from corral.cli import main

    path = tmp_path / "task.json"
    path.write_text(json.dumps(sample_brief()))
    (tmp_path / "corral.yaml").write_text("executor_brief: [")
    monkeypatch.chdir(tmp_path)
    assert main(["executor-brief", "render", str(path)]) == 1
    assert capsys.readouterr().err
