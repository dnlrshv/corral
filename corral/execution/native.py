"""Native dispatch preparation: trusted launch plan plus demonstrated worker boundary.

Everything the worker may touch is decided here, in the controller process, from
controller-owned configuration. The submitted spec contributes the objective and the
candidate/verifier declarations only: it can never name a binary, an endpoint, an account,
a credential or a writable root. Containment is demonstrated by a real sandboxed probe
against controller-created sentinels before any harness is allowed to start, and
preparation fails closed otherwise.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from . import containment, routes
from .adapter import build_adapter_command

SENTINEL_NAME = ".corral-containment-sentinel"


@dataclass
class Prepared:
    command: list[str]
    run_cwd: str
    env: dict[str, str]
    boundary: containment.Boundary
    demonstration: dict
    plan: dict
    evidence: dict = field(default_factory=dict)


def _scratch_root(state_dir: Path) -> Path:
    root = Path(state_dir) / "scratch"
    root.mkdir(parents=True, exist_ok=True)
    return root


def build_boundary(*, workspace: str, state_dir: Path, artifacts: Path, task_dir: Path,
                   source_root: Path, verifier_roots: tuple[str, ...] = (),
                   host_protected: tuple[str, ...] = (), route_read: tuple[str, ...] = (),
                   task_id: str) -> tuple[containment.Boundary, Path, list[str]]:
    """Deny controller state, artifacts, source, verifier bundles and account stores.

    Disposable sentinels are created inside already-denied trusted state so the probe can
    prove read *and* write denial with a kernel errno, without opening a real credential.
    """
    state_dir = Path(state_dir)
    artifacts = Path(artifacts)
    task_dir = Path(task_dir)
    scratch_root = _scratch_root(state_dir)
    scratch = scratch_root / task_id
    scratch.mkdir(parents=True, exist_ok=True)
    task_dir.mkdir(parents=True, exist_ok=True)
    denied = {
        str(state_dir.resolve()),
        str(artifacts.resolve()),
        str(Path(source_root).resolve()),
        str(scratch_root.resolve()),
        *{str(Path(item).resolve()) for item in verifier_roots},
        *{str(Path(item).expanduser().resolve()) for item in host_protected},
        *containment.sensitive_account_stores(),
    }
    granted = {str(Path(item).expanduser().resolve()) for item in route_read if item}
    # A grant is a narrow read-only exception that must match one denied root exactly. The
    # denial itself stays in the profile: Seatbelt applies the last matching rule, so the
    # audited text keeps "deny this store" plus "except this route's own credential home".
    auth_read_granted = sorted(granted & denied)
    sentinels = (
        containment.write_sentinel(state_dir / "sentinels" / f"{task_id}.controller-state",
                                   "controller state store"),
        containment.write_sentinel(task_dir / SENTINEL_NAME, "trusted adapter task directory"),
        containment.write_sentinel(scratch_root / f"{task_id}-sibling{SENTINEL_NAME}",
                                   "sibling task scratch"),
    )
    boundary = containment.Boundary(workspace=str(Path(workspace).resolve()),
                                    scratch=str(scratch.resolve()), tmpdir=str(scratch.resolve()),
                                    deny=tuple(sorted(denied)), allow=tuple(sorted(granted)),
                                    sentinels=sentinels)
    return boundary, scratch, auth_read_granted


def prepare(*, spec: dict, host: dict, profile, task_dir: Path, workspace: str, state_dir: Path,
            artifacts: Path, source_root: Path, task_id: str, verifier_roots: tuple[str, ...],
            usage_path: Path, context_path: Path) -> Prepared:
    """Resolve the trusted route, demonstrate containment and build the adapter command."""
    declared = routes.declared_routes(host)
    route = declared.get(profile.route)
    if route is None:
        raise PermissionError(f"no trusted native route declaration for {profile.route!r}")
    forbidden = (str(Path(workspace).resolve()), str(Path(task_dir).resolve()),
                 str(Path(state_dir).resolve()), str(Path(artifacts).resolve()))
    plan = routes.plan(route, profile, host_routes=tuple(host.get("routes") or ()),
                       forbidden_roots=forbidden)
    boundary, scratch, auth_read_granted = build_boundary(
        workspace=workspace, state_dir=Path(state_dir), artifacts=artifacts, task_dir=task_dir,
        source_root=source_root, verifier_roots=verifier_roots,
        host_protected=tuple(host.get("protected_paths") or ()),
        route_read=route.runtime_read, task_id=task_id)
    overlaps = containment.refuse_overlaps(boundary, scratch_root=str(_scratch_root(Path(state_dir))))
    if overlaps:
        raise PermissionError("worker boundary configuration overlaps trusted state: " + "; ".join(overlaps))
    demonstration = containment.require(boundary)
    (task_dir / "boundary.json").write_text(json.dumps(
        {"boundary": boundary.as_dict(), "profile_digest": demonstration.get("profile_digest"),
         "auth_read_granted": auth_read_granted,
         "containment_scope": {"contained": demonstration.get("contained_operations"),
                               "not_contained": demonstration.get("not_contained"),
                               "isolation_claim": demonstration.get("isolation_claim")},
         "note": "read is default-allow with curated credential/controller denials; write is default-deny"},
        indent=2, sort_keys=True))
    (task_dir / "launch-plan.json").write_text(json.dumps(plan.as_dict(), indent=2, sort_keys=True))
    command = build_adapter_command(task_dir=task_dir, workspace=workspace, source=source_root)
    env = {"CORRAL_CONTEXT_PATH": str(context_path), "CORRAL_USAGE_PATH": str(usage_path)}
    for name in plan.credential_env:
        value = os.environ.get(name)
        if value:
            # Value is forwarded in-process only; the name is the only part ever recorded.
            env[name] = value
    evidence = {**plan.evidence, "binary": plan.binary,
                "adapter_command": command, "adapter_cwd": str(task_dir),
                "containment": {"passed": bool(demonstration.get("passed")),
                                "profile_digest": demonstration.get("profile_digest"),
                                "checks": demonstration.get("checks"),
                                "sentinels": demonstration.get("sentinels"),
                                "not_contained": demonstration.get("not_contained"),
                                "isolation_claim": demonstration.get("isolation_claim"),
                                "blocker": demonstration.get("blocker")},
                "auth_read_granted": auth_read_granted, "scratch": str(scratch),
                "credential_env_present": sorted(name for name in plan.credential_env if env.get(name)),
                "credential_env_missing": sorted(name for name in plan.credential_env if not env.get(name))}
    return Prepared(command=command, run_cwd=str(task_dir), env=env, boundary=boundary,
                    demonstration=demonstration, plan=plan.as_dict(), evidence=evidence)
