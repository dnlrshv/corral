"""Shared support for offline native full-path proofs.

Nothing here is monkeypatched into Corral: the synthetic harness is a real executable that
a host-owned route declaration points at, exactly like a production harness binary. Every
artifact it produces is explicitly marked synthetic so no reader can mistake it for a live
provider response.
"""
from __future__ import annotations

import dataclasses
import json
import stat
import subprocess
import sys
from pathlib import Path

from corral.execution.controller import Controller
from corral.execution.profiles import Profile

FAKE_HARNESS = "corral-fake-native"
FAKE_ROUTE = "fixture-synthetic-native"
FAKE_HOST = "fixture-native"
FAKE_MODEL = "fixture-model"
FAKE_EFFORT = "high"

#: Real executable harness. Ops are declared in the prompt file, one per line:
#:   @op write <relpath> <base64>        write a workspace file (a real code change)
#:   @op result <base64-json>            structured completion, written to {result_file}
#:   @op narrative <text>                narrative text (separate channel from structured)
#:   @op usage <json>                    reported counters
#:   @op usage-mode <cumulative|delta>   counter semantics
#:   @op status <completed|failed>       envelope status
#:   @op exit <code>                     process exit status
#:   @op identity <json>                 override the reported identity block (a lying harness)
#:   @op deny-probe <abspath>            attempt read+write, report the kernel errno
#:   @op forge-receipt <abspath>         attempt to overwrite a trusted controller artifact
FAKE_NATIVE_CLI = r'''#!/usr/bin/env python3
"""Explicitly synthetic native coding harness for offline Corral proofs."""
import base64, errno, json, os, sys
from pathlib import Path


TWO_FIELD_OPS = {"write"}


def parse(argv):
    ops, named = [], {}
    index = 0
    while index < len(argv):
        item = argv[index]
        if item.startswith("--"):
            named[item] = argv[index + 1]
            index += 2
        else:
            index += 1
    for line in Path(named["--prompt"]).read_text().splitlines():
        if not line.startswith("@op "):
            continue
        body = line[4:]
        kind = body.split(" ", 1)[0]
        ops.append(body.split(" ", 2) if kind in TWO_FIELD_OPS else body.split(" ", 1))
    return named, ops


def probe(target):
    observed = {}
    try:
        with open(target, "rb") as handle:
            handle.read(1)
        observed["read"] = "allowed"
    except OSError as error:
        observed["read"] = errno.errorcode.get(error.errno, str(error.errno))
    try:
        fd = os.open(target, os.O_WRONLY | os.O_APPEND)
        os.close(fd)
        observed["write"] = "allowed"
    except OSError as error:
        observed["write"] = errno.errorcode.get(error.errno, str(error.errno))
    return observed


def main():
    named, ops = parse(sys.argv[1:])
    workspace = Path(named["--workspace"])
    detail = {"home": os.environ.get("HOME"), "cwd": os.getcwd(),
              "tmpdir": os.environ.get("TMPDIR"), "probes": {}}
    status, narrative, usage, mode, code = "completed", "", {}, "cumulative", 0
    identity_override = {}
    for op in ops:
        kind = op[0]
        if kind == "write":
            (workspace / op[1]).write_bytes(base64.b64decode(op[2]))
        elif kind == "result":
            Path(named["--result"]).write_bytes(base64.b64decode(op[1]))
        elif kind == "narrative":
            narrative = op[1]
        elif kind == "usage":
            usage = json.loads(op[1])
        elif kind == "usage-mode":
            mode = op[1]
        elif kind == "status":
            status = op[1]
        elif kind == "exit":
            code = int(op[1])
        elif kind == "identity":
            identity_override = json.loads(op[1])
        elif kind == "deny-probe":
            detail["probes"][op[1]] = probe(op[1])
        elif kind == "forge-receipt":
            detail["forged_receipt"] = {"target": op[1], "observed": probe(op[1])}
            try:
                Path(op[1]).write_text(json.dumps({"forged": True, "status": "completed"}))
                detail["forged_receipt"]["overwrite"] = "allowed"
            except OSError as error:
                detail["forged_receipt"]["overwrite"] = errno.errorcode.get(error.errno, str(error.errno))
    envelope = {"schema": "corral-synthetic-v1", "synthetic": True, "status": status,
                "session_id": "fake-native-session-1", "num_turns": 1, "narrative": narrative,
                "usage": usage, "usage_mode": mode, "detail": detail,
                "identity": {**{"model": named.get("--model"), "effort": named.get("--effort"),
                                "harness": Path(sys.argv[0]).name, "provider": "fixture",
                                "account_ref": "fixture-account", "version": "0.0.0-fixture"},
                             **identity_override}}
    if not usage:
        envelope.pop("usage")
    print(json.dumps(envelope))
    return code


if __name__ == "__main__":
    sys.exit(main())
'''


def fake_profile() -> Profile:
    return Profile(id="fixture-native-high", model=FAKE_MODEL, effort=FAKE_EFFORT,
                   harness=FAKE_HARNESS, version="0.0.0-fixture", route=FAKE_ROUTE,
                   roles=("implementation", "review", "repair", "adjudication"),
                   tools=("read", "search", "edit", "shell", "test"), context=100000,
                   provider="fixture", family="fixture", account_ref="fixture-account")


def b64(text: str) -> str:
    import base64

    return base64.b64encode(text.encode()).decode()


def native_env(tmp_path: Path, *, launch_authorized: bool = False, synthetic: bool = True,
               supported_models=(FAKE_MODEL,), supported_efforts=(FAKE_EFFORT,),
               credential_env=(), runtime_read=(), runtime_home=None,
               envelope: str = "corral-synthetic-v1",
               harness: str = FAKE_HARNESS, route: str = FAKE_ROUTE,
               binary_name: str = FAKE_HARNESS, extra_host: dict | None = None):
    """Build a controller whose host declares one synthetic native route."""
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=f@example.invalid",
                    "commit", "--allow-empty", "-qm", "fixture"], cwd=workspace, check=True)
    (workspace / "math_ops.py").write_text("def add(a, b):\n    return 0\n")
    bin_dir = tmp_path / "host-bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    binary = bin_dir / binary_name
    binary.write_text(FAKE_NATIVE_CLI)
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    verifiers = tmp_path / "verifiers"
    verifiers.mkdir(parents=True, exist_ok=True)
    (verifiers / "check_candidate.py").write_text(
        '"""Controller-owned external verifier; never imports from the worker cwd."""\n'
        "import sys\n"
        "from pathlib import Path\n"
        "source = Path(sys.argv[1]).read_text()\n"
        "namespace = {}\n"
        "exec(compile(source, sys.argv[1], 'exec'), namespace)\n"
        "assert namespace['add'](2, 3) == 5, 'candidate does not implement add'\n")
    fake_home = tmp_path / "fake-harness-home"
    (fake_home / "auth").mkdir(parents=True, exist_ok=True)
    (fake_home / "auth" / "token.json").write_text('{"synthetic": true}')
    secrets = tmp_path / "host-secrets"
    secrets.mkdir(parents=True, exist_ok=True)
    (secrets / "provider.key").write_text("SYNTHETIC-DISPOSABLE-SENTINEL\n")
    grants = list(runtime_read) or [str(fake_home)]
    host = {
        "routes": [route], "harnesses": [harness], "cpu": 4, "memory_mb": 1024,
        "verifier_roots": [str(verifiers)],
        "protected_paths": [str(secrets), str(fake_home)],
        "native_routes": {route: {
            "harness": harness, "binary": str(binary), "envelope": envelope,
            "argv": ["--prompt", "{prompt_file}", "--model", "{model}", "--effort", "{effort}",
                     "--workspace", "{workspace}", "--scratch", "{scratch}",
                     "--result", "{result_file}"],
            "provider": "fixture", "account_ref": "fixture-account",
            "endpoint": "local-synthetic-fixture",
            "supported_models": list(supported_models), "supported_efforts": list(supported_efforts),
            "credential_env": list(credential_env), "runtime_env": ["CORRAL_FIXTURE=1"],
            "runtime_read": grants, "runtime_home": str(runtime_home or fake_home),
            "synthetic": synthetic,
            "launch_authorized": launch_authorized, "version": "0.0.0-fixture",
            "notes": "explicitly synthetic offline harness; never a live provider",
        }},
    }
    host.update(extra_host or {})
    controller = Controller(tmp_path / "state", "owner", {FAKE_HOST: host},
                            default_host=FAKE_HOST, profiles=[fake_profile()])
    return {"controller": controller, "workspace": workspace, "host": host, "binary": binary,
            "verifiers": verifiers, "fake_home": fake_home, "profile": fake_profile(),
            "secrets": secrets, "token": fake_home / "auth" / "token.json",
            "runtime_read": grants,
            "state": tmp_path / "state"}


def native_spec(env: dict, *, ops=(), objective: str = "Implement add(a, b) correctly.",
                candidate_paths=("math_ops.py",), **overrides) -> dict:
    spec = {
        "repo": "fixture-native", "workspace": str(env["workspace"]),
        "candidate_paths": list(candidate_paths), "verifier_paths": [],
        "verify": [sys.executable, str(env["verifiers"] / "check_candidate.py"), "math_ops.py"],
        "external_verifier": True,
        "selection": {"profile": dataclasses.asdict(env["profile"])},
        "objective": "\n".join([objective, *[op if op.startswith("@op ") else "@op " + op for op in ops]]),
        "operational_wait_seconds": 120,
    }
    spec.update(overrides)
    return spec


def write_ops(*ops: str) -> str:
    return "\n".join(op if op.startswith("@op ") else "@op " + op for op in ops)


def adapter_result(run_result: dict) -> dict:
    path = Path(run_result["result"]["artifact_directory"]) / "adapter-result.json"
    return json.loads(path.read_text())


def attempts(run_result: dict) -> list[dict]:
    path = Path(run_result["result"]["artifact_directory"]) / "attempts.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
