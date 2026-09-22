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
from dataclasses import dataclass, field
from pathlib import Path

from . import containment, inspection_packet, routes, verifier_containment
from .adapter import build_adapter_command

SENTINEL_NAME = ".corral-containment-sentinel"
_RUNTIME_WRITE_SECRET_MARKERS = ("auth", "credential", "key", "oauth", "publisher", "token")


@dataclass
class Prepared:
    command: list[str]
    run_cwd: str
    env: dict[str, str]
    boundary: containment.Boundary
    demonstration: dict
    plan: dict
    evidence: dict = field(default_factory=dict)
    verifier_profile: str | None = None
    verifier_evidence: dict | None = None
    verifier_env: dict[str, str] | None = None


def _scratch_root(state_dir: Path) -> Path:
    root = Path(state_dir) / "scratch"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _runtime_file_is_sensitive(path: Path) -> bool:
    return any(marker in part.lower() for part in path.parts
               for marker in _RUNTIME_WRITE_SECRET_MARKERS)


def _inspection_self_code(source_root: Path) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Return the exact installed modules needed by the packet-only transport.

    This is controller-owned code, not a host route grant: the inspection worker keeps the
    candidate and all non-Corral source paths denied.  ``-I -m`` needs the installed Corral
    package directory for Python's import machinery and the exact transitive transport files.
    """
    from corral import __file__ as corral_init
    from corral.execution import __file__ as execution_init

    from . import inspection_packet, inspection_transport, store, workspace
    from corral import redaction

    root = source_root.resolve()
    modules = (corral_init, execution_init, inspection_transport.__file__,
               inspection_packet.__file__, store.__file__, workspace.__file__, redaction.__file__)
    files = {Path(item).resolve() for item in modules if item}
    invalid = [str(item) for item in files
               if not item.is_file() or item.is_symlink() or not item.is_relative_to(root)]
    if invalid:
        raise PermissionError("inspection transport self-code is not a regular installed module: "
                              + ", ".join(sorted(invalid)))
    directories = {root, root / "corral", root / "corral" / "execution"}
    invalid_dirs = [str(item) for item in directories
                    if not item.is_dir() or item.is_symlink() or not item.is_relative_to(root)]
    if invalid_dirs:
        raise PermissionError("inspection transport package directories are invalid: "
                              + ", ".join(sorted(invalid_dirs)))
    return (tuple(sorted(str(item) for item in files)),
            tuple(sorted(str(item) for item in directories)),
            (str((root / "corral").resolve()),))


def build_boundary(*, workspace: str, state_dir: Path, artifacts: Path, task_dir: Path,
                   source_root: Path, verifier_roots: tuple[str, ...] = (),
                   host_protected: tuple[str, ...] = (), route_read: tuple[str, ...] = (),
                   route_write: tuple[str, ...] = (), route_write_files: tuple[str, ...] = (),
                   task_id: str, packet_only: bool = False) -> tuple[containment.Boundary, Path, list[str]]:
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
    if packet_only:
        denied.add(str(Path(workspace).resolve()))
    granted = {str(Path(item).expanduser().resolve()) for item in route_read if item}
    writable_files = {Path(item).expanduser().resolve() for item in route_write_files if item}
    invalid_file_grants = [str(item) for item in writable_files
                           if (item.exists() and (not item.is_file() or item.is_symlink()))
                           or not item.parent.is_dir()
                           or _runtime_file_is_sensitive(item)
                           or not any(item != Path(grant) and item.is_relative_to(Path(grant))
                                      for grant in granted)]
    if invalid_file_grants:
        raise PermissionError("runtime writable files must be regular paths below a declared read root: "
                              + ", ".join(sorted(invalid_file_grants)))
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
    worker_workspace = scratch if packet_only else Path(workspace).resolve()
    trusted_read_allow, trusted_metadata_allow, trusted_read_roots = (
        _inspection_self_code(Path(source_root)) if packet_only else ((), (), ()))
    boundary = containment.Boundary(workspace=str(worker_workspace),
                                    scratch=str(scratch.resolve()), tmpdir=str(scratch.resolve()),
                                    deny=tuple(sorted(denied)), allow=tuple(sorted(granted)),
                                    write_allow=tuple(sorted({str(Path(item).expanduser().resolve()) for item in route_write if item})),
                                    write_file_allow=tuple(sorted(str(item) for item in writable_files)),
                                    trusted_read_allow=trusted_read_allow,
                                    trusted_metadata_allow=trusted_metadata_allow,
                                    trusted_read_roots=trusted_read_roots,
                                    sentinels=sentinels)
    return boundary, scratch, auth_read_granted


def prepare(*, spec: dict, host: dict, profile, task_dir: Path, workspace: str, state_dir: Path,
            artifacts: Path, source_root: Path, task_id: str, verifier_roots: tuple[str, ...],
            usage_path: Path, context_path: Path,
            credential_values: dict[str, str] | None = None, attempt: str | None = None) -> Prepared:
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
        route_read=route.runtime_read, route_write=route.runtime_write,
        route_write_files=route.runtime_write_files, task_id=task_id,
        packet_only=route.inspection_only)
    overlaps = containment.refuse_overlaps(boundary, scratch_root=str(_scratch_root(Path(state_dir))))
    if overlaps:
        raise PermissionError("worker boundary configuration overlaps trusted state: " + "; ".join(overlaps))
    demonstration = containment.require(boundary)
    verifier_prepared = None
    if not route.inspection_only:
        if not attempt:
            raise PermissionError("native verifier preparation requires an attempt identity")
        verifier_prepared = verifier_containment.prepare(
            workspace=workspace, state_dir=state_dir, artifacts=artifacts, task_dir=task_dir,
            task_id=task_id, attempt=attempt,
            protected_paths=tuple(host.get("protected_paths") or ()),
            probe_sentinels=tuple(host.get("verifier_probe_sentinels") or ()))
    packet_record = None
    if route.inspection_only:
        context = json.loads(Path(context_path).read_text())
        candidate_binding = inspection_packet.bind_candidate(
            spec, workspace, context.get("workspace_provenance"))
        packet = inspection_packet.build(spec, context, workspace, candidate_binding)
        packet_path = inspection_packet.persist(packet, scratch)
        packet_record = {"path": str(packet_path), "digest": packet["digest"],
                         "task": packet["task"], "attempt": packet["attempt"],
                         "generation": packet["generation"],
                         "documents": [{"path": item["path"], "kind": item["kind"],
                                        "sha256": item["sha256"], "bytes": item["bytes"]}
                                       for item in packet["documents"]],
                         "capability": packet["capability"], "provenance": packet["provenance"]}
    (task_dir / "boundary.json").write_text(json.dumps(
        {"boundary": boundary.as_dict(), "profile_digest": demonstration.get("profile_digest"),
         "auth_read_granted": auth_read_granted,
         "containment_scope": {"contained": demonstration.get("contained_operations"),
                               "not_contained": demonstration.get("not_contained"),
                               "isolation_claim": demonstration.get("isolation_claim")},
         "note": ("inspection route receives only a copied packet and cannot read the candidate workspace"
                  if route.inspection_only else
                  "read is default-allow with curated credential/controller denials; write is default-deny")},
        indent=2, sort_keys=True))
    if verifier_prepared is not None:
        (task_dir / "verifier-boundary.json").write_text(json.dumps(
            {"boundary": verifier_prepared.boundary.as_dict(),
             "evidence": verifier_prepared.evidence}, indent=2, sort_keys=True))
    plan_dict = plan.as_dict()
    if packet_record:
        plan_dict["inspection_packet"] = packet_record
    (task_dir / "launch-plan.json").write_text(json.dumps(plan_dict, indent=2, sort_keys=True))
    command = build_adapter_command(task_dir=task_dir, workspace=workspace, source=source_root)
    env = {"CORRAL_CONTEXT_PATH": str(context_path), "CORRAL_USAGE_PATH": str(usage_path)}
    from .secret_env import select
    # Values are forwarded in-process only; only names are recorded in evidence.
    env.update(select(plan.credential_env, credential_values))
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
                "inspection_packet": packet_record,
                "credential_env_present": sorted(name for name in plan.credential_env if env.get(name)),
                "credential_env_missing": sorted(name for name in plan.credential_env if not env.get(name))}
    return Prepared(command=command, run_cwd=str(task_dir), env=env, boundary=boundary,
                    demonstration=demonstration, plan=plan_dict, evidence=evidence,
                    verifier_profile=verifier_prepared.profile if verifier_prepared else None,
                    verifier_evidence=verifier_prepared.evidence if verifier_prepared else None,
                    verifier_env=({"TMPDIR": verifier_prepared.boundary.tmpdir,
                                   "HOME": verifier_prepared.boundary.scratch}
                                  if verifier_prepared else None))
