from dataclasses import replace

import pytest

from corral.execution.demo import fixture_profiles
from corral.execution.phases import select_phase
from corral.execution.pr_demo import run
from corral.execution.profiles import resolve
from tests.test_execution_regressions import setup, spec  # noqa: F401


def test_adaptive_phase_native_effort_and_scope(setup):  # noqa: F811
    controller, repo = setup
    task = controller.submit("owner", "phases", spec(repo))
    first = select_phase(controller, "owner", task, "diagnose", role="implementation",
        request={"default": "strong-low", "demand": "deep"})
    second = select_phase(controller, "owner", task, "specialist", role="review", previous="diagnose",
        request={"default": "strong-low", "evidence": {"weak-xhigh": 10}})
    third = select_phase(controller, "owner", task, "finish", role="implementation", previous="specialist",
        request={"default": "strong-low", "effort": "low"})
    assert first["selection"]["profile"]["id"] == third["selection"]["profile"]["id"] == "strong-low"
    assert second["session_action"] == "new-session-with-saved-context"
    assert first["acceptance"] == third["acceptance"]
    mapped = [replace(fixture_profiles()[0], portable_intent="light", mapping_version="fixture-v2", speed="fast")]
    selected = resolve(mapped, role="implementation", routes=["fixture"], portable_intent="light")
    assert selected["profile"]["effort"] == "low" and selected["profile"]["speed"] == "fast"
    with pytest.raises(PermissionError):
        resolve(mapped, role="implementation", routes=["fixture"], portable_intent="deep")
    strong_high = replace(fixture_profiles()[0], id="strong-high", effort="high")
    pool = [*fixture_profiles(), strong_high]
    assert resolve(pool, role="implementation", routes=["fixture"], demand="deep",
                   evidence={"strong-high": 9}, default="strong-low")["profile"]["id"] == "strong-high"
    assert resolve(pool, role="implementation", routes=["fixture"], model="fixture-strong",
                   effort="low", default="strong-low")["profile"]["id"] == "strong-low"


def test_fake_pr_with_actual_repair_process(tmp_path):
    evidence = run(tmp_path)
    assert evidence["actual"]["merge"]["merged"]
    assert evidence["paid_model_calls"] == 0
    assert len(evidence["pr_usage"]) == 3
