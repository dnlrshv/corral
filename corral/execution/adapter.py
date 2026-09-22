"""Trusted native adapter: launched by the controller outside the worker boundary.

The adapter is controller-side code. It reads only controller-written files, proves the
worker containment boundary with a real sandboxed probe, then launches the harness *inside*
that boundary and interprets the harness output with the declared versioned envelope schema.
Worker I/O (workspace, scratch) and trusted I/O (task directory) never share a channel: the
worker cannot write the task directory, so it cannot forge the structured completion, the
usage record or the containment receipt.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from ..redaction import redact_nested_text
from . import containment, envelopes
from .inspection_packet import PACKET_FILE
from .store import digest
from .workspace import safe_path

ADAPTER_RESULT = "adapter-result.json"
ATTEMPTS = "attempts.jsonl"
BOUNDARY_FILE = "boundary.json"
CONTEXT_FILE = "context.json"
PLAN_FILE = "launch-plan.json"
PROMPT_FILE = "prompt.md"
RESULT_SCHEMA_FILE = "result-schema.json"
HARNESS_LOG = "harness.log"

BASE_ENV_NAMES = ("PATH", "LANG", "LC_ALL", "TERM")
DEFAULT_RESULT_SCHEMA = {"type": "object", "additionalProperties": True}


def source_root() -> Path:
    """Repository root that makes ``corral`` importable from any worker cwd."""
    return Path(__file__).resolve().parents[2]


_BOOTSTRAP = (
    "import runpy,sys;"
    "sys.path.insert(0,{root!r});"
    "runpy.run_module('corral.execution.adapter',run_name='__main__',alter_sys=True)"
)


def build_adapter_command(*, task_dir: Path | str, workspace: Path | str,
                          source: Path | str | None = None) -> list[str]:
    """Return the trusted adapter command line, importable from a foreign cwd.

    ``-I`` isolates the interpreter: no ambient PYTHONPATH, no user site, and no
    worker-writable cwd prepended to ``sys.path``, so a workspace cannot shadow the
    adapter package or inject ``sitecustomize``. The source root is a literal argv
    value owned by the controller, never read from the environment or the workspace.
    """
    root = Path(source if source is not None else source_root()).resolve()
    workspace_path = Path(workspace).resolve()
    task_path = Path(task_dir).resolve()
    # Checked first: a worker-writable source root is a security refusal, not a config typo.
    for label, path in (("workspace", workspace_path), ("task directory", task_path)):
        if root == path or root in path.parents:
            raise PermissionError(f"corral source root must not live inside the {label}: {root}")
    adapter_module = root / "corral" / "execution" / "adapter.py"
    if not adapter_module.is_file():
        raise PermissionError(f"corral source root does not contain the adapter package: {root}")
    return [sys.executable, "-I", "-c", _BOOTSTRAP.format(root=str(root)),
            "--task-dir", str(task_path), "--workspace", str(workspace_path)]


def _load_json(path: Path, label: str) -> dict:
    try:
        payload = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise PermissionError(f"controller-written {label} is missing or unparsable: {type(error).__name__}")
    if not isinstance(payload, dict):
        raise PermissionError(f"controller-written {label} must be an object")
    return payload


def _boundary(task_dir: Path) -> tuple[containment.Boundary, dict]:
    record = _load_json(task_dir / BOUNDARY_FILE, "worker boundary")
    declared = record.get("boundary")
    if not isinstance(declared, dict):
        raise PermissionError("worker boundary declaration is incomplete")
    try:
        boundary = containment.Boundary(workspace=str(declared["workspace"]), scratch=str(declared["scratch"]),
                                        tmpdir=str(declared["tmpdir"]), deny=tuple(declared.get("deny") or ()),
                                        allow=tuple(declared.get("allow") or ()),
                                        deny_write=tuple(declared.get("deny_write") or ()),
                                        write_allow=tuple(declared.get("write_allow") or ()),
                                        write_file_allow=tuple(declared.get("write_file_allow") or ()),
                                        trusted_read_allow=tuple(declared.get("trusted_read_allow") or ()),
                                        trusted_metadata_allow=tuple(declared.get("trusted_metadata_allow") or ()),
                                        trusted_read_roots=tuple(declared.get("trusted_read_roots") or ()),
                                        sentinels=tuple(declared.get("sentinels") or ()),
                                        network=bool(declared.get("network", True)))
    except KeyError as error:
        raise PermissionError(f"worker boundary declaration is missing {error}") from error
    return boundary, record


def _substitute(argv: list[str], values: dict[str, str]) -> list[str]:
    """Render a controller-declared argv without interpreting worker prompt text.

    Route placeholders are checked before values are inserted. This preserves literal
    braces in an objective while still refusing a broken route declaration.
    """
    pattern = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}")
    rendered = []
    for template in argv:
        unknown = sorted(set(pattern.findall(template)) - {"{" + key + "}" for key in values})
        if unknown:
            raise PermissionError(f"route argv retains an unresolved placeholder: {unknown}")
        item = template
        for key, value in values.items():
            item = item.replace("{" + key + "}", value)
        rendered.append(item)
    return rendered


def _harness_env(plan: dict, boundary: containment.Boundary, home: str | None,
                 scratch: Path) -> tuple[dict[str, str], list[str], list[str]]:
    env: dict[str, str] = {}
    for name in BASE_ENV_NAMES:
        if os.environ.get(name):
            env[name] = os.environ[name]
    env["TMPDIR"] = str(scratch)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if home:
        env["HOME"] = home
    for pair in plan.get("route", {}).get("runtime_env") or ():
        name, _, value = str(pair).partition("=")
        if name:
            env[name] = value
    missing, forwarded = [], []
    for name in plan.get("credential_env") or ():
        value = os.environ.get(name)
        if value:
            env[name] = value
            forwarded.append(name)
        else:
            missing.append(name)
    return env, forwarded, missing


def _write_prompt(scratch: Path, context: dict) -> Path:
    objective = str(context.get("objective") or "").strip()
    if not objective:
        raise PermissionError("native dispatch requires an explicit objective; nothing was inferred")
    parts = [objective]
    extras = context.get("prompt_extras")
    if isinstance(extras, str) and extras.strip():
        parts.append(extras.strip())
    history = context.get("amendment_history")
    if isinstance(history, list) and history:
        parts.append("Prior amendments for this same task identity (continue, do not restart):")
        parts.extend(f"- {json.dumps(item, sort_keys=True)}" for item in history)
    path = scratch / PROMPT_FILE
    path.write_text("\n\n".join(parts) + "\n")
    return path


def _record_attempt(task_dir: Path, record: dict) -> None:
    with (task_dir / ATTEMPTS).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def _write_result(task_dir: Path, result: dict) -> None:
    payload = json.dumps(result, indent=2, sort_keys=True)
    tmp = task_dir / (ADAPTER_RESULT + ".partial")
    tmp.write_text(payload)
    os.replace(tmp, task_dir / ADAPTER_RESULT)


def run_adapter(task_dir: Path, workspace: Path) -> int:
    """Execute one trusted adapter attempt. Returns the process exit status."""
    task_dir = Path(task_dir).resolve()
    workspace = Path(workspace).resolve()
    started = time.time()
    context = _load_json(task_dir / CONTEXT_FILE, "task context")
    plan = _load_json(task_dir / PLAN_FILE, "native launch plan")
    boundary, boundary_record = _boundary(task_dir)
    attempt = str(context.get("attempt") or "unknown-attempt")
    usage_path = Path(os.environ.get("CORRAL_USAGE_PATH") or (task_dir / "native-usage.json"))
    if not safe_path(str(workspace), str(Path(context.get("result_file") or "result.json"))).parent:
        raise PermissionError("unsafe result path")
    route = plan.get("route") or {}
    inspection_only = route.get("inspection_only") is True
    expected_worker_workspace = Path(boundary.scratch).resolve() if inspection_only else workspace
    if str(expected_worker_workspace) != str(Path(boundary.workspace).resolve()):
        raise PermissionError("adapter workspace does not match the declared worker boundary")

    result: dict[str, Any] = {
        "schema": "corral-adapter-result-v1", "task": context.get("task"), "attempt": attempt,
        "status": "failed", "structured": None, "narrative": "", "identity_observed": {},
        "identity_requested": {"model": plan.get("model"), "effort": plan.get("effort"),
                               "route": plan.get("route", {}).get("id"),
                               "provider": plan.get("route", {}).get("provider"),
                               "account_ref": plan.get("route", {}).get("account_ref")},
        "usage_events": [], "errors": [], "warnings": [], "containment": None, "harness": {},
        "synthetic": bool(plan.get("route", {}).get("synthetic")), "detail": {},
    }
    try:
        receipt = containment.require(boundary)
    except PermissionError as error:
        result["errors"].append(f"worker containment unavailable: {error}")
        result["containment"] = {"passed": False, "blocker": str(error)}
        _finish(task_dir, result, usage_path, started, attempt)
        return 1
    expected_digest = boundary_record.get("profile_digest")
    if expected_digest and expected_digest != receipt.get("profile_digest"):
        result["errors"].append("worker boundary profile changed between controller and adapter")
        result["containment"] = receipt
        _finish(task_dir, result, usage_path, started, attempt)
        return 1
    result["containment"] = receipt

    scratch = Path(boundary.scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    schema = str(route.get("envelope") or "")
    try:
        prompt_path = _write_prompt(scratch, context)
        packet_path = scratch / PACKET_FILE
        if inspection_only:
            packet = _load_json(packet_path, "inspection packet")
            declared_packet = plan.get("inspection_packet") or {}
            if packet.get("digest") != declared_packet.get("digest"):
                raise PermissionError("inspection packet changed after controller preparation")
            if digest({key: value for key, value in packet.items() if key != "digest"}) != packet.get("digest"):
                raise PermissionError("inspection packet digest is invalid")
        structured_path = scratch / "structured-result.json"
        schema_path = scratch / RESULT_SCHEMA_FILE
        schema_path.write_text(json.dumps(context.get("result_schema") or DEFAULT_RESULT_SCHEMA, indent=2))
        values = {"workspace": str(workspace), "scratch": str(scratch),
                  "prompt_file": str(prompt_path), "model": str(plan.get("model") or ""),
                  "prompt": prompt_path.read_text(),
                  "effort": str(plan.get("effort") or ""), "result_file": str(structured_path),
                  "schema_file": str(schema_path), "log_file": str(task_dir / HARNESS_LOG),
                  "packet_file": str(packet_path)}
        argv = _substitute([str(plan.get("binary") or "")] + list(plan.get("argv") or ()), values)
    except PermissionError as error:
        result["errors"].append(str(error))
        _finish(task_dir, result, usage_path, started, attempt)
        return 1
    if not argv or not argv[0]:
        result["errors"].append("route produced no harness command")
        _finish(task_dir, result, usage_path, started, attempt)
        return 1

    env, forwarded, missing = _harness_env(plan, boundary, plan.get("home"), scratch)
    if missing and not result["synthetic"]:
        result["errors"].append(f"route credential environment is absent: {sorted(missing)}")
        result["harness"] = {"route": route.get("id"), "credential_env_present": forwarded}
        _finish(task_dir, result, usage_path, started, attempt)
        return 1
    result["harness"] = {"route": route.get("id"), "binary": plan.get("binary"),
                         "argv_digest": hashlib.sha256(json.dumps(argv).encode()).hexdigest(),
                         "argv": argv, "env_names": sorted(env), "credential_env_present": forwarded,
                         "credential_env_missing": sorted(missing), "cwd": str(workspace),
                         "containment": receipt.get("profile_digest")}
    profile = containment.build_profile(boundary)
    command = containment.wrapped(profile, argv)
    wait_seconds = context.get("operational_wait_seconds")
    exit_code = None
    try:
        with (task_dir / "harness.stdout").open("wb") as out, (task_dir / "harness.stderr").open("wb") as err:
            child = subprocess.Popen(command, cwd=str(scratch if inspection_only else workspace),
                                     stdout=out, stderr=err,
                                     stdin=subprocess.DEVNULL, env=env, start_new_session=False)
            try:
                exit_code = child.wait(timeout=float(wait_seconds) if wait_seconds else None)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
                result["errors"].append(f"explicit operational wait exceeded: {wait_seconds}s")
    except OSError as error:
        result["errors"].append(f"harness launch failed: {type(error).__name__}: {error}")
        _finish(task_dir, result, usage_path, started, attempt)
        return 1

    stdout_text = _read_tail(task_dir / "harness.stdout")
    stderr_text = _read_tail(task_dir / "harness.stderr")
    log_text = _read_tail(task_dir / HARNESS_LOG) if (task_dir / HARNESS_LOG).exists() else ""
    try:
        envelope = envelopes.parse(schema, stdout_text=stdout_text, stderr_text=stderr_text,
                                   exit_code=exit_code if exit_code is not None else -1,
                                   invocation=attempt, result_path=str(scratch / "structured-result.json"),
                                   log_text=log_text, synthetic_expected=result["synthetic"])
    except ValueError as error:
        result["errors"].append(str(error))
        result["detail"] = {"exit_code": exit_code, "stdout_bytes": len(stdout_text),
                            "stderr_bytes": len(stderr_text), "raw_preserved": True}
        _finish(task_dir, result, usage_path, started, attempt)
        return 1
    result["status"] = "completed" if envelope.ok else "failed"
    result["structured"] = envelope.structured
    result["narrative"] = envelope.narrative
    result["identity_observed"] = envelope.identity
    result["errors"].extend(envelope.errors)
    result["warnings"].extend(envelope.warnings)
    result["detail"] = envelope.detail
    failure_path = not envelope.ok
    events = [dict(event, failure_path=failure_path) for event in envelope.usage_events]
    result["usage_events"] = events
    if not events:
        result["warnings"].append("no native usage counters were reported; usage stays unknown, not zero")
    _finish(task_dir, result, usage_path, started, attempt, events)
    return 0 if envelope.ok else 1


def _read_tail(path: Path, limit: int = 4_000_000) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    return data[-limit:].decode("utf-8", errors="replace")


def _finish(task_dir: Path, result: dict, usage_path: Path, started: float, attempt: str,
            events: list[dict] | None = None) -> None:
    result["detail"]["duration_seconds"] = round(time.time() - started, 3)
    result["detail"]["raw_stdout_preserved"] = (task_dir / "harness.stdout").exists()
    result["detail"]["raw_stderr_preserved"] = (task_dir / "harness.stderr").exists()
    # Harness errors, warnings and stdout/stderr tails can echo credentials (tracebacks,
    # request dumps); only redacted diagnostics leave the raw harness streams.
    for field in ("errors", "warnings", "detail"):
        result[field] = redact_nested_text(result[field])
    _write_result(task_dir, result)
    _record_attempt(task_dir, {"attempt": attempt, "status": result["status"],
                               "errors": result["errors"], "warnings": result["warnings"],
                               "usage_events": len(result["usage_events"]),
                               "identity_observed": result["identity_observed"],
                               "synthetic": result["synthetic"],
                               "duration_seconds": result["detail"]["duration_seconds"]})
    if events:
        write_native_usage(usage_path, events)
    elif usage_path.exists() is False:
        # Telemetry absence is preserved as an explicit empty record, never as zeros.
        from .atomic_io import write_json
        write_json(usage_path, [])


def write_native_usage(usage_file: Path, events: list[dict]) -> None:
    """Append usage events to the controller-owned usage path (trusted side only)."""
    existing: list[dict] = []
    if usage_file.is_file():
        try:
            loaded = json.loads(usage_file.read_text())
            existing = [item for item in loaded if isinstance(item, dict)] if isinstance(loaded, list) else []
        except (ValueError, OSError):
            existing = []
    existing.extend(events)
    from .atomic_io import write_json
    write_json(usage_file, existing)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-dir", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    args = parser.parse_args()
    return run_adapter(args.task_dir, args.workspace)


if __name__ == "__main__":
    sys.exit(main())
