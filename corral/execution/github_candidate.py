"""Controller-owned authenticated PR reads and immutable Git-object exports."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from .store import digest


def _oid(value: Any, label: str) -> str:
    text = str(value or "")
    if len(text) != 40 or any(char not in "0123456789abcdef" for char in text):
        raise ValueError(f"authenticated GitHub {label} is not a full lowercase commit OID")
    return text


def _base_ref(value: Any) -> str:
    text = str(value or "")
    result = subprocess.run(["git", "check-ref-format", "--branch", text],
                            text=True, capture_output=True)
    if result.returncode or text.startswith("-"):
        raise ValueError("authenticated GitHub base ref is invalid")
    return text


def authenticated_pr(repository: str, number: int, github: dict[str, Any]) -> dict[str, Any]:
    """Read candidate identity through the controller's configured GitHub account."""
    if number <= 0:
        raise ValueError("PR number must be positive")
    executable = github.get("executable", "gh")
    result = subprocess.run([executable, "api", "--method", "GET",
                             f"repos/{repository}/pulls/{number}"],
                            text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError("authenticated GitHub PR read failed")
    value = json.loads(result.stdout)
    if value.get("state") != "open":
        raise PermissionError("only an authenticated open PR is reviewable")
    return {"repository": repository, "pr_number": number,
            "head": _oid((value.get("head") or {}).get("sha"), "head"),
            "base": _oid((value.get("base") or {}).get("sha"), "base"),
            "base_ref": _base_ref((value.get("base") or {}).get("ref")),
            "draft": bool(value.get("draft")), "url": value.get("html_url"),
            "auth_mode": github.get("auth_mode", "controller-github-read")}


class GitObjects:
    """Fetch and read Git objects without checkout filters, hooks or diff drivers."""

    def __init__(self, path: Path, remote_url: str, *, allow_file_remote: bool = False):
        self.path, self.remote_url = path.resolve(), remote_url
        self.allow_file_remote = allow_file_remote
        if not self.path.is_absolute() or not remote_url or remote_url.startswith("-"):
            raise ValueError("Git object cache and remote URL are required")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            subprocess.run(["git", "init", "--bare", str(self.path)], check=True,
                           capture_output=True)

    def run(self, *args: str, input: bytes | None = None) -> bytes:
        env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
        file_policy = "always" if self.allow_file_remote else "never"
        command = ["git", "-c", "core.hooksPath=/dev/null", "-c",
                   f"protocol.file.allow={file_policy}", "-C", str(self.path), *args]
        result = subprocess.run(command, input=input, capture_output=True, env=env)
        if result.returncode:
            raise RuntimeError("trusted Git object operation failed")
        return result.stdout

    def fetch_pr(self, number: int, expected_head: str, base: str, base_ref: str) -> None:
        head_target = f"refs/corral/pr-{number}/head"
        base_target = f"refs/corral/pr-{number}/base"
        self.run("fetch", "--no-tags", "--force", "--", self.remote_url,
                 f"+refs/pull/{number}/head:{head_target}",
                 f"+refs/heads/{base_ref}:{base_target}")
        fetched_head = self.run("rev-parse", head_target).decode().strip()
        fetched_base = self.run("rev-parse", base_target).decode().strip()
        if fetched_head != expected_head or fetched_base != base:
            raise PermissionError("authenticated GitHub identity differs from fetched Git objects")
        for oid in (expected_head, base):
            self.run("cat-file", "-e", oid + "^{commit}")

    def export(self, head: str, destination: Path) -> dict[str, dict[str, Any]]:
        raw = self.run("ls-tree", "-rz", "--full-tree", head)
        selected: dict[str, dict[str, Any]] = {}
        for record in raw.split(b"\0"):
            if not record:
                continue
            metadata, name_bytes = record.split(b"\t", 1)
            mode, kind, oid = metadata.decode().split()
            name = name_bytes.decode("utf-8")
            relative = PurePosixPath(name)
            if kind != "blob" or mode not in {"100644", "100755"}:
                continue
            if relative.is_absolute() or ".." in relative.parts:
                raise PermissionError("Git tree contains an unsafe path")
            data = self.run("cat-file", "blob", oid)
            target = destination.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            selected[name] = {"digest": hashlib.sha256(data).hexdigest(),
                              "mode": int(mode, 8)}
        return selected

    def diff(self, base: str, head: str) -> bytes:
        return self.run("-c", "diff.external=", "diff", "--binary", "--no-ext-diff",
                        "--no-textconv", base, head, "--")


def _freeze(root: Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        path.chmod(0o555 if path.is_dir() else 0o444)


def prepare(state: Path, repository: str, number: int, policy_id: str,
            repository_config: dict[str, Any], *, expected_head: str | None = None,
            expected_base: str | None = None) -> dict[str, Any]:
    """Create or reuse a controller-derived immutable export and provenance receipt."""
    policy = repository_config.get("review_policies", {}).get(policy_id)
    if not isinstance(policy, dict):
        raise PermissionError("review policy is not registered")
    policy_digest = str(policy.get("digest", ""))
    if len(policy_digest) != 64 or any(c not in "0123456789abcdef" for c in policy_digest):
        raise ValueError("registered policy digest is invalid")
    candidate = authenticated_pr(repository, number, repository_config["github"])
    if candidate["draft"] and policy.get("allow_draft") is not True:
        raise PermissionError("draft PR is excluded by the registered review policy")
    if expected_head is not None and _oid(expected_head, "expected head") != candidate["head"]:
        raise PermissionError("requested head differs from authenticated GitHub head")
    if expected_base is not None and _oid(expected_base, "expected base") != candidate["base"]:
        raise PermissionError("requested base differs from authenticated GitHub base")
    objects = GitObjects(Path(repository_config["git_object_cache"]),
                         repository_config["remote_url"],
                         allow_file_remote=repository_config.get(
                             "development_file_remote") is True)
    objects.fetch_pr(number, candidate["head"], candidate["base"], candidate["base_ref"])
    binding = {key: candidate[key] for key in (
        "repository", "pr_number", "head", "base", "auth_mode")}
    binding.update(policy_id=policy_id, policy_digest=policy_digest)
    directory_id = digest(binding)
    root = state.resolve() / "candidate-snapshots" / directory_id
    receipt_path = root / "controller-provenance.json"
    if receipt_path.is_file():
        return json.loads(receipt_path.read_text())
    if root.exists():
        raise RuntimeError("incomplete immutable export requires operator reconciliation")
    root.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=directory_id + ".", dir=root.parent))
    try:
        selected = objects.export(candidate["head"], temporary)
        diff_path = "candidate.diff"
        diff_data = objects.diff(candidate["base"], candidate["head"])
        (temporary / diff_path).write_bytes(diff_data)
        diff_sha = hashlib.sha256(diff_data).hexdigest()
        selected[diff_path] = {"digest": diff_sha, "mode": 0o444}
        selected_digest = digest(selected)
        record = {**binding, "workspace": str(root), "selected_files": selected,
                  "selected_files_digest": selected_digest, "diff_path": diff_path,
                  "diff_sha256": diff_sha, "export_digest": selected_digest}
        receipt = {"export_id": digest(record), **record}
        (temporary / "controller-provenance.json").write_text(
            json.dumps(receipt, indent=2, sort_keys=True) + "\n")
        _freeze(temporary)
        try:
            temporary.rename(root)
            root.chmod(0o555)
        except FileExistsError:
            temporary.chmod(0o755)
            shutil.rmtree(temporary)
        return json.loads(receipt_path.read_text())
    except BaseException:
        if temporary.exists():
            for path in temporary.rglob("*"):
                path.chmod(0o755 if path.is_dir() else 0o644)
            temporary.chmod(0o755)
            shutil.rmtree(temporary)
        raise
