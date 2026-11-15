import json
from copy import deepcopy
from importlib.resources import files

import pytest

from corral.cli import main
from corral.execution import executor_brief as brief
from .executor_brief_support import sample_brief
from .test_execution_regressions import setup, spec  # noqa: F401


def test_schema_file_equals_the_module_constant():
    schema = json.loads(
        files("corral.execution")
        .joinpath("schemas/executor_brief.schema.json")
        .read_text()
    )
    assert schema == brief.BRIEF_SCHEMA_V1
    import jsonschema

    jsonschema.validate(sample_brief(), schema)


def test_valid_brief_passes_with_no_errors():
    assert brief.validate(sample_brief()) == []


def test_brief_without_acceptance_is_rejected():
    value = sample_brief()
    del value["acceptance"]
    assert any("acceptance" in error for error in brief.validate(value))
    value["acceptance"] = []
    assert brief.validate(value)


def test_brief_without_commands_is_rejected():
    value = sample_brief()
    del value["commands"]
    assert any("commands" in error for error in brief.validate(value))
    value["commands"] = []
    assert brief.validate(value)


def test_acceptance_verify_must_name_a_declared_command():
    value = sample_brief()
    value["acceptance"][0]["verify"] = "missing"
    assert any("declared command" in e for e in brief.validate(value))


def test_duplicate_command_or_criterion_ids_are_rejected():
    for key in ("commands", "acceptance"):
        value = sample_brief()
        value[key].append(deepcopy(value[key][0]))
        assert any("duplicate" in e for e in brief.validate(value))


def test_constraint_without_reason_is_rejected():
    value = sample_brief()
    value["constraints"][0].pop("reason")
    assert "constraint 0 has no reason" in brief.validate(value)


@pytest.mark.parametrize(
    "path", ["", "/absolute", "~/home", "a/../b", "a//b", "./a", "a/", ".."]
)
def test_paths_must_be_relative_normalized_and_free_of_parent_references(path):
    for key in ("source", "change", "read_only", "cwd"):
        value = sample_brief()
        if key == "source":
            value["sources"][0]["path"] = path
        elif key == "cwd":
            value["commands"][0]["cwd"] = path
        else:
            value["scope"][key] = [path]
        assert any("relative normalized" in e for e in brief.validate(value))


def test_unknown_schema_or_extra_keys_are_rejected():
    value = sample_brief()
    value["schema"] = "corral-unknown-v2"
    value["extra"] = True
    value["commands"][0]["shell"] = True
    assert len(brief.validate(value)) == 3


def test_credential_shaped_text_is_a_permission_error_naming_the_field():
    value = sample_brief()
    value["commands"][0]["evidence"] = "api_key = 'not-a-real-secret-value'"
    with pytest.raises(PermissionError, match=r"brief.commands\[0\].evidence"):
        brief.validate(value)


def test_submit_derives_objective_from_goal(setup):  # noqa: F811
    controller, repo = setup
    value = sample_brief()
    task = controller.submit("owner", "brief", {**spec(repo), "brief": value})
    stored = controller.store.get("request", task)
    assert stored["objective"] == value["goal"]
    assert stored["brief"] == value


def test_submit_refuses_an_objective_that_differs_from_goal(setup):  # noqa: F811
    controller, repo = setup
    with pytest.raises(ValueError, match="objective and brief.goal differ"):
        controller.submit(
            "owner",
            "brief",
            {**spec(repo), "brief": sample_brief(), "objective": "Other task"},
        )


def test_steer_accepts_a_brief_and_keeps_objective_coherent(setup):  # noqa: F811
    controller, repo = setup
    task = controller.submit("owner", "brief", spec(repo))
    value = sample_brief()
    controller.steer("owner", task, "amend", {"brief": value})
    assert controller.context(task)["brief"] == value
    assert controller.context(task)["objective"] == value["goal"]


def test_continuation_accepts_a_brief_amendment_and_refuses_it_as_a_binding(tmp_path):
    from . import continuation_support as cs
    from corral.execution import continuation
    from .test_execution_continuation_authority import AUTHORITY_KEYS

    env = cs.deterministic_env(tmp_path)
    task, _ = cs.run_stage1(env)
    value = sample_brief("Extend the duration parser.")
    controller = env["controller"]
    result = controller.continue_task("owner", task, "new", {"brief": value})
    assert result["generation"] == 2
    assert controller.context(task)["brief"] == value
    assert controller.context(task)["objective"] == value["goal"]
    assert "brief" not in result["continuation"]["binding"]
    assert "brief" not in continuation.BINDING_KEYS and "brief" not in AUTHORITY_KEYS
    assert (
        controller.continue_task("owner", task, "new", {"brief": value})["scheduled"]
        is False
    )
    changed = deepcopy(value)
    changed["why"] = "Changed context."
    with pytest.raises(ValueError, match="conflicting continuation identity"):
        controller.continue_task("owner", task, "new", {"brief": changed})
    with pytest.raises(ValueError, match="objective and brief.goal differ"):
        controller.continue_task(
            "owner", task, "other", {"brief": value, "objective": "Other"}
        )


def test_service_submit_stores_the_brief_in_the_resolved_spec(tmp_path):
    from .test_execution_service_cli import git_repo, configs
    from corral.execution.service import Service

    service = Service(configs(tmp_path, git_repo(tmp_path / "workspace")))
    value = sample_brief()
    status = service.submit("brief", "demo", brief=value)
    task = status["event"]["task_id"]
    assert service.controller.store.get("request", task)["brief"] == value
    assert status["event"]["resolved_spec"]["brief"] == value


def test_agent_cli_submit_reads_a_brief_file_and_objective_becomes_optional(tmp_path):
    from .test_execution_service_cli import git_repo, configs, cli
    from corral.execution.store import Store

    config = configs(tmp_path, git_repo(tmp_path / "workspace"))
    value = sample_brief()
    path = tmp_path / "brief.json"
    path.write_text(json.dumps(value))
    status = cli(
        "corral.execution.agent_cli",
        "--config",
        config,
        "submit",
        "--repository",
        "demo",
        "--event-id",
        "brief",
        "--brief",
        path,
    )
    store = Store(tmp_path / "state/controller.sqlite")
    request = store.get("request", status["event"]["task_id"])
    assert request["brief"] == value and request["objective"] == value["goal"]


def test_cli_brief_check_reports_errors_and_exit_status(tmp_path, capsys):
    path = tmp_path / "brief.json"
    path.write_text(json.dumps(sample_brief()))
    assert main(["executor-brief", "check", str(path)]) == 0
    path.write_text("{}")
    assert main(["executor-brief", "check", str(path)]) == 1
    assert "acceptance" in capsys.readouterr().out


def test_brief_digest_is_canonical():
    value = sample_brief()
    assert brief.digest(value) == brief.digest(dict(reversed(list(value.items()))))


@pytest.mark.parametrize(
    "value",
    [None, [], 1, "text", {"commands": True, "acceptance": [None], "scope": []}],
)
def test_malformed_brief_returns_errors(value):
    assert brief.validate(value)


def test_service_command_and_agent_amend_continue_read_briefs(tmp_path):
    from .test_execution_service_cli import git_repo, configs, cli
    from corral.execution.service import Service

    config = configs(tmp_path, git_repo(tmp_path / "workspace"))
    path = tmp_path / "task.json"
    value = sample_brief()
    path.write_text(json.dumps(value))
    status = cli(
        "corral.execution.service_command",
        "--config",
        config,
        "submit",
        "--repository",
        "demo",
        "--event-id",
        "brief-service",
        "--brief",
        path,
    )
    task = status["event"]["task_id"]
    changed = sample_brief("Check the copied output.")
    path.write_text(json.dumps(changed))
    cli(
        "corral.execution.agent_cli",
        "--config",
        config,
        "amend",
        "--event-id",
        "brief-service",
        "--amendment-id",
        "brief-amend",
        "--brief",
        path,
    )
    service = Service(config)
    assert service.controller.context(task)["brief"] == changed
    assert service.controller.context(task)["objective"] == changed["goal"]
    assert service.controller.run(service.token, task, execution_host="primary")[
        "result"
    ]["accepted"]
    newest = sample_brief("Check a later generation.")
    path.write_text(json.dumps(newest))
    continued = cli(
        "corral.execution.agent_cli",
        "--config",
        config,
        "continue",
        "--event-id",
        "brief-service",
        "--continuation-id",
        "brief-next",
        "--brief",
        path,
    )
    assert service.controller.context(task)["brief"] == newest
    assert continued["event"]["status"] == "prepared"
    assert service.controller.context(task)["objective"] == newest["goal"]


def test_python_agent_submit_accepts_a_brief_without_objective(tmp_path):
    from corral.execution.agent import AgentConfig, CorralAgent
    from corral.execution.store import Store
    from .test_execution_service_cli import configs, git_repo

    workspace = git_repo(tmp_path / "workspace")
    configs(tmp_path, workspace)
    agent = CorralAgent(
        AgentConfig(
            controller_config=tmp_path / "controller.json", development_mode=True
        )
    )
    value = sample_brief()
    task = agent.submit(workspace, brief=value)
    request = Store(tmp_path / "state/controller.sqlite").get("request", task)
    assert request["brief"] == value and request["objective"] == value["goal"]
    with pytest.raises(ValueError, match="objective or brief is required"):
        agent.submit(workspace)


@pytest.mark.parametrize(
    "module", ["corral.execution.agent_cli", "corral.execution.service_command"]
)
def test_null_brief_file_is_refused_even_with_an_objective(tmp_path, module):
    from .test_execution_service_cli import configs, git_repo, cli
    from corral.execution.store import Store

    config = configs(tmp_path, git_repo(tmp_path / "workspace"))
    path = tmp_path / "task.json"
    path.write_text("null")
    result = cli(
        module,
        "--config",
        config,
        "submit",
        "--repository",
        "demo",
        "--event-id",
        "null-brief",
        "--objective",
        "Copy input.",
        "--brief",
        path,
        check=False,
    )
    assert result.returncode != 0 and "brief must be object" in result.stderr
    assert Store(tmp_path / "state/controller.sqlite").records("request") == {}


def test_service_event_identity_includes_brief_context(tmp_path):
    from .test_execution_service_cli import configs, git_repo
    from corral.execution.service import Service

    service = Service(configs(tmp_path, git_repo(tmp_path / "workspace")))
    value = sample_brief()
    first = service.submit("brief-identity", "demo", brief=value)
    assert (
        service.submit("brief-identity", "demo", brief=value)["event"]["task_id"]
        == first["event"]["task_id"]
    )
    value = deepcopy(value)
    value["why"] = "Changed context with the same goal."
    with pytest.raises(
        ValueError, match="service event identity already has different content"
    ):
        service.submit("brief-identity", "demo", brief=value)
