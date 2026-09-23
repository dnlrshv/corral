"""Tests for safe-boundary active-work amendments (W3-03).

Verifies:
- Instruction received during active work is acknowledged and applied at a safe boundary.
- Old-version evidence never falsely closes the amended objective.
- Current attempt finishes against its old version with prior objective acceptance preserved.
- New objective clearly remains pending.
- Clean continuation generation carries checkpoint and satisfies the amended objective under the same task ID.
"""
from __future__ import annotations

import sys
import time
import threading

import pytest

from corral.execution.controller import Controller
from corral.execution.demo import fixture_profiles
from tests.test_execution_wave import make_repo


@pytest.fixture
def amendment_env(tmp_path):
    repo = make_repo(tmp_path / "repo")
    state_dir = tmp_path / "controller_state"
    controller = Controller(
        state_dir, "owner",
        {"fixture": {"routes": ["fixture"], "harnesses": ["synthetic"], "cpu": 4, "memory_mb": 1024}},
        default_host="fixture", profiles=fixture_profiles(),
    )
    return controller, repo, state_dir


def test_active_work_amendment_does_not_falsely_close_new_objective(amendment_env):
    """W3-03: Useful active-work amendment applied at safe boundary; old evidence never falsely closes new objective."""
    controller, repo, _ = amendment_env

    # Worker script reads dynamic payload from context to simulate real worker behavior
    worker_script = (
        "import os, json, time\n"
        "time.sleep(0.1)\n"
        "ctx_path = os.environ['CORRAL_CONTEXT_PATH']\n"
        "with open(ctx_path) as f:\n"
        "    ctx = json.load(f)\n"
        "gen = ctx.get('generation', 1)\n"
        "with open('.gen', 'w') as f:\n"
        "    f.write(str(gen))\n"
        "with open('result.json', 'w') as f:\n"
        "    f.write('{\"ok\": true}\\n')\n"
        "with open('duration.py', 'w') as f:\n"
        "    if gen == 2:\n"
        "        f.write('def parse(s):\\n' \n"
        "                '    total = 0\\n' \n"
        "                '    for part in s.split():\\n' \n"
        "                '        if part.endswith(\"h\"): total += int(part[:-1]) * 60\\n' \n"
        "                '        elif part.endswith(\"m\"): total += int(part[:-1])\\n' \n"
        "                '    return total\\n')\n"
        "    else:\n"
        "        f.write('def parse(s):\\n    return int(s.replace(\"m\", \"\"))\\n')\n"
    )

    # Verifier script reads the generation from .gen
    verify_script = (
        "import sys, os\n"
        "sys.path.insert(0, '')\n"
        "import duration\n"
        "with open('.gen') as f:\n"
        "    gen = int(f.read().strip())\n"
        "if gen == 2:\n"
        "    assert duration.parse('1h 30m') == 90\n"
        "    assert duration.parse('45m') == 45\n"
        "else:\n"
        "    assert duration.parse('45m') == 45\n"
    )

    spec = {
        "repo": "test-repo",
        "workspace": str(repo),
        "objective": "Support minutes format (e.g. 45m)",
        "candidate_paths": ["duration.py", "result.json"],
        "command": [sys.executable, "-c", worker_script],
        "verify": [sys.executable, "-c", verify_script],
        "result_file": "result.json",
    }

    task_id = controller.submit("owner", "req-amend-1", spec)

    def steer_during_run():
        time.sleep(0.05)
        controller.steer("owner", task_id, "amendment-hours", {
            "objective": "Support minutes AND hours format (e.g. 1h 30m)",
        })

    t = threading.Thread(target=steer_during_run)
    t.start()

    # Run the initial dispatch (which was launched against objective 1)
    run_res = controller.run("owner", task_id, execution_host="fixture")
    t.join()
    result = run_res["result"]

    # CRITICAL: Old-version evidence was accepted against ITS objective, but an amendment is pending
    assert result["accepted"] is True
    assert result["amendment_pending"] is True
    assert result["dispatch_objective"] == "Support minutes format (e.g. 45m)"
    assert result["current_objective"] == "Support minutes AND hours format (e.g. 1h 30m)"

    state = controller.store.get("state", task_id)
    assert state["status"] == "completed"
    assert state["amended_objective_pending"] is True

    # The controller MUST automatically schedule a continuation generation
    lineage = controller.status("owner", task_id)["lineage"]
    assert lineage["pending_generation"] == 2
    assert lineage["scheduled_generation"] == 2

    # Dispatch generation 2 automatically scheduled task
    gen2_run = controller.run("owner", task_id, execution_host="fixture")
    assert gen2_run["result"]["accepted"] is True
    assert gen2_run["result"]["generation"] == 2

    status = controller.status("owner", task_id)
    lineage = status["lineage"]
    assert lineage["current_generation"] == 2
    assert len(lineage["generations"]) == 2
    assert lineage["generations"][0]["accepted"] is True
    assert lineage["generations"][1]["accepted"] is True


def test_amendment_boundary_exiting_race(amendment_env):
    """W3-03: Meaningful boundary race coverage. Amendment arrives right as the worker is exiting."""
    controller, repo, _ = amendment_env

    # We want the amendment to arrive EXACTLY when the process finishes, but before verification completes.
    worker_script = (
        "import os, json\n"
        "with open('result.json', 'w') as f:\n"
        "    f.write('{\"ok\": true}\\n')\n"
        "with open('duration.py', 'w') as f:\n"
        "    f.write('def parse(s):\\n    return int(s.replace(\"m\", \"\"))\\n')\n"
    )

    verify_script = (
        "import sys, os, json, time\n"
        "sys.path.insert(0, '')\n"
        "import duration\n"
        "assert duration.parse('45m') == 45\n"
        "# Delay verification slightly to allow steering thread to inject amendment\n"
        "time.sleep(0.2)\n"
    )

    spec = {
        "repo": "test-repo",
        "workspace": str(repo),
        "objective": "Support minutes format (e.g. 45m)",
        "candidate_paths": ["duration.py", "result.json"],
        "command": [sys.executable, "-c", worker_script],
        "verify": [sys.executable, "-c", verify_script],
        "result_file": "result.json",
    }

    task_id = controller.submit("owner", "req-amend-2", spec)

    # Worker executes fast. We steer while it's in verification phase.
    def steer_during_verify():
        time.sleep(0.1)
        controller.steer("owner", task_id, "amendment-race", {
            "objective": "Support minutes AND hours format",
        })

    t = threading.Thread(target=steer_during_verify)
    t.start()

    run_res = controller.run("owner", task_id, execution_host="fixture")
    t.join()
    result = run_res["result"]

    assert result["accepted"] is True
    assert result["amendment_pending"] is True
    assert result["dispatch_objective"] == "Support minutes format (e.g. 45m)"

    # Ensure a new generation was scheduled
    lineage = controller.status("owner", task_id)["lineage"]
    assert lineage["pending_generation"] == 2
    assert lineage["scheduled_generation"] == 2
