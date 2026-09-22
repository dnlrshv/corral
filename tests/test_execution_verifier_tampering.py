import subprocess
import sys
import pytest

from corral.execution.controller import Controller
from corral.execution.demo import fixture_profiles


@pytest.fixture
def test_env(tmp_path):
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=f@example.invalid",
                    "commit", "--allow-empty", "-qm", "fixture"], cwd=repo, check=True)
    controller = Controller(tmp_path / "state", "owner", {"fixture": {"routes": ["fixture"],
        "harnesses": ["synthetic"], "cpu": 4, "memory_mb": 1024}}, default_host="fixture", profiles=fixture_profiles())
    return controller, repo


def test_submit_rejects_overlapping_candidate_and_verifier(test_env):
    controller, repo = test_env
    spec = {
        "repo": "test",
        "workspace": str(repo),
        "candidate_paths": ["result.json", "tests/test_foo.py"],
        "verifier_paths": ["tests/test_foo.py"],
        "command": [sys.executable, "-c", "open('result.json','w').write('{\"ok\":true}')"],
        "verify": [sys.executable, "-c", "assert True"],
    }
    with pytest.raises(PermissionError, match="candidate_paths must not overlap with verifier_paths"):
        controller.submit("owner", "overlap", spec)


def test_worker_tampering_with_verifier_is_rejected(test_env):
    controller, repo = test_env
    verifier_script = repo / "verify_solution.py"
    verifier_script.write_text("import sys\nassert False, 'original test fails'\n")

    # Worker attempts to sabotage the test so it passes
    worker_code = """
import pathlib
pathlib.Path('verify_solution.py').write_text('import sys\\nsys.exit(0)\\n')
pathlib.Path('result.json').write_text('{"ok":true}')
"""
    spec = {
        "repo": "test",
        "workspace": str(repo),
        "candidate_paths": ["result.json"],
        "verifier_paths": ["verify_solution.py"],
        "command": [sys.executable, "-c", worker_code],
        "verify": [sys.executable, "verify_solution.py"],
    }
    task = controller.submit("owner", "tamper", spec)
    run_result = controller.run("owner", task, execution_host="fixture")
    result = run_result["result"]
    receipt = result["receipt"]

    assert receipt["verifier_intact"] is False
    assert result["accepted"] is False


def test_clean_worker_preserves_intact_verifier_and_accepts(test_env):
    controller, repo = test_env
    verifier_script = repo / "verify_solution.py"
    verifier_script.write_text("import json, pathlib\ndata = json.loads(pathlib.Path('result.json').read_text())\nassert data == {'ok': True}\n")

    # Worker writes valid result and does not touch verifier
    worker_code = """
import pathlib
pathlib.Path('result.json').write_text('{"ok":true}')
"""
    spec = {
        "repo": "test",
        "workspace": str(repo),
        "candidate_paths": ["result.json"],
        "verifier_paths": ["verify_solution.py"],
        "command": [sys.executable, "-c", worker_code],
        "verify": [sys.executable, "verify_solution.py"],
    }
    task = controller.submit("owner", "clean", spec)
    run_result = controller.run("owner", task, execution_host="fixture")
    result = run_result["result"]
    receipt = result["receipt"]

    assert receipt["verifier_intact"] is True
    assert receipt["unchanged"] is True
    assert result["accepted"] is True


def test_verifier_mutating_candidate_is_rejected(test_env):
    controller, repo = test_env
    # The verifier script inappropriately mutates candidate file during execution
    verifier_code = """
import pathlib
pathlib.Path('result.json').write_text('{"mutated":true}')
"""
    worker_code = """
import pathlib
pathlib.Path('result.json').write_text('{"ok":true}')
"""
    spec = {
        "repo": "test",
        "workspace": str(repo),
        "candidate_paths": ["result.json"],
        "command": [sys.executable, "-c", worker_code],
        "verify": [sys.executable, "-c", verifier_code],
    }
    task = controller.submit("owner", "candidate-mutation", spec)
    run_result = controller.run("owner", task, execution_host="fixture")
    result = run_result["result"]
    receipt = result["receipt"]

    assert receipt["unchanged"] is False
    assert result["accepted"] is False
