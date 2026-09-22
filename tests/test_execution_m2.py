import copy
import subprocess

import pytest

from corral.execution.scheduler import due_occurrence, ready
from corral.execution.store import Store
from corral.execution.workspace import apply_manifest, manifest


def test_dirty_binary_untracked_transfer_and_newer_dev(tmp_path):
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=f@example.invalid",
                    "commit", "--allow-empty", "-qm", "fixture"], cwd=repo, check=True)
    (repo / "data.bin").write_bytes(b"\x00\xffold")
    initial = manifest(repo, ["data.bin", "new.txt"])
    (repo / "data.bin").write_bytes(b"\x00\xffnew")
    (repo / "new.txt").write_text("untracked authorized")
    result = manifest(repo, ["data.bin", "new.txt"])
    (repo / "data.bin").write_bytes(b"\x00\xffold")
    (repo / "new.txt").unlink()
    assert apply_manifest(repo, result, initial)["digest"] == result["digest"]
    (repo / "new.txt").write_text("new owner change")
    with pytest.raises(PermissionError):
        apply_manifest(repo, initial, result)
    assert (repo / "new.txt").read_text() == "new owner change"
    with pytest.raises(PermissionError):
        manifest(repo, [".env"])
    with pytest.raises(ValueError):
        manifest(repo, ["../escape"])
    broken = copy.deepcopy(result)
    broken["files"]["new.txt"]["data"] = "AAAA"
    with pytest.raises(ValueError):
        apply_manifest(repo, broken, result)


def test_fairness_resources_dependencies_and_recurrence(tmp_path):
    host = {"cpu": 4, "memory_mb": 400, "routes": ["fixture"], "interactive_boost_seconds": 10}
    tasks = [{"id": "wave", "mode": "wave", "submitted": 0, "route": "fixture", "cpu": 4},
             {"id": "chat", "mode": "interactive", "submitted": 20, "route": "fixture", "cpu": 1},
             {"id": "blocked", "mode": "wave", "submitted": 0, "route": "fixture", "dependencies": ["absent"]}]
    assert ready(tasks, [], [], host, 21) == ["wave"]
    assert ready(tasks, [], [{"cpu": 3}], host, 21) == ["chat"]
    store = Store(tmp_path / "s")
    schedule = {"id": "timer", "start": 0, "interval": 10}
    assert due_occurrence(store, schedule, 35)
    assert due_occurrence(store, schedule, 39) is None
    assert due_occurrence(store, schedule, 40)
