"""Controller-owned authenticated PR reads and immutable Git-object exports."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import shlex
import subprocess
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Any

from .policy_inputs import normalize as normalize_policy_inputs
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

    def __init__(self, path: Path, remote_url: str, *, allow_file_remote: bool = False,
                 credential_helper: list[str] | None = None):
        self.path, self.remote_url = path.resolve(), remote_url
        self.allow_file_remote = allow_file_remote
        self.credential_helper = list(credential_helper or [])
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
        if self.credential_helper:
            binary = Path(self.credential_helper[0])
            if (not binary.is_absolute() or not binary.is_file()
                    or not os.access(binary, os.X_OK)):
                raise PermissionError("trusted Git credential helper must be an absolute executable")
            helper = "!" + shlex.join(self.credential_helper)
            command[1:1] = ["-c", "credential.helper=", "-c",
                            f"credential.helper={helper}", "-c", "credential.useHttpPath=true"]
        result = subprocess.run(command, input=input, capture_output=True, env=env)
        if result.returncode:
            raise RuntimeError("trusted Git object operation failed")
        return result.stdout

    def fetch_pr(self, number: int, expected_head: str, base: str, base_ref: str) -> str:
        head_target = f"refs/corral/pr-{number}/head"
        base_target = f"refs/corral/pr-{number}/base"
        self.run("fetch", "--no-tags", "--force", "--", self.remote_url,
                 f"+refs/pull/{number}/head:{head_target}",
                 f"+refs/heads/{base_ref}:{base_target}")
        fetched_head = self.run("rev-parse", head_target).decode().strip()
        current_base = self.run("rev-parse", base_target).decode().strip()
        if fetched_head != expected_head:
            raise PermissionError("authenticated GitHub head differs from fetched Git objects")
        for oid in (expected_head, base):
            self.run("cat-file", "-e", oid + "^{commit}")
        self.run("merge-base", "--is-ancestor", base, current_base)
        return current_base

    def _blob(self, revision: str, name: str) -> tuple[str, str, bytes]:
        raw = self.run("ls-tree", "-z", revision, "--", name)
        records = [item for item in raw.split(b"\0") if item]
        if len(records) != 1:
            raise PermissionError(f"registered inspection source is unavailable: {name}")
        metadata, actual = records[0].split(b"\t", 1)
        mode, kind, oid = metadata.decode().split()
        if actual.decode("utf-8") != name or kind != "blob" or mode not in {"100644", "100755"}:
            raise PermissionError(f"registered inspection source is not a regular file: {name}")
        return mode, oid, self.run("cat-file", "blob", oid)

    def source_digest(self, revision: str, name: str) -> str:
        return hashlib.sha256(self._blob(revision, name)[2]).hexdigest()

    def changed_paths(self, base: str, head: str) -> list[str]:
        raw = self.run("diff", "--name-only", "-z", "--diff-filter=ACMRT", base, head, "--")
        return sorted(item.decode("utf-8") for item in raw.split(b"\0") if item)

    def export(self, head: str, destination: Path,
               paths: list[str]) -> dict[str, dict[str, Any]]:
        selected: dict[str, dict[str, Any]] = {}
        for name in sorted(set(paths)):
            relative = PurePosixPath(name)
            if relative.is_absolute() or ".." in relative.parts:
                raise PermissionError("Git tree contains an unsafe path")
            _mode, _oid_value, data = self._blob(head, name)
            target = destination.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            selected[name] = {"digest": hashlib.sha256(data).hexdigest(),
                              "mode": 0o444}
        return selected

    def diff(self, base: str, head: str) -> bytes:
        return self.run("-c", "diff.external=", "diff", "--binary", "--no-ext-diff",
                        "--no-textconv", base, head, "--")


def _freeze(root: Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        path.chmod(0o555 if path.is_dir() else 0o444)


def _remove_tree(root: Path) -> None:
    for path in root.rglob("*"):
        path.chmod(0o755 if path.is_dir() else 0o644)
    root.chmod(0o755)
    shutil.rmtree(root)


def _observed_files(root: Path) -> dict[str, dict[str, Any]]:
    return {str(path.relative_to(root)): {
        "digest": hashlib.sha256(path.read_bytes()).hexdigest(),
        "mode": path.stat().st_mode & 0o777}
        for path in sorted(root.rglob("*")) if path.is_file()}


def _write_receipt(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.chmod(0o400)
    os.replace(temporary, path)


def _policy_binding(objects: GitObjects, policy_id: str, policy: dict[str, Any],
                    base: str, base_ref: str) -> tuple[str, list[str]]:
    inputs = normalize_policy_inputs(policy.get("inputs"))
    required = list(inputs["required_sources"])
    if not required:
        raise ValueError("review policy requires explicit repository policy sources")
    if inputs.get("base_ref") not in (None, base_ref):
        raise PermissionError("review policy base_ref differs from authenticated PR base")
    source_digests = {name: objects.source_digest(base, name) for name in required}
    observed = digest({"policy_id": policy_id, "inputs": inputs, "sources": source_digests})
    expected = policy.get("expected_digest")
    if expected is not None and expected != observed:
        raise PermissionError("repository policy sources differ from the registered digest")
    related = policy.get("related_sources", [])
    if (not isinstance(related, list) or any(
            not isinstance(name, str) or not name or PurePosixPath(name).is_absolute()
            or ".." in PurePosixPath(name).parts for name in related)):
        raise ValueError("review policy related_sources are invalid")
    return observed, sorted(set(required + related))


def prepare(state: Path, repository: str, number: int, policy_id: str,
            repository_config: dict[str, Any], *, expected_head: str | None = None,
            expected_base: str | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Create or reuse a controller-derived immutable export and provenance receipt."""
    policy = repository_config.get("review_policies", {}).get(policy_id)
    if not isinstance(policy, dict):
        raise PermissionError("review policy is not registered")
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
                             "development_file_remote") is True,
                         credential_helper=repository_config.get("github", {}).get(
                             "git_credential_helper"))
    base_ref_tip = objects.fetch_pr(
        number, candidate["head"], candidate["base"], candidate["base_ref"])
    policy_digest, related = _policy_binding(
        objects, policy_id, policy, candidate["base"], candidate["base_ref"])
    changed = objects.changed_paths(candidate["base"], candidate["head"])
    selected_paths = sorted(set(changed + related))
    if any(name == ".corral-review" or name.startswith(".corral-review/")
           for name in selected_paths):
        raise PermissionError("candidate collides with the reserved review metadata path")
    binding = {key: candidate[key] for key in (
        "repository", "pr_number", "head", "base", "auth_mode")}
    binding.update(policy_id=policy_id, policy_digest=policy_digest)
    directory_id = digest(binding)
    root = state.resolve() / "candidate-snapshots" / directory_id
    receipts = state.resolve() / "trusted-export-receipts"
    receipt_path = receipts / (directory_id + ".json")
    observation = {"repository": repository, "pr_number": number,
                   "head": candidate["head"], "base": candidate["base"],
                   "base_ref": candidate["base_ref"], "base_ref_tip": base_ref_tip,
                   "auth_mode": candidate["auth_mode"], "observed_at": time.time()}
    if receipt_path.is_file() and root.is_dir():
        return json.loads(receipt_path.read_text()), observation
    if receipt_path.exists() and not root.is_dir():
        raise RuntimeError("trusted export receipt exists without its immutable workspace")
    root.parent.mkdir(parents=True, exist_ok=True)
    receipts.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=directory_id + ".", dir=root.parent))
    try:
        selected = objects.export(candidate["head"], temporary, selected_paths)
        diff_path = ".corral-review/candidate.diff"
        diff_data = objects.diff(candidate["base"], candidate["head"])
        diff_target = temporary / diff_path
        diff_target.parent.mkdir(parents=True)
        diff_target.write_bytes(diff_data)
        diff_sha = hashlib.sha256(diff_data).hexdigest()
        selected[diff_path] = {"digest": diff_sha, "mode": 0o444}
        selected_digest = digest(selected)
        record = {**binding, "workspace": str(root), "selected_files": selected,
                  "selected_files_digest": selected_digest, "diff_path": diff_path,
                  "diff_sha256": diff_sha, "export_digest": selected_digest}
        receipt = {"export_id": digest(record), **record}
        _freeze(temporary)
        if root.exists():
            if _observed_files(root) != selected:
                raise RuntimeError("incomplete immutable export bytes require reconciliation")
            _remove_tree(temporary)
        else:
            temporary.rename(root)
            root.chmod(0o555)
        _write_receipt(receipt_path, receipt)
        return receipt, observation
    except BaseException:
        if temporary.exists():
            _remove_tree(temporary)
        raise
