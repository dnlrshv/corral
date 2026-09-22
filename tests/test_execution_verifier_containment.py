"""Real native verifier boundary: candidate tests keep workspace access, not publisher state."""
from __future__ import annotations

import sys

import pytest

from corral.execution import containment

from . import native_support as ns

pytestmark = pytest.mark.skipif(containment.sandbox_exec() is None,
                                reason="real verifier containment requires macOS sandbox-exec")


def test_native_verifier_runs_candidate_check_but_cannot_read_publisher_sentinel(tmp_path):
    env = ns.native_env(tmp_path)
    publisher = tmp_path / "publisher-credentials"
    publisher.mkdir()
    sentinel = publisher / "publish.token"
    sentinel.write_text("DISPOSABLE-PUBLISHER-SENTINEL\n")
    env["host"]["protected_paths"].append(str(publisher))
    env["host"]["verifier_probe_sentinels"] = [str(sentinel)]
    verifier = env["verifiers"] / "verify_candidate.py"
    verifier.write_text(
        "import errno, sys\n"
        "from pathlib import Path\n"
        f"publisher = Path({str(sentinel)!r})\n"
        "try:\n    publisher.read_bytes()\nexcept OSError as error:\n"
        "    assert error.errno in (errno.EPERM, errno.EACCES), error\n"
        "else:\n    raise AssertionError('publisher credential was readable')\n"
        "namespace = {}\nsource = Path(sys.argv[1]).read_text()\n"
        "exec(compile(source, sys.argv[1], 'exec'), namespace)\n"
        "assert namespace['add'](2, 3) == 5\n")
    candidate = "def add(a, b):\n    return a + b\n"
    structured = '{"answer": 5, "changed": ["math_ops.py"]}'
    task = env["controller"].submit(
        "owner", "native-verifier-publisher-boundary",
        ns.native_spec(env, ops=[
            f"write math_ops.py {ns.b64(candidate)}",
            f"result {ns.b64(structured)}",
            "narrative Implemented add().",
            "usage {\"input_tokens\": 1, \"output_tokens\": 1, \"total_tokens\": 2}",
        ], verify=[sys.executable, str(verifier), "math_ops.py"]),
    )
    run = env["controller"].run("owner", task, execution_host=ns.FAKE_HOST)
    receipt = run["result"]["receipt"]
    assert run["result"]["accepted"] is True
    proof = receipt["verifier_containment"]
    assert proof["passed"] is True
    assert str(sentinel.resolve()) in proof["sentinels"]
    checks = {item["check"]: item for item in proof["checks"]}
    assert checks[f"read-file:{sentinel.resolve()}"]["errno"] == "EPERM"
    assert checks[f"write-file:{sentinel.resolve()}"]["errno"] == "EPERM"
