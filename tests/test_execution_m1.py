import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from corral.execution.controller import Controller
from corral.execution.profiles import Profile, observe, resolve
from corral.execution.store import Store
from corral.execution.usage import Spool, summarize


def profiles():
    return [Profile("strong-low", "fixture-strong", "low", "synthetic", "1", "fixture",
                    ("implementation", "review", "repair", "adjudication"), ("shell", "edit"), 1000),
            Profile("weak-xhigh", "fixture-weak", "xhigh", "synthetic", "1", "fixture",
                    ("implementation", "review", "repair", "adjudication"), ("shell", "edit"), 1000)]


def test_joint_resolution_and_overrides():
    options = dict(role="implementation", routes=["fixture"], default="strong-low")
    assert resolve(profiles(), **options)["confidence"] == "low"
    for evidence, expected in [({"strong-low": 9, "weak-xhigh": 2}, "strong-low"),
                               ({"strong-low": 1, "weak-xhigh": 8}, "weak-xhigh")]:
        assert resolve(profiles(), **options, evidence=evidence)["profile"]["id"] == expected
    assert resolve(profiles(), **options, model="fixture-weak")["profile"]["effort"] == "xhigh"
    assert resolve(profiles(), **options, effort="low")["profile"]["model"] == "fixture-strong"
    with pytest.raises(PermissionError):
        resolve(profiles(), **options, model="fixture-strong", effort="xhigh")
    selected = resolve(profiles(), **options)
    assert observe(selected, {})["observed"] == {}
    assert observe(selected, {"effort": "high"})["mismatches"] == ["effort"]
    with pytest.raises(PermissionError):
        observe(selected, {"route": "unapproved"})
    assert resolve([], **options, deterministic=True)["inference_calls"] == 0


def test_usage_outage_replay_resets_and_unknown(tmp_path):
    spool, target = Spool(tmp_path / "spool"), Store(tmp_path / "controller")
    events = [{"id": str(i), "invocation": "worker", "scope": "session", "epoch": epoch,
               "sequence": seq, "mode": "cumulative", "counters": {"input": value}}
              for i, epoch, seq, value in [(1, 0, 2, 20), (2, 0, 1, 10), (3, 1, 1, 5)]]
    for event in events + events:
        spool.append(event)
    # Simulate unavailable publisher; durable source survives a new object.
    Spool(tmp_path / "spool").flush(target)
    spool.flush(target)
    assert len(target.records("usage")) == 3
    report = summarize(list(target.records("usage").values()), registered=["worker", "root"])
    assert report["observed_fields"]["input"] == 25
    assert report["unknown"] == [{"invocation": "root", "reason": "no native events"}]
    spool.append({**events[0], "counters": {"input": 999}})
    spool.flush(target)
    assert len(target.records("usage_conflict")) == 1
    decreasing = [events[1], {**events[0], "counters": {"input": 1}}]
    assert summarize(decreasing)["unknown"][0]["reason"] == "unmarked counter reset"


def test_one_complete_task_real_process(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=f@example.invalid",
                    "commit", "--allow-empty", "-qm", "fixture"], cwd=workspace, check=True)
    controller = Controller(tmp_path / "state", "client-secret",
                            {"local-fixture": {"routes": ["fixture"], "harnesses": ["synthetic"]}},
                            default_host="local-fixture", profiles=profiles())
    spec = {"repo": "fixture", "workspace": str(workspace), "candidate_paths": ["result.json"],
            "command": [sys.executable, "-c", "import json; open('result.json','w').write(json.dumps({'answer':42,'prose':''}))"],
            "verify": [sys.executable, "-c", "import json; assert json.load(open('result.json')) == {'answer':42,'prose':''}"],
            "selection": resolve(profiles(), role="implementation", routes=["fixture"], default="strong-low")}
    task = controller.submit("client-secret", "one-task", spec)
    assert controller.submit("client-secret", "one-task", spec) == task
    with pytest.raises(PermissionError):
        controller.steer("worker-PASS", task, "attack", {"owner": "worker"})
    result = controller.run("client-secret", task, execution_host="local-fixture")
    assert result["result"]["accepted"]
    assert result["result"]["structured"] == {"answer": 42, "prose": ""}
    assert json.loads((controller.artifacts / task / "structured.json").read_text()) == {"answer": 42, "prose": ""}
    assert controller.run("client-secret", task, execution_host="local-fixture") == result


def test_single_writer_race_and_uncertainty(tmp_path):
    store = Store(tmp_path / "store")
    def claim(owner):
        try:
            return store.acquire("workspace", owner)
        except PermissionError:
            return None
    with ThreadPoolExecutor(2) as pool:
        assert sorted(str(x) for x in pool.map(claim, ["a", "b"])) == ["1", "None"]
    owner, epoch, _ = store.ownership("workspace")
    store.transition_owner("workspace", owner, epoch, "uncertain")
    with pytest.raises(PermissionError):
        store.acquire("workspace", "new-worker")
