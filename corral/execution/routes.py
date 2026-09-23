"""Trusted native route bindings: harness executable, argv contract and envelope schema.

A route is controller/host-owned configuration. Nothing in a submitted task spec may
name a binary, an endpoint, an account or a model: the spec only selects a registered
profile id, and the profile's route must be declared and authorized by the host. Live
(non-synthetic) routes stay refused until the host explicitly authorizes launch.
"""
from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

PLACEHOLDERS: tuple[str, ...] = (
    "{workspace}",
    "{scratch}",
    "{prompt_file}",
    "{prompt}",
    "{model}",
    "{effort}",
    "{result_file}",
    "{schema_file}",
    "{log_file}",
    "{packet_file}",
)

ENVELOPE_SCHEMAS: tuple[str, ...] = (
    "corral-synthetic-v1",
    "agy-json-v1",
    "codex-jsonl-v1",
    "qwen-code-stream-v1",
    "corral-inspection-report-v1",
)


@dataclass(frozen=True)
class NativeRoute:
    id: str
    harness: str
    binary: str
    argv: tuple[str, ...]
    envelope: str
    provider: str
    account_ref: str
    endpoint: str
    supported_models: tuple[str, ...]
    supported_efforts: tuple[str, ...]
    credential_env: tuple[str, ...] = ()
    runtime_env: tuple[str, ...] = ()
    runtime_read: tuple[str, ...] = ()
    runtime_write: tuple[str, ...] = ()
    runtime_write_files: tuple[str, ...] = ()
    runtime_home: str | None = None
    synthetic: bool = False
    launch_authorized: bool = False
    version: str | None = None
    notes: str = ""
    inspection_only: bool = False

    def as_dict(self) -> dict:
        value = {name: getattr(self, name) for name in self.__dataclass_fields__}
        value["argv"] = list(self.argv)
        for name in ("supported_models", "supported_efforts", "credential_env", "runtime_env",
                 "runtime_read", "runtime_write", "runtime_write_files"):
            value[name] = list(value[name])
        return value


def _as_tuple(value) -> tuple:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(value)


def declare(route_id: str, raw: dict) -> NativeRoute:
    """Validate one host-declared route entry, failing closed on any gap."""
    if not isinstance(raw, dict):
        raise PermissionError(f"native route {route_id} must be a declaration object")
    binary = raw.get("binary")
    argv = _as_tuple(raw.get("argv"))
    envelope = raw.get("envelope")
    if not isinstance(binary, str) or not binary:
        raise PermissionError(f"native route {route_id} requires an explicit binary")
    if not argv:
        raise PermissionError(f"native route {route_id} requires an explicit argv contract")
    if envelope not in ENVELOPE_SCHEMAS:
        raise PermissionError(f"native route {route_id} declares unsupported envelope schema {envelope!r}")
    unknown = sorted({token for item in argv for token in _placeholders(item)} - set(PLACEHOLDERS))
    if unknown:
        raise PermissionError(f"native route {route_id} uses unsupported placeholders: {unknown}")
    models = _as_tuple(raw.get("supported_models"))
    if not models:
        raise PermissionError(f"native route {route_id} must pin the models it actually serves")
    inspection_only = bool(raw.get("inspection_only", False))
    runtime_env = _as_tuple(raw.get("runtime_env"))
    runtime_read = _as_tuple(raw.get("runtime_read"))
    runtime_write = _as_tuple(raw.get("runtime_write"))
    runtime_write_files = _as_tuple(raw.get("runtime_write_files"))
    runtime_home = str(raw["runtime_home"]) if raw.get("runtime_home") else None
    credential_env = _as_tuple(raw.get("credential_env"))
    if inspection_only:
        forbidden = {"{workspace}", "{scratch}", "{prompt_file}", "{prompt}",
                     "{schema_file}", "{log_file}"}
        used = {token for item in argv for token in _placeholders(item)}
        if used & forbidden:
            raise PermissionError("inspection-only route may receive only packet/model/effort/result placeholders")
        required = {"{packet_file}", "{result_file}", "{model}", "{effort}"}
        if not required.issubset(used):
            raise PermissionError("inspection-only route must bind packet, result, model, and effort")
        if envelope != "corral-inspection-report-v1":
            raise PermissionError("inspection-only route requires the inspection report envelope")
        if runtime_env or runtime_read or runtime_write or runtime_write_files or runtime_home:
            raise PermissionError("inspection-only route cannot declare runtime hooks, homes, or file grants")
        if len(credential_env) != 1:
            raise PermissionError("inspection-only route requires exactly one whitelisted credential variable")
    return NativeRoute(
        id=route_id,
        harness=str(raw.get("harness") or route_id),
        binary=binary,
        argv=tuple(str(item) for item in argv),
        envelope=envelope,
        provider=str(raw.get("provider") or "unknown"),
        account_ref=str(raw.get("account_ref") or "unknown"),
        endpoint=str(raw.get("endpoint") or "unknown"),
        supported_models=models,
        supported_efforts=_as_tuple(raw.get("supported_efforts")),
        credential_env=credential_env,
        runtime_env=runtime_env,
        runtime_read=runtime_read,
        runtime_write=runtime_write,
        runtime_write_files=runtime_write_files,
        runtime_home=runtime_home,
        synthetic=bool(raw.get("synthetic", False)),
        launch_authorized=bool(raw.get("launch_authorized", False)),
        version=raw.get("version"),
        notes=str(raw.get("notes") or ""),
        inspection_only=inspection_only,
    )


def _placeholders(item: str) -> list[str]:
    """Return all braced placeholders so unsupported declarations fail closed."""
    return re.findall(r"\{[^{}]+\}", str(item))


def declared_routes(host: dict) -> dict[str, NativeRoute]:
    raw_routes = host.get("native_routes") or {}
    if not isinstance(raw_routes, dict):
        raise PermissionError("host native_routes must be a mapping of route id to declaration")
    return {route_id: declare(route_id, raw) for route_id, raw in raw_routes.items()}


#: Symlinks followed while resolving a route binary before the declaration is refused.
_SYMLINK_LIMIT = 40


def _symlink_walk(path: Path) -> tuple[list[Path], list[Path], Path]:
    """Resolve ``path`` as the kernel will at exec time, recording what it traverses.

    Returns the symlinks crossed, every location traversed (each with its parent directory
    already resolved) and the final resolved path. A worker that can write any traversed
    location (a symlink it could retarget, or a directory it could replace with one before a
    later ``..``) could change what is executed after validation, so each one is checked, not
    just the final file.
    """
    hops: list[Path] = []
    visited: list[Path] = []
    current = Path(path.anchor)
    pending = list(path.parts[1:])
    while pending:
        part = pending.pop(0)
        if part in ("", "."):
            continue
        if part == "..":
            current = current.parent
            continue
        candidate = current / part
        visited.append(candidate)
        if not candidate.is_symlink():
            current = candidate
            continue
        if len(hops) >= _SYMLINK_LIMIT:
            raise PermissionError(f"too many symbolic links resolving {path}")
        hops.append(candidate)
        target = Path(os.readlink(candidate))
        if target.is_absolute():
            current = Path(target.anchor)
            pending = list(target.parts[1:]) + pending
        else:
            pending = list(target.parts) + pending
    return hops, visited, current


def _resolve(route: NativeRoute, forbidden_roots: tuple[str, ...]) -> tuple[Path, Path, list[Path]]:
    candidate = Path(route.binary).expanduser()
    if candidate.is_absolute():
        declared = candidate
    else:
        found = shutil.which(route.binary)
        # `str(Path(""))` is ".", so an `or ""` fallback silently resolves to the cwd and the
        # not-installed refusal never fires. A missing harness is a refusal, not a guess.
        if not found:
            raise PermissionError(f"native route {route.id} binary is not installed: {route.binary}")
        declared = Path(os.path.abspath(found))
    try:
        hops, visited, real = _symlink_walk(declared)
    except OSError as error:
        raise PermissionError(f"native route {route.id} binary cannot be resolved: {declared}") from error
    if str(real) != os.path.realpath(str(declared)):
        raise PermissionError(f"native route {route.id} binary changed while it was resolved: {declared}")
    if not real.is_file():
        raise PermissionError(f"native route {route.id} binary is not a regular file: {real}")
    roots = [Path(root).resolve() for root in forbidden_roots]
    for location in (*visited, real):
        for root_path in roots:
            if location == root_path or root_path in location.parents:
                raise PermissionError(
                    f"native route {route.id} binary must not live in a worker-writable path: "
                    f"{location} under {root_path}")
    return declared, real, hops


def resolve_binary(route: NativeRoute, *, forbidden_roots: tuple[str, ...] = ()) -> str:
    """Return the declared harness path after validating everything it resolves through.

    The declared path, not its resolved target, is what the adapter executes. A virtual
    environment interpreter is a symlink to its base interpreter, and the environment is
    found next to the link (``pyvenv.cfg``): executing the target would run the base
    interpreter without the environment's packages. The final regular file and every
    symlink on the way to it are refused when they sit under a worker-writable root, so
    nothing a worker can retarget lies on the executed path.
    """
    return str(_resolve(route, forbidden_roots)[0])


def authorize(route: NativeRoute, profile, *, host_routes: tuple[str, ...]) -> None:
    """Refuse unregistered routes, unsupported model/effort pins and unauthorized launch."""
    if route.id not in set(host_routes):
        raise PermissionError(f"native route {route.id} is not authorized for this execution host")
    if profile.harness != route.harness:
        raise PermissionError(
            f"profile {profile.id} harness {profile.harness!r} does not match route harness {route.harness!r}")
    if profile.provider != route.provider or profile.account_ref != route.account_ref:
        raise PermissionError(
            f"profile {profile.id} provider/account binding does not match route {route.id}")
    if profile.model not in route.supported_models:
        raise PermissionError(
            f"native route {route.id} does not serve model {profile.model!r}; no silent substitution")
    if profile.effort not in route.supported_efforts:
        raise PermissionError(
            f"native route {route.id} does not serve effort {profile.effort!r} for model {profile.model!r}")
    if route.inspection_only and (set(profile.roles) != {"review"}
                                  or set(profile.tools) != {"inspect-packet", "report"}):
        raise PermissionError("inspection-only route requires a review-only, packet/report profile")
    if not route.synthetic and not route.launch_authorized:
        raise PermissionError(
            f"native route {route.id} is declared but live launch is not authorized on this host")


@dataclass
class LaunchPlan:
    """Fully resolved, trusted launch description handed to the adapter process."""

    route: NativeRoute
    binary: str
    argv: tuple[str, ...]
    model: str
    effort: str
    credential_env: tuple[str, ...]
    home: str | None = None
    evidence: dict = field(default_factory=dict)
    #: The regular file ``binary`` resolves to, recorded for audit; ``binary`` is executed.
    binary_realpath: str | None = None

    def as_dict(self) -> dict:
        return {"route": self.route.as_dict(), "binary": self.binary,
                "binary_realpath": self.binary_realpath, "argv": list(self.argv),
                "model": self.model, "effort": self.effort, "home": self.home,
                "credential_env": list(self.credential_env), "evidence": self.evidence}


def plan(route: NativeRoute, profile, *, host_routes: tuple[str, ...],
         forbidden_roots: tuple[str, ...] = ()) -> LaunchPlan:
    authorize(route, profile, host_routes=host_routes)
    declared, real, hops = _resolve(route, forbidden_roots)
    evidence = {
        "route": route.id,
        "harness": route.harness,
        "provider": route.provider,
        "account_ref": route.account_ref,
        "endpoint": route.endpoint,
        "envelope": route.envelope,
        "declared_version": route.version,
        "synthetic": route.synthetic,
        # Declared authorization is reported verbatim; permission to launch now is separate.
        "launch_authorized": route.launch_authorized,
        "launch_permitted": route.launch_authorized or route.synthetic,
        "requested_model": profile.model,
        "requested_effort": profile.effort,
        "credential_env_names": list(route.credential_env),
        "runtime_home": route.runtime_home,
        "runtime_read": list(route.runtime_read),
        "runtime_write": list(route.runtime_write),
        "runtime_write_files": list(route.runtime_write_files),
        "notes": route.notes,
        "inspection_only": route.inspection_only,
        "binary_realpath": str(real),
        "binary_symlinks": [str(item) for item in hops],
    }
    return LaunchPlan(route=route, binary=str(declared), argv=route.argv, model=profile.model,
                      effort=profile.effort, credential_env=route.credential_env,
                      home=route.runtime_home, evidence=evidence, binary_realpath=str(real))
