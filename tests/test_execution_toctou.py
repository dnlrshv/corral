"""Tests for symlink TOCTOU race protection in workspace manifest application."""
import base64
import hashlib
import subprocess
import pytest

from corral.execution.workspace import apply_manifest, manifest


@pytest.fixture
def repo_dir(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.local"], cwd=repo, check=True)
    (repo / "initial.txt").write_text("initial")
    subprocess.run(["git", "add", "initial.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True)
    return repo


def test_manifest_safe_write_nested_success(repo_dir):
    current = manifest(repo_dir, ["nested/dir/file.txt"])
    data = b"hello nested safe write"
    incoming = {
        "base": current["base"],
        "files": {
            "nested/dir/file.txt": {
                "digest": hashlib.sha256(data).hexdigest(),
                "data": base64.b64encode(data).decode(),
                "mode": 0o644,
            }
        },
    }
    from corral.execution.store import digest
    incoming["digest"] = digest({"base": incoming["base"], "files": incoming["files"]})

    result = apply_manifest(repo_dir, incoming, current)
    assert (repo_dir / "nested/dir/file.txt").read_bytes() == data
    assert result["files"]["nested/dir/file.txt"]["digest"] == hashlib.sha256(data).hexdigest()


def test_manifest_blocks_symlink_toctou_replacement(repo_dir, tmp_path):
    outside = tmp_path / "outside_dir"
    outside.mkdir()

    # Pre-create a directory in repo
    sub = repo_dir / "subdir"
    sub.mkdir()

    current = manifest(repo_dir, ["subdir/target.txt"])
    data = b"secret payload that must not escape"
    incoming = {
        "base": current["base"],
        "files": {
            "subdir/target.txt": {
                "digest": hashlib.sha256(data).hexdigest(),
                "data": base64.b64encode(data).decode(),
                "mode": 0o644,
            }
        },
    }
    from corral.execution.store import digest
    incoming["digest"] = digest({"base": incoming["base"], "files": incoming["files"]})

    # Adversary replaces 'subdir' with a symlink to outside
    sub.rmdir()
    sub.symlink_to(outside)

    # apply_manifest must fail safely with OSError / NotADirectoryError due to O_NOFOLLOW
    with pytest.raises(OSError):
        apply_manifest(repo_dir, incoming, current)

    # Verify no file was written outside
    assert not (outside / "target.txt").exists()
