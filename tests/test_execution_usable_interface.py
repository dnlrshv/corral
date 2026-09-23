import json
import sys
import time
import uuid

import pytest

from corral.execution.agent import CorralAgent
from tests.test_execution_regressions import setup, spec

__all__ = ['setup', 'spec']

def test_usable_agent_run_wrong_host_refusal(setup, tmp_path):
    controller, repo = setup
    controller_config = tmp_path / "controller.json"
    controller_config.write_text(json.dumps({
        "state": str(controller.store.path.parent),
        "token": "owner",
        "hosts": {"fixture": controller.hosts["fixture"], "wrong": {"cpu": 1, "memory_mb": 1}},
        "default_host": "wrong",
        "execution_host": "fixture"
    }))
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "controller_config": str(controller_config)
    }))
    agent_inst = CorralAgent(config_path)
    # Test that run on wrong host raises or blocks appropriately

    with pytest.raises(Exception):
        agent_inst.run(repo, "do something", host="unauthorized_host")

def test_usable_agent_run_profile_defaults(setup, tmp_path):
    controller, repo = setup
    controller_config = tmp_path / "controller.json"
    controller_config.write_text(json.dumps({
        "state": str(controller.store.path.parent),
        "token": "owner",
        "hosts": controller.hosts,
        "default_host": "fixture",
        "execution_host": "fixture",
        "profiles": [
            {
                "id": "gemini-1.5-pro-high",
                "version": 1,
                "model": "gemini-1.5-pro",
                "effort": "high",
                "route": "fixture",
                "harness": "synthetic",
                "roles": {"implementation": "foo"},
                "tools": ["read", "search", "edit", "shell", "test"],
                "context": 0
            }
        ]
    }))
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "controller_config": str(controller_config),
        "default_host": "fixture",
        "profiles": {
            "my-repo-profile": {
                "workspace": str(repo),
                "verify": [sys.executable, "-c", "import sys; sys.exit(0)"],
                "candidate_paths": ["test_prof.txt"],
                "model": "gemini-1.5-pro",
                "effort": "high"
            }
        }
    }))

    agent_inst = CorralAgent(config_path)

    # We omit verify and candidate_paths in the run call, they should be injected from profile
    (repo / "test_prof.txt").write_text("initial")
    result = agent_inst.run(repo, "objective profile", profile_id="my-repo-profile", command=[sys.executable, "-c", "import sys; sys.exit(0)"], timeout=5.0, poll_interval=0.1)

    assert result["state"]["status"] == "completed"
    assert result["result"]["accepted"] is True
    # Verify overrides were used
    task_id = list(controller.store.records("request").keys())[-1]
    req = controller.store.records("request")[task_id]
    assert req["selection"]["profile"]["model"] == "gemini-1.5-pro"
    assert req["selection"]["profile"]["effort"] == "high"

def test_usable_agent_run_verified_outcome(setup, tmp_path, monkeypatch):
    controller, repo = setup
    controller_config = tmp_path / "controller.json"
    controller_config.write_text(json.dumps({
        "state": str(controller.store.path.parent),
        "token": "owner",
        "hosts": controller.hosts,
        "default_host": "fixture",
        "execution_host": "fixture",
        "profiles": []
    }))
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "controller_config": str(controller_config),
        "default_host": "fixture"
    }))

    agent_inst = CorralAgent(config_path)

    # We need a command that succeeds
    (repo / "test.txt").write_text("initial")
    result = agent_inst.run(repo, "objective", candidate_paths=["test.txt"], command=[sys.executable, "-c", "import sys; sys.exit(0)"], verify=[sys.executable, "-c", "import sys; sys.exit(0)"], timeout=5.0, poll_interval=0.1)

    assert result["state"]["status"] == "completed"
    assert result["result"]["accepted"] is True

def test_usable_agent_wave_start_and_disconnect(setup, tmp_path, monkeypatch):
    controller, repo = setup
    controller_config = tmp_path / "controller.json"
    controller_config.write_text(json.dumps({
        "state": str(controller.store.path.parent),
        "token": "owner",
        "hosts": controller.hosts,
        "default_host": "fixture",
        "execution_host": "fixture",
        "profiles": []
    }))
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "controller_config": str(controller_config),
        "default_host": "fixture"
    }))

    agent_inst = CorralAgent(config_path)

    wave_id = "wave-" + uuid.uuid4().hex

    prod_script = "import time, sys\ntime.sleep(1.5)\nwith open('prod.txt', 'w') as f: f.write('produced data')\nsys.exit(0)"
    prod_verify = "import sys\nwith open('prod.txt', 'r') as f: sys.exit(0 if f.read() == 'produced data' else 1)"

    cons_script = "import sys\nwith open('in.txt', 'r') as f: data = f.read()\nif data != 'produced data': sys.exit(1)\nwith open('cons.txt', 'w') as f: f.write('consumed ' + data)\nsys.exit(0)"
    cons_verify = "import sys\nwith open('cons.txt', 'r') as f: sys.exit(0 if f.read() == 'consumed produced data' else 1)"

    tasks = [
        {
            "request_id": "req-prod",
            "name": "producer",
            "spec": {
                "repo": str(repo),
                "workspace": str(repo),
                "objective": "produce",
                "command": [sys.executable, "-c", prod_script],
                "candidate_paths": ["prod.txt"],
                "verify": [sys.executable, "-c", prod_verify]
            }
        },
        {
            "request_id": "req-cons",
            "name": "consumer",
            "spec": {
                "repo": str(repo),
                "workspace": str(repo),
                "objective": "consume",
                "command": [sys.executable, "-c", cons_script],
                "candidate_paths": ["cons.txt"],
                "dependencies": ["producer"],
                "verify": [sys.executable, "-c", cons_verify]
            }
        }
    ]
    handoffs = [
        {
            "producer": "producer",
            "producer_path": "prod.txt",
            "consumer": "consumer",
            "consumer_path": "in.txt"
        }
    ]

    # Start once (client disconnects immediately because run-wave spawns detached and returns)
    res = agent_inst.wave_start(wave_id, tasks, handoffs)
    assert res["dispatch"] == "launched"

    # Reconnect immediately and repeat start before producer finishes
    res2 = agent_inst.wave_start(wave_id, tasks, handoffs)
    assert res2["dispatch"] == "already-running-or-uncertain"

    # Check that autonomous consumer accepted
    for _ in range(50):
        wave_status = agent_inst.wave_status(wave_id)
        if wave_status["state"]["status"] == "completed":
            break
        time.sleep(0.1)
    else:
        assert False, f"Wave did not complete, status: {wave_status['state']['status']}"

    assert wave_status["state"]["status"] == "completed"
    assert (repo / "cons.txt").read_text() == "consumed produced data"

    # Inspect worker claim safety under runner crash and expose uncertain status/reconcile
    wave_id_2 = "wave-" + uuid.uuid4().hex
    crashed_id = f"wave_runner_{uuid.uuid4().hex[:8]}"
    epoch = controller.store.acquire(f"wave_runner:{wave_id_2}", crashed_id)
    controller.store.replace("wave_runner_pid", wave_id_2, {"pid": 99999999, "owner": crashed_id})
    controller.store.transition_owner(f"wave_runner:{wave_id_2}", crashed_id, epoch, "uncertain")

    res3 = agent_inst.wave_start(wave_id_2, tasks, handoffs)
    assert res3["dispatch"] == "already-running-or-uncertain"

    status3 = agent_inst.wave_status(wave_id_2)
    assert status3["runner"]["status"] == "uncertain"

    reconciled = agent_inst.wave_reconcile(wave_id_2)
    assert reconciled["reconciled"] is True

    status3_reconciled = agent_inst.wave_status(wave_id_2)
    assert status3_reconciled["runner"]["status"] == "released"

    # Test unknown identity remains held
    wave_id_3 = "wave-" + uuid.uuid4().hex
    unknown_id = "unknown_identity"
    epoch = controller.store.acquire(f"wave_runner:{wave_id_3}", unknown_id)
    controller.store.transition_owner(f"wave_runner:{wave_id_3}", unknown_id, epoch, "uncertain")
    reconciled_unknown = agent_inst.wave_reconcile(wave_id_3)
    assert reconciled_unknown["reconciled"] is False
    assert agent_inst.wave_status(wave_id_3)["runner"]["status"] == "uncertain"

def test_wait_amendment_dispatch(setup, tmp_path, monkeypatch):
    controller, repo = setup
    controller_config = tmp_path / "controller.json"
    controller_config.write_text(json.dumps({
        "state": str(controller.store.path.parent),
        "token": "owner",
        "hosts": controller.hosts,
        "default_host": "fixture",
        "execution_host": "fixture",
        "profiles": []
    }))
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "controller_config": str(controller_config),
        "default_host": "fixture"
    }))

    agent_inst = CorralAgent(config_path)

    task_id = agent_inst.submit(repo, "objective 1", command=[sys.executable, "-c", "import sys; sys.exit(0)"], verify=[sys.executable, "-c", "import sys; sys.exit(0)"])

    # Simulate completion of generation 1
    controller.run("owner", task_id, execution_host="fixture")

    # Schedule continuation (adds pending generation 2)
    agent_inst.continue_task(task_id, "cont-1", {"objective": "objective 2"})

    # Check wait dispatches generation 2 and returns completed
    res = agent_inst.wait(task_id, poll_interval=0.1, timeout=5.0)
    assert res["state"]["status"] == "completed"
    assert res["lineage"]["current_generation"] == 2
    assert res["state"]["amended_objective_pending"] is False

def test_usable_agent_return_artifact(setup, tmp_path):
    controller, repo = setup
    controller_config = tmp_path / "controller.json"
    controller_config.write_text(json.dumps({
        "state": str(controller.store.path.parent),
        "token": "owner",
        "hosts": controller.hosts,
        "default_host": "fixture",
        "execution_host": "fixture",
        "profiles": []
    }))
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "controller_config": str(controller_config),
        "default_host": "fixture"
    }))

    agent_inst = CorralAgent(config_path)

    (repo / "out.txt").write_text("initial")
    task_id = agent_inst.submit(
        repo, "produce output",
        candidate_paths=["out.txt"],
        command=[sys.executable, "-c", "import sys; open('out.txt', 'w').write('accepted result'); sys.exit(0)"],
        verify=[sys.executable, "-c", "import sys; sys.exit(0 if open('out.txt').read() == 'accepted result' else 1)"]
    )

    agent_inst.dispatch(task_id)
    res = agent_inst.wait(task_id)
    assert res["result"]["accepted"] is True

    # Return to fresh destination
    dest = tmp_path / "returned.txt"
    returned = agent_inst.return_artifact(task_id, "out.txt", dest)
    assert returned.read_text() == "accepted result"

    # Existing identical
    returned_again = agent_inst.return_artifact(task_id, "out.txt", dest)
    assert returned_again.read_text() == "accepted result"

    # Existing different
    dest.write_text("newer dev edit")
    with pytest.raises(PermissionError, match="already exists and differs from artifact. Conflict refused."):
        agent_inst.return_artifact(task_id, "out.txt", dest)

    # Workspace edits do not lose accepted output (it fetches from immutable archive)
    (repo / "out.txt").write_text("hacked output")
    agent_inst.return_artifact(task_id, "out.txt", tmp_path / "other.txt")
    assert (tmp_path / "other.txt").read_text() == "accepted result"

    # Start failed generation 2
    agent_inst.continue_task(task_id, "cont", {"objective": "fail now", "verify": [sys.executable, "-c", "import sys; sys.exit(1)"]})
    agent_inst.dispatch(task_id)
    res2 = agent_inst.wait(task_id)
    assert res2["result"]["accepted"] is False
    assert res2["lineage"]["current_generation"] == 2

    # Now retrieve generation 1
    dest_gen1 = tmp_path / "gen1.txt"
    agent_inst.return_artifact(task_id, "out.txt", dest_gen1, generation=1)
    assert dest_gen1.read_text() == "accepted result"

    # Corrupted archive refuses
    art_dir = controller.artifacts / task_id / "accepted_candidates"
    (art_dir / "out.txt").write_text("corrupted archive bytes")
    with pytest.raises(RuntimeError, match="artifact data corruption in archive"):
        agent_inst.return_artifact(task_id, "out.txt", tmp_path / "corrupt.txt", generation=1)


def test_agent_without_a_host_defers_to_the_controller_default(setup, tmp_path):
    controller, repo = setup
    controller_config = tmp_path / "controller.json"
    controller_config.write_text(json.dumps({
        "state": str(controller.store.path.parent),
        "token": "owner",
        "hosts": controller.hosts,
        "default_host": "fixture",
        "execution_host": "fixture",
    }))
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"controller_config": str(controller_config)}))

    agent_inst = CorralAgent(config_path)
    # The client ships no host of its own; the controller's configured default applies.
    assert agent_inst.config.default_host is None
    task = agent_inst.submit(repo, "objective without a host")
    assert controller.store.get("request", task)["host"] == "fixture"
