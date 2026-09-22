"""Exact-file native runtime grants are proved without provider inference."""
from __future__ import annotations

import hashlib
import json

import pytest

from corral.execution import containment

from . import native_support as ns


pytestmark = pytest.mark.skipif(containment.sandbox_exec() is None,
                                reason="real worker containment requires macOS sandbox-exec")


def _completed_ops() -> list[str]:
    candidate = "def add(a, b):\n    return a + b\n"
    return [
        f"write math_ops.py {ns.b64(candidate)}",
        f"result {ns.b64(json.dumps({'changed': ['math_ops.py']}))}",
        "narrative Wrote the requested candidate change.",
    ]


def _run(tmp_path, files):
    env = ns.native_env(tmp_path)
    env["host"]["native_routes"][ns.FAKE_ROUTE]["runtime_write_files"] = [str(item) for item in files]
    task = env["controller"].submit("owner", "runtime-file-grant",
                                    ns.native_spec(env, ops=_completed_ops()))
    return env, task, env["controller"].run("owner", task, execution_host=ns.FAKE_HOST)


def test_existing_runtime_sidecar_allows_append_probe_without_mutating_contents(tmp_path):
    sidecar = tmp_path / "fake-harness-home" / "conversation_summaries.db"
    sidecar.parent.mkdir(parents=True)
    sidecar.write_bytes(b"immutable fixture bytes")
    before = hashlib.sha256(sidecar.read_bytes()).hexdigest()
    env, task, run = _run(tmp_path, [sidecar])
    assert run["result"]["accepted"] is True
    assert hashlib.sha256(sidecar.read_bytes()).hexdigest() == before
    boundary = json.loads((env["controller"].artifacts / task / "boundary.json").read_text())
    assert str(sidecar) in boundary["boundary"]["write_file_allow"]


def test_missing_runtime_sidecar_is_created_and_removed_by_the_probe(tmp_path):
    sidecar = tmp_path / "fake-harness-home" / "conversation_summaries.db-wal"
    sidecar.parent.mkdir(parents=True)
    _env, _task, run = _run(tmp_path, [sidecar])
    assert run["result"]["accepted"] is True
    assert not sidecar.exists()


def test_runtime_file_outside_read_grant_refuses_before_harness_launch(tmp_path):
    env = ns.native_env(tmp_path)
    sidecar = tmp_path / "publisher-state" / "session.db"
    sidecar.parent.mkdir()
    sidecar.write_text("fixture")
    env["host"]["native_routes"][ns.FAKE_ROUTE]["runtime_read"] = []
    env["host"]["native_routes"][ns.FAKE_ROUTE]["runtime_write_files"] = [str(sidecar)]
    task = env["controller"].submit("owner", "refuse-runtime-file", ns.native_spec(env, ops=[]))
    with pytest.raises(PermissionError, match="runtime writable files"):
        env["controller"].run("owner", task, execution_host=ns.FAKE_HOST)
    assert not (env["controller"].artifacts / task / "harness.stdout").exists()


def test_runtime_file_grant_refuses_native_auth_token_before_harness_launch(tmp_path):
    env = ns.native_env(tmp_path)
    token = env["token"]
    env["host"]["native_routes"][ns.FAKE_ROUTE]["runtime_write_files"] = [str(token)]
    task = env["controller"].submit("owner", "refuse-runtime-token", ns.native_spec(env, ops=[]))
    with pytest.raises(PermissionError, match="runtime writable files"):
        env["controller"].run("owner", task, execution_host=ns.FAKE_HOST)
    assert not (env["controller"].artifacts / task / "harness.stdout").exists()
