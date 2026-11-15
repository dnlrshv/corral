import hashlib
import json
from dataclasses import asdict
from pathlib import Path

import pytest
import yaml

from corral.execution import containment, prelaunch_refusal
from corral.execution.profiles import Profile, registered_profiles
from . import native_support as ns
from .executor_brief_support import sample_brief
from .test_execution_adapter import SUCCESS_OPS

requires_sandbox = pytest.mark.skipif(
    containment.sandbox_exec() is None,
    reason="real worker containment requires macOS sandbox-exec",
)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CORRAL_OVERLAY_FILE", str(tmp_path / "absent-overlay"))
    return ns.native_env(tmp_path)


def config(env, **settings):
    (env["workspace"] / "corral.yaml").write_text(
        yaml.safe_dump({"executor_brief": settings})
    )


def run(env, *, brief=False, request="brief", **overrides):
    spec = ns.native_spec(env, ops=SUCCESS_OPS, **overrides)
    if brief:
        spec["brief"] = sample_brief(spec["objective"])
    task = env["controller"].submit("owner", request, spec)
    return task, env["controller"].run("owner", task, execution_host=ns.FAKE_HOST)


def refuse(env, *, brief=False, **overrides):
    spec = ns.native_spec(env, ops=SUCCESS_OPS, **overrides)
    if brief:
        spec["brief"] = sample_brief(spec["objective"])
    controller = env["controller"]
    task = controller.submit("owner", "refused", spec)
    with pytest.raises((PermissionError, ValueError)):
        controller.run("owner", task, execution_host=ns.FAKE_HOST)
    state = controller.store.get("state", task)
    assert state["status"] == "refused-before-launch"
    resource = "workspace:" + str(env["workspace"].resolve())
    with controller.store.transaction() as db:
        assert (
            prelaunch_refusal.prove_db(db, task, 1, resource)["process"]
            == "never-launched"
        )
    return task, state


def test_profile_tier_defaults_to_standard_and_rejects_unknown_values():
    fields = asdict(ns.fake_profile())
    fields.pop("tier")
    assert Profile(**fields).tier == "standard"
    for value in ("tiny", "", None):
        with pytest.raises(
            ValueError, match="tier must be one of small, standard, frontier"
        ):
            registered_profiles([{**fields, "tier": value}])
    assert all(
        p.tier == "small"
        for p in registered_profiles()
        if p.id.startswith("gemini-3.8-flash-")
    )
    assert (
        next(
            p for p in registered_profiles() if p.id == "qwen3.8-max-high-codex-alibaba"
        ).tier
        == "standard"
    )


@requires_sandbox
def test_legacy_objective_warns_by_default_and_the_fixture_still_completes(env):
    _, status = run(env)
    assert status["result"]["accepted"] is True
    assert status["result"]["brief"]["mode"] == "legacy-objective"
    assert len(status["result"]["brief"]["warnings"]) == 1


@requires_sandbox
def test_legacy_objective_refuse_is_a_prelaunch_refusal_with_a_recorded_reason(env):
    config(env, legacy_objective="refuse")
    _, state = refuse(env)
    assert state["reason"].startswith(
        "native dispatch requires a corral-executor-brief-v1 brief"
    )


@requires_sandbox
def test_brief_dispatch_writes_brief_json_and_context_fields(env):
    from corral.execution.executor_brief import digest

    _, status = run(env, brief=True)
    output = Path(status["result"]["artifact_directory"])
    context = json.loads((output / "context.json").read_text())
    record = json.loads((output / "brief.json").read_text())
    for key in (
        "brief",
        "brief_mode",
        "rules_applied",
        "brief_render",
        "result_schema",
    ):
        assert key in context
    assert record["brief_digest"] == digest(context["brief"])
    assert record["render"] == context["brief_render"]
    assert record["mode"] == status["state"]["brief_mode"] == "brief"
    assert record["warnings"] == []
    assert status["result"]["brief"]["rules_digest"] == record["rules_digest"]


@requires_sandbox
def test_repo_rules_file_is_read_from_the_workspace_and_recorded_with_its_sha256(env):
    config(env, rules_file="rules.yaml")
    path = env["workspace"] / "rules.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "rules": [
                    {
                        "id": "project.test",
                        "text": "Keep addition callable.",
                        "reason": "Callers import it.",
                        "applies_to": {
                            "tiers": ["small"],
                            "phases": ["implementation"],
                            "harnesses": [ns.FAKE_HARNESS],
                            "repos": ["fixture-*"],
                        },
                    }
                ],
            }
        )
    )
    _, status = run(env, brief=True)
    record = json.loads(
        (Path(status["result"]["artifact_directory"]) / "brief.json").read_text()
    )
    assert (
        record["layers"]["repo"]["sha256"]
        == hashlib.sha256(path.read_bytes()).hexdigest()
    )
    assert (
        record["render"]["config_sha256"]
        == hashlib.sha256((env["workspace"] / "corral.yaml").read_bytes()).hexdigest()
    )
    assert record["rules_applied"][-1]["id"] == "project.test"


@requires_sandbox
@pytest.mark.parametrize("path", ["../rules.yaml", "/rules.yaml", "link.yaml"])
def test_repo_rules_file_outside_the_workspace_or_through_a_symlink_is_refused(
    env, path
):
    config(env, rules_file=path)
    (env["workspace"] / "link.yaml").symlink_to(env["workspace"].parent / "rules.yaml")
    refuse(env)


@requires_sandbox
def test_malformed_repo_rules_file_refuses_before_launch(env):
    config(env, rules_file="rules.yaml")
    (env["workspace"] / "rules.yaml").write_text("[]")
    _, state = refuse(env)
    assert "rules.yaml" in state["reason"]


@requires_sandbox
def test_malformed_overlay_refuses_before_launch_and_a_missing_one_is_fine(
    env, tmp_path, monkeypatch
):
    path = tmp_path / "overlay.yaml"
    path.write_text("schema_version: 2\nrules: []")
    monkeypatch.setenv("CORRAL_OVERLAY_FILE", str(path))
    _, state = refuse(env)
    assert str(path) in state["reason"]
    path.unlink()
    _, status = run(env, request="after-overlay-repair")
    assert status["result"]["accepted"] is True


@requires_sandbox
def test_overlay_inside_the_workspace_is_refused(env, monkeypatch):
    monkeypatch.setenv("CORRAL_OVERLAY_FILE", str(env["workspace"] / "overlay.yaml"))
    _, state = refuse(env)
    assert "overlay must not live" in state["reason"]


@requires_sandbox
def test_credential_shaped_brief_text_from_an_amendment_refuses_before_launch(env):
    controller = env["controller"]
    spec = ns.native_spec(env, ops=SUCCESS_OPS)
    task = controller.submit("owner", "credential-amendment", spec)
    value = sample_brief(spec["objective"])
    value["why"] = "api_key = 'not-a-real-secret-value'"
    # Exercise the launch-time recheck independently of the authenticated submit check.
    controller.store.append_ordered("amendment", task + ":stored", {"brief": value})
    with pytest.raises(PermissionError):
        controller.run("owner", task, execution_host=ns.FAKE_HOST)
    state = controller.store.get("state", task)
    assert "credential-shaped brief text refused" in state["reason"]
    assert "not-a-real-secret-value" not in json.dumps(state)
    with controller.store.transaction() as db:
        assert prelaunch_refusal.prove_db(
            db, task, 1, "workspace:" + str(env["workspace"].resolve())
        )


@requires_sandbox
@pytest.mark.parametrize("budget", [None, 10])
def test_prompt_over_budget_refuses_before_launch_with_tier_and_sizes_in_the_reason(
    env, budget
):
    if budget is not None:
        config(env, max_prompt_chars=budget)
    _, state = refuse(env, brief=True, objective="x" * 40001)
    assert f"budget {budget or 40000} for tier small" in state["reason"]
    assert "rendered brief is" in state["reason"] and "characters" in state["reason"]


@requires_sandbox
def test_result_assessment_is_recorded_on_the_result_and_in_status(env):
    task, status = run(env, brief=True)
    report = status["result"]["brief"]["report"]
    assert (
        report["schema_valid"] is False
    )  # Legacy fixture result is still accepted by the verifier.
    assert report["criteria"]["unreported"] == ["AC1"]
    assert status["result"]["accepted"] is True
    assert (
        env["controller"].status("owner", task)["result"]["brief"]["report"] == report
    )


@requires_sandbox
def test_equal_specs_have_equal_prompt_and_rules_digests(env):
    _, first = run(env, brief=True, request="first")
    _, second = run(env, brief=True, request="second")
    one, two = [
        json.loads((Path(s["result"]["artifact_directory"]) / "brief.json").read_text())
        for s in (first, second)
    ]
    assert one["render"] == two["render"]
    assert one["brief_digest"] == two["brief_digest"]
    assert one["rules_applied"] == two["rules_applied"]


@requires_sandbox
def test_workspace_config_does_not_search_parents_and_invalid_unrelated_sections_refuse(
    env,
):
    (env["workspace"].parent / "corral.yaml").write_text(
        "executor_brief: {legacy_objective: refuse}"
    )
    _, status = run(env, request="ignores-parent")
    assert status["result"]["accepted"] is True
    (env["workspace"] / "corral.yaml").write_text("retro: {max_tokens: wrong}")
    _, state = refuse(env)
    assert "retro.max_tokens" in state["reason"]


def test_deterministic_dispatch_skips_brief_policy(tmp_path):
    from .test_execution_service_cli import git_repo, configs
    from corral.execution.service import Service

    workspace = git_repo(tmp_path / "workspace")
    (workspace / "corral.yaml").write_text("executor_brief: {legacy_objective: refuse}")
    service = Service(configs(tmp_path, workspace))
    status = service.submit("deterministic", "demo", "Copy input.")
    task = status["event"]["task_id"]
    status = service.controller.run(service.token, task, execution_host="primary")
    assert status["result"]["accepted"] is True and status["result"]["brief"] is None
    assert not (Path(status["result"]["artifact_directory"]) / "brief.json").exists()


@requires_sandbox
def test_credential_shaped_prompt_extras_refuses_before_launch(env):
    _, state = refuse(env, prompt_extras="api_key = 'not-a-real-secret-value'")
    assert "credential-shaped prompt_extras text refused" in state["reason"]
    assert "not-a-real-secret-value" not in json.dumps(state)
