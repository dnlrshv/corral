"""Offline full-path native proof: submit -> run -> trusted adapter -> sandboxed harness.

Nothing in this path is monkeypatched. The harness is a real executable named by a
host-owned route declaration, the adapter command is built by the production builder and
executed from a foreign cwd with an isolated interpreter, the worker really runs under an
ephemeral Seatbelt profile, and the verifier really executes from a controller-owned root.
Every model/usage artifact is explicitly synthetic fixture evidence.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from corral.execution import containment, native
from corral.execution.adapter import build_adapter_command, source_root

from . import native_support as ns

pytestmark = pytest.mark.skipif(containment.sandbox_exec() is None,
                                reason="real worker containment requires macOS sandbox-exec")

FIXED_CANDIDATE = "def add(a, b):\n    return a + b\n"


def _success_ops() -> list[str]:
    return [
        f"write math_ops.py {ns.b64(FIXED_CANDIDATE)}",
        f"result {ns.b64(json.dumps({'answer': 5, 'changed': ['math_ops.py']}))}",
        "narrative Implemented add() and re-read the contract before finishing.",
        f"usage {json.dumps({'input_tokens': 150, 'output_tokens': 20, 'thinking_tokens': 8, 'cache_read_tokens': 40, 'total_tokens': 170})}",
        "usage-mode cumulative",
    ]


def test_full_path_native_success_is_real_code_change_with_separate_structured_result(tmp_path):
    env = ns.native_env(tmp_path)
    controller, workspace = env["controller"], env["workspace"]
    spec = ns.native_spec(env, ops=_success_ops())
    task = controller.submit("owner", "native-full-path", spec)
    run = controller.run("owner", task, execution_host=ns.FAKE_HOST)
    result = run["result"]

    # 1. Real code change produced by a process that was really inside the sandbox.
    assert (workspace / "math_ops.py").read_text() == FIXED_CANDIDATE
    assert result["accepted"] is True

    # 2. Structured completion is a separate artifact from the narrative, and the adapter
    #    recorded where it came from.
    assert result["structured"] == {"answer": 5, "changed": ["math_ops.py"]}
    adapter = ns.adapter_result(run)
    assert adapter["status"] == "completed"
    assert adapter["narrative"].startswith("Implemented add()")
    assert adapter["structured"] == result["structured"]
    assert adapter["detail"]["structured_source"] == "result_file"
    assert (Path(result["artifact_directory"]) / "structured.json").is_file()

    # 3. Usage stays attributed to its synthetic origin: measured counters are not invented
    #    and nothing is reported as zero.
    usage = result["usage"]
    assert usage["synthetic_fields"] == {"input_tokens": 150, "output_tokens": 20,
                                         "thinking_tokens": 8, "cache_read_tokens": 40,
                                         "total_tokens": 170}
    assert usage["measured_fields"] == {}
    assert usage["estimated_fields"] == {}
    assert usage["origin_breakdown"]["synthetic-fixture"] == 1
    assert usage["account_total"] is None and usage["combined_tokens"] is None

    # 4. Trusted external verifier: controller-owned root, bound bundle, candidate digests.
    receipt = result["receipt"]
    assert receipt["policy"]["kind"] == "external"
    assert receipt["policy_ok"] is True and receipt["exit_code"] == 0
    assert receipt["unchanged"] is True
    assert receipt["candidate_pre"] == receipt["candidate_post"]
    assert receipt["verifier_bundle"]["check_candidate.py"]
    assert receipt["native"]["route"] == ns.FAKE_ROUTE

    # 5. Containment was demonstrated by a real probe, with honest scope limits.
    proven = receipt["containment"]
    assert proven["passed"] is True
    denials = [check for check in proven["checks"] if check["expected"] == "denied"]
    assert denials and all(check["errno"] == "EPERM" for check in denials)
    assert proven["not_contained"] and proven["isolation_claim"]

    # 6. Observed identity comes from harness-reported fields only.
    assert result["observed"] == {"model": ns.FAKE_MODEL, "effort": ns.FAKE_EFFORT,
                                  "harness": ns.FAKE_HARNESS, "provider": "fixture",
                                  "account_ref": "fixture-account", "version": "0.0.0-fixture"}
    assert result["identity_mismatch"] == []

    # 7. Ownership was released and the attempt history is retained.
    owner, _epoch, status = controller.store.ownership("workspace:" + str(workspace.resolve()))
    assert (owner, status) == (task, "released")
    attempts = ns.attempts(run)
    assert len(attempts) == 1 and attempts[0]["status"] == "completed"
    assert attempts[0]["usage_events"] == 1

    # 8. The adapter command is the production-built one and runs outside the workspace.
    evidence = result["native"]
    expected_command = build_adapter_command(task_dir=Path(result["artifact_directory"]),
                                             workspace=workspace, source=source_root())
    assert evidence["adapter_command"] == expected_command
    assert Path(evidence["adapter_cwd"]).resolve() == Path(result["artifact_directory"]).resolve()
    assert Path(evidence["adapter_cwd"]).resolve() != workspace.resolve()
    assert evidence["synthetic"] is True
    assert evidence["launch_authorized"] is False and evidence["launch_permitted"] is True
    assert [str(Path(item).resolve()) for item in evidence["auth_read_granted"]] == [
        str(env["fake_home"].resolve())]

    # 9. The harness ran with the route's fake credential home, never the real account.
    harness_detail = adapter["detail"]["harness_detail"]
    assert harness_detail["home"] == str(env["fake_home"])
    assert harness_detail["cwd"] == str(workspace.resolve())


def test_sandboxed_worker_cannot_forge_receipts_or_read_protected_state(tmp_path):
    env = ns.native_env(tmp_path)
    controller, workspace, state = env["controller"], env["workspace"], env["state"]
    spec = ns.native_spec(env, ops=_success_ops())
    task = controller.submit("owner", "native-hostile-probes", spec)

    # A same-task amendment carries the hostile probe set, so continuation history and the
    # in-flight boundary proof are exercised in one real dispatch.
    artifacts = controller.artifacts / task
    # Every EPERM target must already exist when the harness runs. Seatbelt answers a probe
    # of a *nonexistent* path with ENOENT from the VFS before the read-data/write check, and
    # ENOENT is not containment evidence -- see the explicit absent-path assertion below.
    boundary_record = artifacts / "boundary.json"
    probes = [
        str(state / "controller.sqlite"),
        str(boundary_record),
        str(state / "scratch" / f"{task}-sibling{native.SENTINEL_NAME}"),
        str(env["secrets"] / "provider.key"),
        str(env["token"]),
        str(env["verifiers"] / "check_candidate.py"),
    ]
    absent = str(artifacts / "adapter-result.json")
    assert not Path(absent).exists()
    ops = _success_ops() + [f"deny-probe {item}" for item in probes]
    ops.append(f"deny-probe {absent}")
    ops.append(f"forge-receipt {boundary_record}")
    controller.steer("owner", task, "amend-1", {"objective": ns.write_ops(*ops)})
    run = controller.run("owner", task, execution_host=ns.FAKE_HOST)
    result = run["result"]
    detail = ns.adapter_result(run)["detail"]["harness_detail"]

    assert result["accepted"] is True
    observed = detail["probes"]
    for protected in (str(state / "controller.sqlite"), str(boundary_record),
                      str(state / "scratch" / f"{task}-sibling{native.SENTINEL_NAME}"),
                      str(env["secrets"] / "provider.key"),
                      str(env["verifiers"] / "check_candidate.py")):
        assert observed[protected] == {"read": "EPERM", "write": "EPERM"}, protected
    # The route's own credential home is granted read-only by explicit host configuration.
    assert observed[str(env["token"])] == {"read": "allowed", "write": "EPERM"}
    # An absent path answers ENOENT, which proves nothing about containment; it is recorded
    # here only so the distinction is asserted rather than silently accepted as a denial.
    assert observed[absent] == {"read": "ENOENT", "write": "ENOENT"}
    # The forge attempt was refused by the kernel, and the trusted record is the real one.
    assert detail["forged_receipt"]["target"] == str(boundary_record)
    assert detail["forged_receipt"]["overwrite"] == "EPERM"
    persisted = json.loads(boundary_record.read_text())
    assert "forged" not in persisted and persisted["containment_scope"]["not_contained"]
    # The trusted adapter, running outside the worker boundary, still wrote its own result.
    assert "forged" not in json.loads((artifacts / "adapter-result.json").read_text())

    # Continuation: the amendment is part of the prompt history for the same task identity.
    prompt = (state / "scratch" / task / "prompt.md").read_text()
    assert "Prior amendments for this same task identity" in prompt
    assert "amend-1" in prompt
    assert (workspace / "math_ops.py").read_text() == FIXED_CANDIDATE


def test_repeat_run_does_not_dispatch_a_second_native_attempt(tmp_path):
    env = ns.native_env(tmp_path)
    controller = env["controller"]
    spec = ns.native_spec(env, ops=_success_ops())
    task = controller.submit("owner", "native-no-duplicate", spec)
    first = controller.run("owner", task, execution_host=ns.FAKE_HOST)
    second = controller.run("owner", task, execution_host=ns.FAKE_HOST)
    assert first["result"]["accepted"] is True
    assert second["result"] == first["result"]
    assert len(ns.attempts(second)) == 1
    assert len(controller.store.records("claim")) == 1
