"""Native control plane: evidenced registry, route/model refusals, boundary fail-closed.

Every refusal here is exercised through the real controller or the real route module. No
adapter, process, command builder or sandbox call is monkeypatched, and no live provider is
contacted: the only harness is the explicitly synthetic fixture executable.
"""
from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path

import pytest

from corral.execution import containment, routes
from corral.execution.controller import Controller
from corral.execution.profiles import STANDARD_NATIVE_PROFILES

from . import native_support as ns

pytestmark = pytest.mark.skipif(containment.sandbox_exec() is None,
                                reason="real worker containment requires macOS sandbox-exec")

PLACEHOLDER_MODELS = {"deepseek-v4-flash", "claude", "qwen", "gpt", "gemini-3.1-pro", "fixture"}


def _route_env(tmp_path: Path, env: dict, **route_changes) -> Controller:
    """A second controller over the same fixture host with one route field changed."""
    host = json.loads(json.dumps(env["host"]))
    host["native_routes"][ns.FAKE_ROUTE].update(route_changes)
    return Controller(tmp_path / "state-route", "owner", {ns.FAKE_HOST: host},
                      default_host=ns.FAKE_HOST, profiles=[env["profile"]])


# --------------------------------------------------------------------------- registry hygiene

def test_registered_profiles_are_evidenced_and_contain_no_placeholders():
    """Only routes/models actually observed on this host may be registered."""
    ids = {profile.id for profile in STANDARD_NATIVE_PROFILES}
    assert ids == {"gemini-3.8-flash-high", "gemini-3.8-flash-medium",
                   "qwen3.8-max-high-codex-baba"}
    for profile in STANDARD_NATIVE_PROFILES:
        assert profile.model not in PLACEHOLDER_MODELS
        assert profile.context > 0, profile.id
        assert profile.version and profile.version != "0", profile.id
        assert profile.route.startswith("native-"), profile.id
        assert profile.provider and profile.account_ref, profile.id
        # Requested effort is bound to each profile, never defaulted at launch time.
        assert profile.effort in {"medium", "high"}, profile.id
    qwen = next(p for p in STANDARD_NATIVE_PROFILES if p.id == "qwen3.8-max-high-codex-baba")
    # The Qwen route is the existing Baba Token Plan binding: Codex harness, Qwen model.
    assert (qwen.harness, qwen.model, qwen.account_ref, qwen.provider) == \
        ("codex", "qwen3.8-max", "baba-token-plan", "alibaba")
    assert qwen.context == 258400 and qwen.version == "0.144.1"
    gemini = next(p for p in STANDARD_NATIVE_PROFILES if p.id == "gemini-3.8-flash-high")
    assert (gemini.harness, gemini.model, gemini.route, gemini.account_ref) == \
        ("agy", "gemini-3.8-flash-high", "native-antigravity-agy", "antigravity-signed-in")
    assert gemini.context == 235849 and gemini.version == "1.2.8"
    medium = next(p for p in STANDARD_NATIVE_PROFILES if p.id == "gemini-3.8-flash-medium")
    assert (medium.model, medium.effort, medium.version) == ("gemini-3.8-flash-medium", "medium", "1.2.8")


def test_native_coding_profiles_are_writers_not_read_only_reviewers():
    for profile in STANDARD_NATIVE_PROFILES:
        assert {"implementation", "repair"} <= set(profile.roles), profile.id
        assert "review" not in profile.roles, profile.id
        assert {"read", "search", "edit", "shell", "test"} <= set(profile.tools), profile.id


def test_route_contract_configures_waits_without_artificial_spend_or_turn_caps():
    fields = set(routes.NativeRoute.__dataclass_fields__)
    banned = {"max_turns", "max_retries", "retry_limit", "spend_cap", "budget", "token_budget",
              "turn_limit", "max_tokens"}
    assert not (fields & banned), sorted(fields & banned)
    assert "operational_wait_seconds" not in fields  # it belongs to the task, not the route
    assert not {item for item in routes.PLACEHOLDERS if "turn" in item or "retry" in item
                or "spend" in item or "budget" in item}
    # The wait is an explicit task-level knob that reaches the harness contract untouched.
    assert routes.PLACEHOLDERS == ("{workspace}", "{scratch}", "{prompt_file}", "{prompt}",
                                   "{model}", "{effort}", "{result_file}", "{schema_file}",
                                   "{log_file}", "{packet_file}")


# --------------------------------------------------------------------------- submit refusals

def test_route_refuses_provider_or_account_mismatch(tmp_path):
    env = ns.native_env(tmp_path)
    profile = ns.fake_profile()
    route = routes.declare(ns.FAKE_ROUTE, {**env["host"]["native_routes"][ns.FAKE_ROUTE],
                                           "provider": "other", "account_ref": "other-account"})
    with pytest.raises(PermissionError, match="provider/account"):
        routes.authorize(route, profile, host_routes=(ns.FAKE_ROUTE,))


def test_unsupported_model_for_the_declared_route_is_refused(tmp_path):
    env = ns.native_env(tmp_path, supported_models=("some-other-model",))
    spec = ns.native_spec(env, ops=[])
    with pytest.raises(PermissionError, match="does not serve model"):
        env["controller"].submit("owner", "refuse-model", spec)


def test_unsupported_effort_for_the_declared_model_is_refused(tmp_path):
    env = ns.native_env(tmp_path, supported_efforts=("low",))
    spec = ns.native_spec(env, ops=[])
    with pytest.raises(PermissionError, match="does not serve effort"):
        env["controller"].submit("owner", "refuse-effort", spec)


def test_route_not_listed_by_the_execution_host_is_refused(tmp_path):
    env = ns.native_env(tmp_path, extra_host={"routes": ["some-other-route"]})
    spec = ns.native_spec(env, ops=[])
    # Profile resolution refuses first: no registered profile is eligible on this host.
    with pytest.raises(PermissionError, match="no supported authorized profile"):
        env["controller"].submit("owner", "refuse-route", spec)
    # And the route module independently refuses an unlisted route id.
    route = routes.declared_routes(env["host"])[ns.FAKE_ROUTE]
    with pytest.raises(PermissionError, match="is not authorized for this execution host"):
        routes.authorize(route, env["profile"], host_routes=("some-other-route",))


def test_unregistered_profile_id_is_refused(tmp_path):
    env = ns.native_env(tmp_path)
    spec = ns.native_spec(env, ops=[])
    spec["selection"]["profile"]["id"] = "not-registered-by-the-controller"
    with pytest.raises(PermissionError, match="not registered by controller"):
        env["controller"].submit("owner", "refuse-unregistered", spec)


def test_tampered_profile_declaration_is_refused(tmp_path):
    env = ns.native_env(tmp_path)
    spec = ns.native_spec(env, ops=[])
    spec["selection"]["profile"]["context"] = 999999999
    with pytest.raises(PermissionError, match="does not match trusted registry"):
        env["controller"].submit("owner", "refuse-tampered", spec)


def test_native_profile_may_not_carry_an_explicit_worker_command(tmp_path):
    env = ns.native_env(tmp_path)
    spec = ns.native_spec(env, ops=[])
    spec["command"] = [sys.executable, "-c", "print('bypass the adapter')"]
    with pytest.raises(PermissionError, match="explicit command refused"):
        env["controller"].submit("owner", "refuse-command", spec)


def test_harness_mismatch_between_profile_and_route_is_refused(tmp_path):
    env = ns.native_env(tmp_path)
    route = routes.declared_routes(env["host"])[ns.FAKE_ROUTE]
    profile = dataclasses.replace(env["profile"], harness="some-other-harness")
    with pytest.raises(PermissionError, match="does not match route harness"):
        routes.authorize(route, profile, host_routes=(ns.FAKE_ROUTE,))


def test_live_route_without_explicit_launch_authorisation_is_refused(tmp_path):
    """Declared is not authorized: a non-synthetic route stays gated."""
    env = ns.native_env(tmp_path, synthetic=False, launch_authorized=False)
    spec = ns.native_spec(env, ops=[])
    with pytest.raises(PermissionError, match="live launch is not authorized"):
        env["controller"].submit("owner", "refuse-live", spec)
    # And the plan evidence separates the declaration from permission to launch now.
    route = routes.declared_routes(env["host"])[ns.FAKE_ROUTE]
    with pytest.raises(PermissionError):
        routes.plan(route, env["profile"], host_routes=(ns.FAKE_ROUTE,))
    authorized = routes.declare(ns.FAKE_ROUTE, {**env["host"]["native_routes"][ns.FAKE_ROUTE],
                                                "launch_authorized": True})
    plan = routes.plan(authorized, env["profile"], host_routes=(ns.FAKE_ROUTE,))
    assert plan.evidence["launch_authorized"] is True and plan.evidence["launch_permitted"] is True
    assert plan.evidence["synthetic"] is False


def test_route_declaration_gaps_are_refused(tmp_path):
    binary = tmp_path / "harness"
    binary.write_text("#!/bin/sh\n")
    base = {"harness": "h", "binary": str(binary), "supported_models": ["m"],
            "supported_efforts": ["high"]}
    with pytest.raises(PermissionError, match="requires an explicit binary"):
        routes.declare("r", {**base, "binary": ""})
    with pytest.raises(PermissionError, match="requires an explicit argv contract"):
        routes.declare("r", {**base, "argv": [], "envelope": "agy-json-v1"})
    with pytest.raises(PermissionError, match="unsupported envelope schema"):
        routes.declare("r", {**base, "argv": ["--model", "{model}"], "envelope": "made-up-v9"})
    with pytest.raises(PermissionError, match="unsupported placeholders"):
        routes.declare("r", {**base, "argv": ["{not_a_placeholder}"], "envelope": "agy-json-v1"})
    with pytest.raises(PermissionError, match="must pin the models it actually serves"):
        routes.declare("r", {**base, "supported_models": [], "argv": ["x"],
                             "envelope": "agy-json-v1"})
    with pytest.raises(PermissionError, match="must be a declaration object"):
        routes.declare("r", ["not", "a", "mapping"])


# --------------------------------------------------------------------------- launch refusals

def test_harness_binary_inside_worker_reach_is_refused_before_launch(tmp_path):
    env = ns.native_env(tmp_path)
    inside = env["workspace"] / "evil-harness"
    inside.write_text("#!/bin/sh\necho hijacked\n")
    inside.chmod(0o755)
    controller = _route_env(tmp_path, env, binary=str(inside))
    spec = ns.native_spec(env, ops=[])
    task = controller.submit("owner", "refuse-worker-binary", spec)
    with pytest.raises(PermissionError, match="must not live in a worker-writable path"):
        controller.run("owner", task, execution_host=ns.FAKE_HOST)
    assert not (controller.artifacts / task / "harness.stdout").exists()
    assert controller.store.ownership("workspace:" + str(env["workspace"].resolve()))[2] == "released"


def test_missing_harness_binary_is_refused(tmp_path):
    env = ns.native_env(tmp_path)
    controller = _route_env(tmp_path, env, binary=str(tmp_path / "absent-harness"))
    spec = ns.native_spec(env, ops=[])
    task = controller.submit("owner", "refuse-absent-binary", spec)
    with pytest.raises(PermissionError, match="binary is not a regular file"):
        controller.run("owner", task, execution_host=ns.FAKE_HOST)
    assert not (controller.artifacts / task / "harness.stdout").exists()
    # A relative name that is not on PATH is refused as not installed, never resolved loosely.
    uninstalled = routes.declare("r", {**env["host"]["native_routes"][ns.FAKE_ROUTE],
                                       "binary": "corral-absent-harness-xyz"})
    with pytest.raises(PermissionError, match="binary is not installed"):
        routes.resolve_binary(uninstalled)


def test_boundary_overlap_with_the_workspace_fails_closed_without_launching(tmp_path):
    """Denying the workspace itself is a configuration overlap, not a containment proof."""
    env = ns.native_env(tmp_path, extra_host={"protected_paths": [
        str(tmp_path / "host-secrets"), str(tmp_path / "fake-harness-home"),
        str(tmp_path / "workspace")]})
    spec = ns.native_spec(env, ops=[])
    controller = env["controller"]
    task = controller.submit("owner", "refuse-overlap", spec)
    with pytest.raises(PermissionError, match="overlaps trusted state"):
        controller.run("owner", task, execution_host=ns.FAKE_HOST)
    artifacts = controller.artifacts / task
    assert not (artifacts / "harness.stdout").exists()
    assert not (artifacts / "adapter-result.json").exists()
    # Nothing ran, so ownership is released and the refusal is recorded truthfully.
    assert controller.store.ownership("workspace:" + str(env["workspace"].resolve()))[2] == "released"
    assert controller.store.records("state")[task]["status"] == "refused-before-launch"


def test_route_runtime_log_write_is_probed_and_must_stay_inside_a_read_grant(tmp_path):
    env = ns.native_env(tmp_path)
    log_root = env["fake_home"] / "log"
    log_root.mkdir()
    controller = _route_env(tmp_path, env, runtime_read=[str(env["fake_home"])],
                            runtime_write=[str(log_root)])
    task = controller.submit("owner", "runtime-log-grant", ns.native_spec(env, ops=[]))
    controller.run("owner", task, execution_host=ns.FAKE_HOST)
    adapter = json.loads((controller.artifacts / task / "adapter-result.json").read_text())
    assert adapter["containment"]["passed"] is True
    receipt = json.loads((controller.artifacts / task / "boundary.json").read_text())
    assert str(log_root) in receipt["boundary"]["write_allow"]


def test_runtime_log_write_without_route_read_grant_fails_closed(tmp_path):
    env = ns.native_env(tmp_path)
    log_root = env["fake_home"] / "log"
    log_root.mkdir()
    controller = _route_env(tmp_path, env, runtime_read=[], runtime_write=[str(log_root)])
    task = controller.submit("owner", "refuse-runtime-log", ns.native_spec(env, ops=[]))
    with pytest.raises(PermissionError, match="overlaps trusted state"):
        controller.run("owner", task, execution_host=ns.FAKE_HOST)


def test_read_grant_that_reopens_more_than_one_denied_root_fails_closed(tmp_path):
    env = ns.native_env(tmp_path, runtime_read=[str(tmp_path / "fake-harness-home" / "auth")])
    controller = env["controller"]
    spec = ns.native_spec(env, ops=[])
    task = controller.submit("owner", "refuse-wide-grant", spec)
    with pytest.raises(PermissionError, match="overlaps trusted state"):
        controller.run("owner", task, execution_host=ns.FAKE_HOST)
    assert not (controller.artifacts / task / "harness.stdout").exists()


def test_refused_dispatch_never_leaves_a_live_worker_or_stranded_ownership(tmp_path):
    env = ns.native_env(tmp_path, supported_models=("some-other-model",))
    controller = env["controller"]
    spec = ns.native_spec(env, ops=[])
    with pytest.raises(PermissionError):
        controller.submit("owner", "refuse-before-ownership", spec)
    assert controller.store.ownership("workspace:" + str(env["workspace"].resolve())) is None
    assert controller.store.records("state") == {}


# --------------------------------------------------------------------------- sandbox on/off

def test_deterministic_dispatch_reports_honest_containment_when_sandbox_is_off(tmp_path):
    env = ns.native_env(tmp_path)
    controller = env["controller"]
    workspace = env["workspace"]
    spec = {"repo": "sandbox-off", "workspace": str(workspace), "candidate_paths": ["math_ops.py"],
            "verifier_paths": [], "verify": [sys.executable, "-c", "raise SystemExit(0)"],
            "command": [sys.executable, "-c", "print('deterministic')"]}
    task = controller.submit("owner", "sandbox-off", spec)
    run = controller.run("owner", task, execution_host=ns.FAKE_HOST)
    state = controller.store.get("state", task)
    # Off is reported as off: no boundary label is claimed for an unsandboxed child.
    assert state["containment"] == "standard-clean-env"
    assert run["result"]["accepted"] is True
    assert run["result"]["receipt"]["policy"]["kind"] == "inline"
    assert "containment" not in run["result"]["receipt"]


def test_deterministic_sandbox_flag_really_contains_the_child(tmp_path):
    env = ns.native_env(tmp_path)
    controller, workspace = env["controller"], env["workspace"]
    probe = env["secrets"] / "provider.key"
    child = (
        "import errno,json,os\n"
        "row={}\n"
        "for kind,mode in (('read','rb'),('write',None)):\n"
        "    try:\n"
        "        if mode: fd=os.open(%r,os.O_WRONLY|os.O_APPEND);os.close(fd)\n"
        "        else:\n"
        "            with open(%r,'rb') as h: h.read(1)\n"
        "        row[kind]='allowed'\n"
        "    except OSError as e: row[kind]=errno.errorcode.get(e.errno,str(e.errno))\n"
        "open('probe.json','w').write(json.dumps(row))\n"
    ) % (str(probe), str(probe))
    spec = {"repo": "sandbox-on", "workspace": str(workspace), "candidate_paths": ["math_ops.py"],
            "verifier_paths": [], "verify": [sys.executable, "-c", "raise SystemExit(0)"],
            "command": [sys.executable, "-c", child], "use_sandbox": True}
    task = controller.submit("owner", "sandbox-on", spec)
    controller.run("owner", task, execution_host=ns.FAKE_HOST)
    assert controller.store.get("state", task)["containment"] == "seatbelt-worker-boundary"
    assert json.loads((workspace / "probe.json").read_text()) == {"read": "EPERM", "write": "EPERM"}
    assert probe.read_text() == "SYNTHETIC-DISPOSABLE-SENTINEL\n"


def test_native_worker_cannot_touch_secret_named_files_in_its_own_workspace(tmp_path):
    """Regex denials hold inside the granted workspace; templates stay usable."""
    env = ns.native_env(tmp_path)
    workspace, controller = env["workspace"], env["controller"]
    (workspace / ".env").write_text("SYNTHETIC_DISPOSABLE=1\n")
    (workspace / "provider.key").write_text("SYNTHETIC-DISPOSABLE-KEY\n")
    (workspace / ".env.example").write_text("SYNTHETIC_TEMPLATE=1\n")
    probes = [str(workspace / ".env"), str(workspace / "provider.key"),
              str(workspace / ".env.example")]
    ops = [f"deny-probe {item}" for item in probes]
    spec = ns.native_spec(env, ops=ops)
    task = controller.submit("owner", "secret-names", spec)
    run = controller.run("owner", task, execution_host=ns.FAKE_HOST)
    observed = ns.adapter_result(run)["detail"]["harness_detail"]["probes"]
    assert observed[str(workspace / ".env")] == {"read": "EPERM", "write": "EPERM"}
    assert observed[str(workspace / "provider.key")] == {"read": "EPERM", "write": "EPERM"}
    # A conventional template is not a secret and must stay readable by a coding worker.
    assert observed[str(workspace / ".env.example")]["read"] == "allowed"
    assert (workspace / ".env").read_text() == "SYNTHETIC_DISPOSABLE=1\n"
    assert run["result"]["receipt"]["policy_ok"] is True


def test_native_worker_cannot_read_the_controller_source_tree_or_artifacts(tmp_path):
    env = ns.native_env(tmp_path)
    controller, workspace = env["controller"], env["workspace"]
    artifacts = controller.artifacts
    source_file = Path(containment.__file__)
    spec = ns.native_spec(env, ops=[
        f"deny-probe {source_file}",
        f"deny-probe {controller.store.path}",
        f"deny-probe {env['verifiers'] / 'check_candidate.py'}",
    ])
    task = controller.submit("owner", "protected-reads", spec)
    run = controller.run("owner", task, execution_host=ns.FAKE_HOST)
    observed = ns.adapter_result(run)["detail"]["harness_detail"]["probes"]
    # The controller source is denied even though it is the adapter's own package.
    assert observed[str(source_file)] == {"read": "EPERM", "write": "EPERM"}
    assert observed[str(controller.store.path)] == {"read": "EPERM", "write": "EPERM"}
    assert observed[str(env["verifiers"] / "check_candidate.py")] == \
        {"read": "EPERM", "write": "EPERM"}
    assert run["result"]["accepted"] is False  # no candidate was written by these probe ops
    assert (workspace / "math_ops.py").read_text() == "def add(a, b):\n    return 0\n"
    assert artifacts.is_dir()
