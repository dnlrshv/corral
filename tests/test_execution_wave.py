"""Unit and integration tests for finite automatic dependent wave progression and artifact handoff."""
from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

from corral.execution.controller import Controller
from corral.execution.demo import fixture_profiles
from corral.execution.wave import WaveRunner, handoff_key


def make_repo(path: Path) -> Path:
    repo = path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "config", "user.name", "Fixture"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "f@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "--allow-empty", "-qm", "initial"], cwd=repo, check=True)
    return repo


@pytest.fixture
def wave_env(tmp_path):
    repo1 = make_repo(tmp_path / "step1")
    repo2 = make_repo(tmp_path / "step2")
    repo3 = make_repo(tmp_path / "step3")
    state_dir = tmp_path / "controller_state"
    controller = Controller(
        state_dir, "owner",
        {"fixture": {"routes": ["fixture"], "harnesses": ["synthetic"], "cpu": 4, "memory_mb": 1024}},
        default_host="fixture", profiles=fixture_profiles(),
    )
    return controller, repo1, repo2, repo3, state_dir


def test_wave_advances_from_accepted_producer_to_consumer_via_artifact_handoff(wave_env):
    """W3-01: Finite wave advances automatically through digest-bound artifact handoff and readiness dispatch."""
    controller, repo1, repo2, _, _ = wave_env

    # Step 1 produces normalizer.py
    step1_worker = (
        "with open('normalizer.py', 'w') as f: f.write('def normalize(x): return x.strip().lower()\\n')\n"
        "with open('result.json', 'w') as f: f.write('{\"ok\": true}\\n')"
    )
    step1_verify = "import normalizer; assert normalizer.normalize(' FOO ') == 'foo'"

    # Step 2 imports normalizer.py and produces report.json
    step2_worker = (
        "import normalizer\n"
        "val = normalizer.normalize(' WAVE-3 ')\n"
        "with open('report.json', 'w') as f: f.write(f'{{\"normalized\": \"{val}\"}}\\n')\n"
        "with open('result.json', 'w') as f: f.write('{\"ok\": true}\\n')"
    )
    step2_verify = "import json; assert json.load(open('report.json'))['normalized'] == 'wave-3'"

    tasks = [
        {
            "name": "step1",
            "request_id": "req-step1",
            "spec": {
                "repo": "step1-repo",
                "workspace": str(repo1),
                "candidate_paths": ["normalizer.py", "result.json"],
                "command": [sys.executable, "-c", step1_worker],
                "verify": [sys.executable, "-c", step1_verify],
                "result_file": "result.json",
            },
        },
        {
            "name": "step2",
            "request_id": "req-step2",
            "spec": {
                "repo": "step2-repo",
                "workspace": str(repo2),
                "dependencies": ["step1"],
                "candidate_paths": ["report.json", "result.json"],
                "command": [sys.executable, "-c", step2_worker],
                "verify": [sys.executable, "-c", step2_verify],
                "result_file": "result.json",
            },
        },
    ]
    handoffs = [
        {
            "producer": "step1",
            "producer_path": "normalizer.py",
            "consumer": "step2",
            "consumer_path": "normalizer.py",
        }
    ]

    runner = WaveRunner(controller, "owner")
    runner.submit_wave("wave-1", tasks, handoffs)

    # Run wave to completion
    result = runner.run("wave-1", execution_host="fixture")
    assert result["all_completed"] is True
    assert result["status"] == "completed"
    assert len(result["blocked_tasks"]) == 0

    # Verify step2 workspace received and committed normalizer.py
    step2_norm = repo2 / "normalizer.py"
    assert step2_norm.is_file()
    assert "def normalize(x):" in step2_norm.read_text()

    # Verify handoff record in controller store
    step1_id = runner.controller.store.get("wave", "wave-1")["tasks"]["step1"]
    step2_id = runner.controller.store.get("wave", "wave-1")["tasks"]["step2"]
    hkey = handoff_key(step1_id, "normalizer.py", step2_id, "normalizer.py")
    hrecord = controller.store.get("artifact_handoff", hkey)
    assert hrecord is not None
    assert hrecord["status"] == "bound"
    assert hrecord["producer_generation"] == 1
    assert hrecord["artifact_digest"] == hashlib.sha256(step2_norm.read_bytes()).hexdigest()

    # Verify candidate artifacts preserved in immutable controller artifacts dir
    cand_dir = Path(controller.artifacts) / step1_id / "accepted_candidates"
    assert (cand_dir / "normalizer.py").is_file()


def test_wave_idempotency_repeat_run_does_not_duplicate_inference_or_handoff(wave_env):
    """W3-02: Repeating submit, handoff, completion events and reconnect does not duplicate work."""
    controller, repo1, repo2, _, _ = wave_env

    step1_worker = "open('normalizer.py','w').write('x=1\\n'); open('result.json','w').write('{\"ok\":true}')"
    step2_worker = "import normalizer; assert normalizer.x==1; open('result.json','w').write('{\"ok\":true}')"

    tasks = [
        {"name": "step1", "request_id": "req1", "spec": {
            "repo": "r1", "workspace": str(repo1), "candidate_paths": ["normalizer.py", "result.json"],
            "command": [sys.executable, "-c", step1_worker], "verify": [sys.executable, "-c", "open('result.json')"]}},
        {"name": "step2", "request_id": "req2", "spec": {
            "repo": "r2", "workspace": str(repo2), "dependencies": ["step1"], "candidate_paths": ["result.json"],
            "command": [sys.executable, "-c", step2_worker], "verify": [sys.executable, "-c", "open('result.json')"]}},
    ]
    handoffs = [{"producer": "step1", "producer_path": "normalizer.py", "consumer": "step2", "consumer_path": "normalizer.py"}]

    runner = WaveRunner(controller, "owner")
    runner.submit_wave("wave-idemp", tasks, handoffs)

    # Initial run
    res1 = runner.run("wave-idemp", execution_host="fixture")
    assert res1["all_completed"] is True
    invocations_first = dict(controller.store.records("invocation"))

    # Second run against completed state: no new dispatches
    res2 = runner.run("wave-idemp", execution_host="fixture")
    assert res2["all_completed"] is True
    assert res2["admitted"] == []
    assert dict(controller.store.records("invocation")) == invocations_first

    # Re-submitting the identical wave plan returns existing record without conflict
    sub2 = runner.submit_wave("wave-idemp", tasks, handoffs)
    assert sub2["wave_id"] == "wave-idemp"


def test_corrupted_or_missing_producer_output_blocks_only_dependent_work_while_independent_work_proceeds(wave_env):
    """W3-05: Missing/corrupted producer output blocks only affected dependent; independent ready work continues."""
    controller, repo1, repo2, repo3, _ = wave_env

    # Step 1: worker claims to succeed but fails to write declared candidate normalizer.py
    step1_worker = "open('result.json', 'w').write('{\"ok\": true}')"
    step1_verify = "open('result.json')"

    # Step 2: depends on step1 and requires normalizer.py handoff
    step2_worker = "open('result.json', 'w').write('{\"ok\": true}')"

    # Step 3: independent task (no dependency on step1)
    step3_worker = "open('step3_done', 'w').write('done'); open('result.json', 'w').write('{\"ok\": true}')"
    step3_verify = "assert open('step3_done').read() == 'done'"

    tasks = [
        {"name": "step1", "request_id": "req-s1", "spec": {
            "repo": "r1", "workspace": str(repo1), "candidate_paths": ["normalizer.py", "result.json"],
            "command": [sys.executable, "-c", step1_worker], "verify": [sys.executable, "-c", step1_verify]}},
        {"name": "step2", "request_id": "req-s2", "spec": {
            "repo": "r2", "workspace": str(repo2), "dependencies": ["step1"], "candidate_paths": ["result.json"],
            "command": [sys.executable, "-c", step2_worker], "verify": [sys.executable, "-c", "open('result.json')"]}},
        {"name": "step3", "request_id": "req-s3", "spec": {
            "repo": "r3", "workspace": str(repo3), "dependencies": [], "candidate_paths": ["result.json"],
            "command": [sys.executable, "-c", step3_worker], "verify": [sys.executable, "-c", step3_verify]}},
    ]
    handoffs = [{"producer": "step1", "producer_path": "normalizer.py", "consumer": "step2", "consumer_path": "normalizer.py"}]

    runner = WaveRunner(controller, "owner")
    runner.submit_wave("wave-isolation", tasks, handoffs)

    result = runner.run("wave-isolation", execution_host="fixture")

    # Step 1 completed, but handoff failed because normalizer.py was missing
    step2_id = runner.controller.store.get("wave", "wave-isolation")["tasks"]["step2"]
    step3_id = runner.controller.store.get("wave", "wave-isolation")["tasks"]["step3"]

    assert step2_id in result["blocked_tasks"]
    assert result["task_summary"][step2_id]["blocked"] is True
    assert "producer artifact missing" in result["task_summary"][step2_id]["blocker"]

    # Step 3 (independent) proceeded and completed successfully!
    assert result["task_summary"][step3_id]["status"] == "accepted"
    assert (repo3 / "step3_done").read_text() == "done"


def test_intervening_consumer_changes_refuses_handoff(wave_env):
    """Protects consumer workspace from intervening overwrite if destination modified after wave submission."""
    controller, repo1, repo2, _, _ = wave_env

    # Initial file exists in step2
    (repo2 / "lib.py").write_text("initial = True\n")
    subprocess.run(["git", "add", "lib.py"], cwd=repo2, check=True)
    subprocess.run(["git", "commit", "-m", "add lib"], cwd=repo2, check=True)

    tasks = [
        {"name": "s1", "request_id": "req-1", "spec": {
            "repo": "r1", "workspace": str(repo1), "candidate_paths": ["lib.py", "result.json"],
            "command": [sys.executable, "-c", "open('lib.py','w').write('producer=1\\n'); open('result.json','w').write('{\"ok\":true}')"],
            "verify": [sys.executable, "-c", "open('result.json')"]}},
        {"name": "s2", "request_id": "req-2", "spec": {
            "repo": "r2", "workspace": str(repo2), "dependencies": ["s1"], "candidate_paths": ["result.json"],
            "command": [sys.executable, "-c", "open('result.json','w').write('{\"ok\":true}')"],
            "verify": [sys.executable, "-c", "open('result.json')"]}},
    ]
    handoffs = [{"producer": "s1", "producer_path": "lib.py", "consumer": "s2", "consumer_path": "lib.py"}]

    runner = WaveRunner(controller, "owner")
    runner.submit_wave("wave-protect", tasks, handoffs)

    # Intervening edit in step2 consumer workspace before handoff runs!
    (repo2 / "lib.py").write_text("intervening_dev_change = True\n")

    result = runner.run("wave-protect", execution_host="fixture")
    s2_id = runner.controller.store.get("wave", "wave-protect")["tasks"]["s2"]
    assert s2_id in result["blocked_tasks"]
    assert "changed since wave submission" in result["task_summary"][s2_id]["blocker"]
    # Verify the intervening change was NOT overwritten
    assert "intervening_dev_change" in (repo2 / "lib.py").read_text()


def test_wave_cycle_detection_refuses_cyclic_plan(wave_env):
    """Cycle in wave dependency specification is detected and rejected before creating tasks."""
    controller, repo1, repo2, _, _ = wave_env
    tasks = [
        {"name": "A", "request_id": "req-A", "spec": {"repo": "r1", "workspace": str(repo1), "dependencies": ["B"]}},
        {"name": "B", "request_id": "req-B", "spec": {"repo": "r2", "workspace": str(repo2), "dependencies": ["A"]}},
    ]
    runner = WaveRunner(controller, "owner")
    with pytest.raises(ValueError, match="dependency cycle detected"):
        runner.submit_wave("cyclic-wave", tasks, [])

def test_wave_multi_producer_fan_in_allows_sequential_handoffs(wave_env):
    """W3-06: Two producers handing off to one consumer succeeds, bypassing unrelated-HEAD rejection."""
    controller, repo1, repo2, repo3, _ = wave_env

    (repo3 / "target1.py").write_text("empty\n")
    (repo3 / "target2.py").write_text("empty\n")
    subprocess.run(["git", "add", "."], cwd=repo3, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo3, check=True)

    tasks = [
        {"name": "p1", "request_id": "req-p1", "spec": {
            "repo": "r1", "workspace": str(repo1), "candidate_paths": ["target1.py", "result.json"],
            "command": [sys.executable, "-c", "open('target1.py','w').write('p1\\n'); open('result.json','w').write('{\"ok\":true}')"],
            "verify": [sys.executable, "-c", "open('result.json')"]}},
        {"name": "p2", "request_id": "req-p2", "spec": {
            "repo": "r2", "workspace": str(repo2), "candidate_paths": ["target2.py", "result.json"],
            "command": [sys.executable, "-c", "open('target2.py','w').write('p2\\n'); open('result.json','w').write('{\"ok\":true}')"],
            "verify": [sys.executable, "-c", "open('result.json')"]}},
        {"name": "c1", "request_id": "req-c1", "spec": {
            "repo": "r3", "workspace": str(repo3), "dependencies": ["p1", "p2"], "candidate_paths": ["result.json"],
            "command": [sys.executable, "-c", "open('result.json','w').write('{\"ok\":true}')"],
            "verify": [sys.executable, "-c", "open('result.json')"]}},
    ]
    handoffs = [
        {"producer": "p1", "producer_path": "target1.py", "consumer": "c1", "consumer_path": "target1.py"},
        {"producer": "p2", "producer_path": "target2.py", "consumer": "c1", "consumer_path": "target2.py"},
    ]

    runner = WaveRunner(controller, "owner")
    sub = runner.submit_wave("wave-fanin", tasks, handoffs)
    p1_id, p2_id, c1_id = sub["tasks"]["p1"], sub["tasks"]["p2"], sub["tasks"]["c1"]

    # Run wave
    res = runner.run("wave-fanin", "fixture")
    # p1 and p2 run
    assert res["task_summary"][p1_id]["status"] == "accepted"
    assert res["task_summary"][p2_id]["status"] == "accepted"
    # c1 runs and finishes
    assert res["task_summary"][c1_id]["status"] == "accepted"

    # Check that both files are present
    assert (repo3 / "target1.py").read_text() == "p1\n"
    assert (repo3 / "target2.py").read_text() == "p2\n"

def test_wave_reconcile_recovers_dead_runner(wave_env, tmp_path):
    """W3-07: Killed runner claimed recovery exposes precise uncertainty or releases dead process."""
    controller, _, _, _, _ = wave_env

    # write config for cli
    import json
    config = tmp_path / "controller.json"
    config.write_text(json.dumps({
        "state": str(controller.store.path.parent),
        "token": "owner",
        "hosts": controller.hosts,
        "default_host": "fixture",
        "execution_host": "fixture",
        "profiles": []
    }))

    # 1. Acquire owner, no PID record (simulate launch failure before child registration)
    controller.store.acquire("wave_runner:my_wave", "runner1")

    import subprocess
    import sys
    out = subprocess.run([sys.executable, "-m", "corral.execution.cli", "--config", str(config)],
                         input=json.dumps({"action": "reconcile-wave", "wave_id": "my_wave"}),
                         text=True, capture_output=True, check=True)
    res = json.loads(out.stdout)
    assert res["reconciled"] is False
    assert res["reason"] == "unobservable identity"

    # Owner should now be transitioned to "uncertain"
    with controller.store.transaction() as db:
        owner = db.execute("SELECT status FROM owners WHERE resource=?", ("wave_runner:my_wave",)).fetchone()
        assert owner[0] == "uncertain"

    # 2. Provide dead PID
    controller.store.replace("wave_runner_pid", "my_wave", {"owner": "runner1", "pid": 9999999})
    out2 = subprocess.run([sys.executable, "-m", "corral.execution.cli", "--config", str(config)],
                         input=json.dumps({"action": "reconcile-wave", "wave_id": "my_wave"}),
                         text=True, capture_output=True, check=True)
    res2 = json.loads(out2.stdout)
    assert res2["reconciled"] is True
    assert res2["reason"] == "proven dead"

    # Owner should now be "released"
    with controller.store.transaction() as db:
        owner = db.execute("SELECT status FROM owners WHERE resource=?", ("wave_runner:my_wave",)).fetchone()
        assert owner[0] == "released"


def _cli_config(controller, tmp_path):
    import json

    config = tmp_path / "controller.json"
    config.write_text(json.dumps({
        "state": str(controller.store.path.parent), "token": "owner",
        "hosts": controller.hosts, "default_host": "fixture",
        "execution_host": "fixture", "profiles": [],
    }))
    return config


def _cli(config, request, monkeypatch, capsys):
    import io
    import json

    from corral.execution import cli

    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(request)))
    cli.main(["--config", str(config)])
    return json.loads(capsys.readouterr().out)


def _runner_owner(controller, wave_id):
    return controller.store.ownership(f"wave_runner:{wave_id}")


def test_run_wave_records_the_runner_birth_identity(wave_env, tmp_path, monkeypatch, capsys):
    import os

    from corral.execution.runtime_identity import process_start

    controller = wave_env[0]
    config = _cli_config(controller, tmp_path)
    launched = []

    class Runner:
        pid = os.getpid()

    from types import SimpleNamespace

    from corral.execution import cli

    # Only the runner launch is faked; process observation still runs the real ``ps``.
    monkeypatch.setattr(cli, "subprocess", SimpleNamespace(
        DEVNULL=subprocess.DEVNULL,
        Popen=lambda command, **_kwargs: launched.append(command) or Runner()))

    result = _cli(config, {"action": "run-wave", "wave_id": "identity-wave"}, monkeypatch, capsys)

    assert result["dispatch"] == "launched"
    assert "--execute-wave" in launched[0]
    record = controller.store.get("wave_runner_pid", "identity-wave")
    assert record["owner"] == result["runner_id"]
    assert record["identity"]["pid"] == os.getpid()
    assert record["identity"]["process_start"] == process_start(os.getpid())


def test_wave_reconcile_treats_a_reused_runner_pid_as_dead(
        wave_env, tmp_path, monkeypatch, capsys):
    """A PID handed to another process after the runner exited must not hold the wave."""
    import os

    controller = wave_env[0]
    config = _cli_config(controller, tmp_path)
    epoch = controller.store.acquire("wave_runner:reused", "runner1")
    # This PID is alive, but it belongs to a different process birth than the runner's.
    controller.store.replace("wave_runner_pid", "reused", {
        "owner": "runner1", "pid": os.getpid(),
        "identity": {"pid": os.getpid(), "process_start": "Thu Jan  1 00:00:00 1970",
                     "executable": sys.executable, "host": "fixture"}})

    result = _cli(config, {"action": "reconcile-wave", "wave_id": "reused"}, monkeypatch, capsys)

    assert result == {"wave_id": "reused", "reconciled": True, "reason": "proven dead"}
    assert _runner_owner(controller, "reused") == ("runner1", epoch, "released")


def test_wave_reconcile_keeps_a_runner_whose_birth_identity_matches(
        wave_env, tmp_path, monkeypatch, capsys):
    import os
    import socket

    from corral.execution.runtime_identity import launched

    controller = wave_env[0]
    config = _cli_config(controller, tmp_path)
    epoch = controller.store.acquire("wave_runner:live", "runner1")
    controller.store.replace("wave_runner_pid", "live", {
        "owner": "runner1", "pid": os.getpid(),
        "identity": launched(os.getpid(), sys.executable, socket.gethostname())})

    result = _cli(config, {"action": "reconcile-wave", "wave_id": "live"}, monkeypatch, capsys)

    assert result == {"wave_id": "live", "reconciled": False,
                      "reason": "process is still alive"}
    assert _runner_owner(controller, "live") == ("runner1", epoch, "active")


def test_wave_reconcile_never_trusts_a_live_pid_without_birth_identity(
        wave_env, tmp_path, monkeypatch, capsys):
    """An older record holds only a PID: an existing process cannot prove the runner alive."""
    import os

    controller = wave_env[0]
    config = _cli_config(controller, tmp_path)
    epoch = controller.store.acquire("wave_runner:legacy", "runner1")
    controller.store.replace("wave_runner_pid", "legacy", {"owner": "runner1",
                                                           "pid": os.getpid()})

    result = _cli(config, {"action": "reconcile-wave", "wave_id": "legacy"}, monkeypatch, capsys)

    assert result == {"wave_id": "legacy", "reconciled": False,
                      "reason": "cannot verify process identity"}
    assert _runner_owner(controller, "legacy") == ("runner1", epoch, "uncertain")
