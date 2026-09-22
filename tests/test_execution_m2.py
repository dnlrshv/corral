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


@pytest.mark.parametrize("secret", [
    "OPENAI_API_KEY=sk-proj-abcdefghijk12345",
    "SLACK_TOKEN=xoxb-12345678-abcdefgh",
    "GITHUB_TOKEN=github_pat_abcdefghijk",
    "password: 'plain-text-secret'",
])
def test_manifest_refuses_additional_credential_shapes(tmp_path, secret):
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=f@example.invalid",
                    "commit", "--allow-empty", "-qm", "fixture"], cwd=repo, check=True)
    (repo / "selected.txt").write_text(secret)
    with pytest.raises(PermissionError, match="credential-shaped"):
        manifest(repo, ["selected.txt"])


def test_manifest_allows_source_token_expressions_and_annotations(tmp_path):
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=f@example.invalid",
                    "commit", "--allow-empty", "-qm", "fixture"], cwd=repo, check=True)
    (repo / "selected.py").write_text(
        '_TOKEN = re.compile(r"(\\d+)([dhm])")\ndef parse(token: str):\n    return token\n')
    assert manifest(repo, ["selected.py"])["files"]["selected.py"]["digest"]


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
