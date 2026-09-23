"""Native test verifiers get no network unless the host opts in; coding workers keep egress."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from corral.execution import containment, verifier_containment
from corral.execution.containment import Boundary, build_profile

from . import native_support as ns

needs_sandbox = pytest.mark.skipif(containment.sandbox_exec() is None,
                                   reason="real verifier containment requires macOS sandbox-exec")

NETWORK_CHECK = f"network-connect:{containment.NETWORK_PROBE_TARGET}"


def _boundary(tmp_path: Path, **changes) -> Boundary:
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "state" / "scratch" / "task-1"
    for item in (workspace, scratch):
        item.mkdir(parents=True, exist_ok=True)
    return Boundary(workspace=str(workspace), scratch=str(scratch), tmpdir=str(scratch),
                    deny=(), **changes)


def _verifier(tmp_path: Path, **changes) -> verifier_containment.Prepared:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    # As in a real run, the verifier receipt directory lives inside the denied artifacts.
    artifacts = tmp_path / "artifacts"
    return verifier_containment.prepare(workspace=workspace, state_dir=tmp_path / "state",
                                        artifacts=artifacts, task_dir=artifacts / "task-1",
                                        task_id="task-1", attempt="attempt-1", **changes)


# --------------------------------------------------------------------------- profile text

def test_a_boundary_without_network_renders_an_explicit_network_denial(tmp_path):
    profile = build_profile(_boundary(tmp_path, network=False))
    assert "(deny default)" in profile
    assert "(deny network*)" in profile
    assert "(allow network" not in profile


def test_a_coding_worker_boundary_keeps_network_egress_by_default(tmp_path):
    boundary = _boundary(tmp_path)
    assert boundary.network is True
    profile = build_profile(boundary)
    assert "(allow network*)" in profile
    assert "(deny network*)" not in profile


def test_scope_states_network_as_contained_only_when_the_boundary_denies_it(tmp_path):
    contained, not_contained = containment.scope(_boundary(tmp_path, network=False))
    assert contained == ["file-read*", "file-write*", "network*"]
    assert not any("network egress" in item for item in not_contained)
    assert any("same-UID" in item for item in not_contained)
    contained, not_contained = containment.scope(_boundary(tmp_path))
    assert contained == ["file-read*", "file-write*"]
    assert containment.NETWORK_NOT_CONTAINED in not_contained


def test_verifier_network_is_denied_unless_the_host_opts_in():
    assert verifier_containment.network_opt_in(None) is False
    assert verifier_containment.network_opt_in({}) is False
    assert verifier_containment.network_opt_in({"verifier_network": False}) is False
    assert verifier_containment.network_opt_in({"verifier_network": True}) is True


@pytest.mark.parametrize("value", ["true", "yes", 1, 0, None, ["true"]])
def test_verifier_network_opt_in_must_be_a_boolean(value):
    with pytest.raises(ValueError, match="verifier_network must be true or false"):
        verifier_containment.network_opt_in({"verifier_network": value})


# --------------------------------------------------------------------------- real probe

@needs_sandbox
def test_verifier_boundary_denies_network_by_default_and_the_probe_proves_it(tmp_path):
    prepared = _verifier(tmp_path)
    assert prepared.boundary.network is False
    assert "(deny network*)" in prepared.profile and "(allow network" not in prepared.profile
    evidence = prepared.evidence
    assert evidence["passed"] is True and evidence["network"] is False
    assert "network*" in evidence["contained_operations"]
    assert not any("network egress" in item for item in evidence["not_contained"])
    checks = {item["check"]: item for item in evidence["checks"]}
    assert checks[NETWORK_CHECK]["expected"] == "denied"
    assert checks[NETWORK_CHECK]["observed"] == "denied"
    assert checks[NETWORK_CHECK]["errno"] in ("EPERM", "EACCES")


@needs_sandbox
def test_verifier_boundary_with_the_host_opt_in_allows_network(tmp_path):
    prepared = _verifier(tmp_path, network=True)
    assert prepared.boundary.network is True
    assert "(allow network*)" in prepared.profile and "(deny network*)" not in prepared.profile
    evidence = prepared.evidence
    assert evidence["passed"] is True and evidence["network"] is True
    assert "network*" not in evidence["contained_operations"]
    assert containment.NETWORK_NOT_CONTAINED in evidence["not_contained"]
    assert NETWORK_CHECK not in {item["check"] for item in evidence["checks"]}


@needs_sandbox
def test_probe_fails_closed_when_a_claimed_network_denial_is_not_enforced(tmp_path, monkeypatch):
    # The boundary claims no network, but the rendered profile allows it: the probe observes
    # the real kernel result instead of trusting the configuration.
    rendered = containment.build_profile
    monkeypatch.setattr(containment, "build_profile",
                        lambda boundary: rendered(boundary).replace("(deny network*)", "(allow network*)"))
    receipt = containment.demonstrate(_boundary(tmp_path, network=False))
    assert receipt["passed"] is False
    assert NETWORK_CHECK in receipt["blocker"]
    observed = {item["check"]: item for item in receipt["checks"]}
    assert observed[NETWORK_CHECK]["observed"] != "denied"


# --------------------------------------------------------------------------- end to end

# A loopback round trip needs no listener outside the verifier and sends nothing off the host.
_LOOPBACK_VERIFIER = (
    "import errno, socket, sys\n"
    "expect = %r\n"
    "try:\n"
    "    with socket.socket() as server:\n"
    "        server.bind(('127.0.0.1', 0))\n"
    "        server.listen(1)\n"
    "        with socket.create_connection(server.getsockname(), timeout=5):\n"
    "            pass\n"
    "    seen = 'allowed'\n"
    "except OSError as error:\n"
    "    seen = errno.errorcode.get(error.errno, str(error.errno))\n"
    "sys.exit(0 if seen == expect else 1)\n"
)


def _run_with_loopback_verifier(tmp_path: Path, expect: str, **extra_host) -> dict:
    env = ns.native_env(tmp_path, extra_host=extra_host or None)
    candidate = "def add(a, b):\n    return a + b\n"
    structured = '{"answer": 5, "changed": ["math_ops.py"]}'
    task = env["controller"].submit(
        "owner", f"verifier-network-{expect}",
        ns.native_spec(env, ops=[
            f"write math_ops.py {ns.b64(candidate)}",
            f"result {ns.b64(structured)}",
            "narrative Implemented add().",
            "usage {\"input_tokens\": 1, \"output_tokens\": 1, \"total_tokens\": 2}",
        ], verify=[sys.executable, "-I", "-c", _LOOPBACK_VERIFIER % expect]),
    )
    return env["controller"].run("owner", task, execution_host=ns.FAKE_HOST)


@needs_sandbox
def test_native_candidate_tests_get_no_network_by_default(tmp_path):
    run = _run_with_loopback_verifier(tmp_path, "EPERM")
    receipt = run["result"]["receipt"]
    assert receipt["exit_code"] == 0, receipt
    assert run["result"]["accepted"] is True
    proof = receipt["verifier_containment"]
    assert proof["network"] is False
    assert {item["check"]: item["errno"] for item in proof["checks"]}[NETWORK_CHECK] == "EPERM"
    # The coding worker's boundary is unchanged: it still reaches its provider.
    assert run["result"]["native"]["containment"]["not_contained"].count(
        containment.NETWORK_NOT_CONTAINED) == 1


@needs_sandbox
def test_native_candidate_tests_get_network_with_the_host_opt_in(tmp_path):
    run = _run_with_loopback_verifier(tmp_path, "allowed", verifier_network=True)
    receipt = run["result"]["receipt"]
    assert receipt["exit_code"] == 0, receipt
    assert run["result"]["accepted"] is True
    assert receipt["verifier_containment"]["network"] is True


@needs_sandbox
def test_a_malformed_verifier_network_value_refuses_before_launch(tmp_path):
    env = ns.native_env(tmp_path, extra_host={"verifier_network": "yes"})
    task = env["controller"].submit("owner", "verifier-network-malformed", ns.native_spec(env, ops=[]))
    with pytest.raises(ValueError, match="verifier_network"):
        env["controller"].run("owner", task, execution_host=ns.FAKE_HOST)
    assert env["controller"].store.get("state", task)["status"] == "refused-before-launch"
