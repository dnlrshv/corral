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
from pathlib import Path
from typing import Any

from corral.redaction import check_outbound_safe

from .store import digest
from .workspace import safe_path

PACKET_FILE = "inspection-packet.json"
PACKET_SCHEMA = "corral-inspection-packet-v1"
_HEX_REV = re.compile(r"^[0-9a-f]{40,64}$")
_EXECUTION_WORDS = re.compile(
    r"\b(run|execute|invoke|launch)\b.{0,40}\b(test|tests|script|scripts|build|import|pytest|make|npm|package)\b",
    re.IGNORECASE,
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _provenance(workspace: Path, raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise PermissionError("inspection reviewer requires controller-owned provenance")
    kind = raw.get("kind")
    if kind not in ("git-checkout", "immutable-snapshot"):
        raise PermissionError("inspection provenance kind must be git-checkout or immutable-snapshot")
    required = ("repo", "head", "base")
    if any(not isinstance(raw.get(key), str) or not raw[key] for key in required):
        raise PermissionError("inspection provenance requires repo, head, and base")
    if not _HEX_REV.fullmatch(raw["head"]) or not _HEX_REV.fullmatch(raw["base"]):
        raise PermissionError("inspection head/base must be full hexadecimal revisions")
    result = {key: raw[key] for key in required}
    result["kind"] = kind
    if raw.get("pr") is not None:
        if isinstance(raw["pr"], bool) or not isinstance(raw["pr"], int) or raw["pr"] <= 0:
            raise PermissionError("inspection PR number must be a positive integer")
        result["pr"] = raw["pr"]
    if kind == "git-checkout":
        try:
            actual = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=workspace, text=True, stderr=subprocess.DEVNULL
            ).strip()
        except (OSError, subprocess.CalledProcessError) as error:
            raise PermissionError("declared Git checkout has no readable Git metadata") from error
        if actual != raw["head"]:
            raise PermissionError("inspection checkout HEAD does not match declared candidate")
        status = subprocess.check_output(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=workspace,
            text=True,
            stderr=subprocess.DEVNULL,
        ).splitlines()
        result["checkout_dirty"] = bool(status)
        result["checkout_status_digest"] = digest(status)
    else:
        # A snapshot deliberately does not call Git.  Its identity is the declared remote
        # provenance plus the exact document digests recorded below.
        result["git_metadata_required"] = False
    return result


def build(spec: dict, context: dict, workspace: Path | str) -> dict[str, Any]:
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
        if check_outbound_safe(text):
            raise PermissionError(f"credential-shaped inspection input refused: {name}")
        documents.append({"path": name, "kind": "diff" if name == diff_path else "source",
                          "sha256": _sha(data), "bytes": len(data), "content": text})

    objective = str(context.get("objective") or "").strip()
    if not objective:
        raise PermissionError("inspection packet requires an explicit objective")
    denied = ["candidate-code-execution", "shell", "tests", "imports", "build-hooks", "package-commands"]
    provenance = _provenance(root, spec.get("inspection_provenance"))
    document_bindings = [
        {"path": item["path"], "kind": item["kind"], "sha256": item["sha256"],
         "bytes": item["bytes"]}
        for item in documents
    ]
    provenance["documents_digest"] = digest(document_bindings)
    if provenance["kind"] == "immutable-snapshot":
        provenance["files_export_digest"] = provenance["documents_digest"]
        provenance["files_export_digest_source"] = "controller-selected-document-bytes"
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
