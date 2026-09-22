"""Build an immutable, controller-selected packet for inspection-only reviewers.

The model transport receives this packet as data.  It gets no repository path, process
tool, shell, import hook, or callback that could ask the controller to execute candidate
code.  Source selection and provenance validation happen here, before paid inference.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import urllib.parse
from pathlib import Path
from typing import Any

from corral.redaction import check_outbound_safe, check_source_text_safe

from .store import digest
from .workspace import safe_path

PACKET_FILE = "inspection-packet.json"
PACKET_SCHEMA = "corral-inspection-packet-v1"
_HEX_REV = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_BASE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_EXECUTION_WORDS = re.compile(
    r"\b(run|execute|invoke|launch)\b.{0,40}\b(test|tests|script|scripts|build|import|pytest|make|npm|package)\b",
    re.IGNORECASE,
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _trusted_workspace(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PermissionError("inspection reviewer requires controller workspace provenance")
    expected = value.get("digest")
    actual = digest({key: item for key, item in value.items() if key != "digest"})
    if not isinstance(expected, str) or expected != actual:
        raise PermissionError("controller workspace provenance digest is invalid")
    if value.get("kind") not in ("checkout", "immutable_snapshot"):
        raise PermissionError("inspection workspace must be checkout or immutable_snapshot")
    if not _HEX_REV.fullmatch(str(value.get("head") or "")):
        raise PermissionError("controller workspace head must be a full Git SHA")
    if not isinstance(value.get("files"), dict):
        raise PermissionError("controller workspace file binding is missing")
    return value


def _remote_identity(remote: str) -> str:
    """Return a non-secret repository identity derived from the checkout's Git remote."""
    if remote.startswith("git@github.com:"):
        slug = remote.split(":", 1)[1]
    else:
        parsed = urllib.parse.urlsplit(remote)
        slug = parsed.path.lstrip("/") if parsed.hostname == "github.com" else ""
    if slug.endswith(".git"):
        slug = slug[:-4]
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", slug):
        return slug
    return "remote-sha256:" + hashlib.sha256(remote.encode()).hexdigest()


def bind_candidate(spec: dict, workspace: Path | str, workspace_provenance: Any) -> dict[str, Any]:
    """Derive candidate identity from controller-preflight provenance before inference."""
    trusted = _trusted_workspace(workspace_provenance)
    pr = spec.get("inspection_pr")
    if pr is not None and (isinstance(pr, bool) or not isinstance(pr, int) or pr <= 0):
        raise PermissionError("inspection PR number must be a positive integer")
    if trusted["kind"] == "immutable_snapshot":
        required = ("repo", "base", "export_id", "export_digest", "trusted_export_id",
                    "policy_id", "policy_digest", "auth_mode")
        if any(not isinstance(trusted.get(key), str) or not trusted[key] for key in required):
            raise PermissionError("controller snapshot provenance is incomplete")
        base = trusted["base"]
        if not _HEX_REV.fullmatch(base):
            raise PermissionError("controller snapshot base must be a full Git SHA")
        result = {"kind": "immutable_snapshot", "repo": trusted["repo"],
                  "head": trusted["head"], "base": base,
                  "export_id": trusted["export_id"], "export_digest": trusted["export_digest"],
                  "trusted_export_id": trusted["trusted_export_id"],
                  "policy_id": trusted["policy_id"], "policy_digest": trusted["policy_digest"],
                  "auth_mode": trusted["auth_mode"],
                  "workspace_provenance_digest": trusted["digest"],
                  "git_metadata_required": False}
    else:
        base_ref = spec.get("inspection_base_ref")
        if not isinstance(base_ref, str) or not _BASE_REF.fullmatch(base_ref) or base_ref.startswith("-"):
            raise PermissionError("checkout inspection requires a safe inspection_base_ref")
        try:
            remote = subprocess.check_output(
                ["git", "remote", "get-url", "origin"], cwd=workspace, text=True,
                stderr=subprocess.DEVNULL).strip()
            current_head = subprocess.check_output(
                ["git", "rev-parse", "--verify", "HEAD^{commit}"], cwd=workspace, text=True,
                stderr=subprocess.DEVNULL).strip()
            base = subprocess.check_output(
                ["git", "rev-parse", "--verify", f"{base_ref}^{{commit}}"], cwd=workspace,
                text=True, stderr=subprocess.DEVNULL).strip()
        except (OSError, subprocess.CalledProcessError) as error:
            raise PermissionError("checkout inspection candidate binding is unavailable") from error
        if not remote or current_head != trusted["head"] or not _HEX_REV.fullmatch(base):
            raise PermissionError("checkout inspection candidate binding is invalid")
        result = {"kind": "checkout", "repo": _remote_identity(remote),
                  "head": trusted["head"], "base": base, "base_ref": base_ref,
                  "workspace_provenance_digest": trusted["digest"]}
    if pr is not None:
        result["pr"] = pr
    result["digest"] = digest(result)
    return result


def build(spec: dict, context: dict, workspace: Path | str,
          candidate_binding: dict[str, Any]) -> dict[str, Any]:
    """Read only the controller allowlist and return a digest-bound review packet."""
    root = Path(workspace).resolve()
    if spec.get("role") != "review":
        raise PermissionError("inspection packet transport is restricted to the review role")
    selected = spec.get("inspection_paths")
    if not isinstance(selected, list) or not selected or not all(isinstance(item, str) for item in selected):
        raise PermissionError("inspection reviewer requires a nonempty inspection_paths allowlist")
    diff_path = spec.get("inspection_diff_path")
    if not isinstance(diff_path, str) or not diff_path:
        raise PermissionError("inspection reviewer requires an immutable diff path")
    allowlist = list(dict.fromkeys([*selected, diff_path]))
    candidates = set(spec.get("candidate_paths") or ())
    if any(name not in candidates for name in allowlist):
        raise PermissionError("every inspection input must be bound as a candidate path")

    documents = []
    for name in allowlist:
        path = safe_path(root, name)
        if not path.is_file():
            raise PermissionError(f"inspection input is missing: {name}")
        data = path.read_bytes()
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise PermissionError(f"inspection input must be UTF-8 text: {name}") from error
        if check_source_text_safe(text):
            raise PermissionError(f"credential-shaped inspection input refused: {name}")
        documents.append({"path": name, "kind": "diff" if name == diff_path else "source",
                          "sha256": _sha(data), "bytes": len(data), "content": text})

    objective = str(context.get("objective") or "").strip()
    if not objective:
        raise PermissionError("inspection packet requires an explicit objective")
    if check_outbound_safe(objective):
        raise PermissionError("credential-shaped inspection objective refused")
    denied = ["candidate-code-execution", "shell", "tests", "imports", "build-hooks", "package-commands"]
    trusted = _trusted_workspace(context.get("workspace_provenance"))
    if candidate_binding.get("workspace_provenance_digest") != trusted["digest"]:
        raise PermissionError("inspection candidate is not bound to controller workspace provenance")
    if candidate_binding.get("digest") != digest(
            {key: item for key, item in candidate_binding.items() if key != "digest"}):
        raise PermissionError("inspection candidate binding digest is invalid")
    for document in documents:
        bound = trusted["files"].get(document["path"])
        if not isinstance(bound, dict) or bound.get("digest") != document["sha256"]:
            raise PermissionError("inspection document changed after controller preflight")
    document_bindings = [
        {"path": item["path"], "kind": item["kind"], "sha256": item["sha256"],
         "bytes": item["bytes"]}
        for item in documents
    ]
    provenance = {**candidate_binding, "documents_digest": digest(document_bindings)}
    packet = {
        "schema": PACKET_SCHEMA,
        "task": str(context.get("task") or ""),
        "attempt": str(context.get("attempt") or ""),
        "generation": int(context.get("generation") or 1),
        "objective": objective,
        "provenance": provenance,
        "documents": documents,
        "capability": {
            "mode": "stateless-inspection-only",
            "tools_supplied": [],
            "session_reuse": False,
            "denied": denied,
            "execution_request_detected": bool(_EXECUTION_WORDS.search(objective)),
            "report_output": "controller scratch and artifacts only",
        },
    }
    if not packet["task"] or not packet["attempt"]:
        raise PermissionError("inspection packet lacks controller task/attempt binding")
    packet["digest"] = digest(packet)
    return packet


def persist(packet: dict[str, Any], scratch: Path | str) -> Path:
    """Atomically persist the packet in the one-task scratch directory."""
    directory = Path(scratch)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / PACKET_FILE
    partial = path.with_suffix(".partial")
    partial.write_text(json.dumps(packet, indent=2, sort_keys=True))
    partial.replace(path)
    return path
