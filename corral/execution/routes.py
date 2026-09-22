"""Trusted native route bindings: harness executable, argv contract and envelope schema.

A route is controller/host-owned configuration. Nothing in a submitted task spec may
name a binary, an endpoint, an account or a model: the spec only selects a registered
profile id, and the profile's route must be declared and authorized by the host. Live
(non-synthetic) routes stay refused until the host explicitly authorizes launch.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

PLACEHOLDERS: tuple[str, ...] = (
    "{workspace}",
    "{scratch}",
    "{prompt_file}",
    "{model}",
    "{effort}",
    "{result_file}",
    "{schema_file}",
    "{log_file}",
)

ENVELOPE_SCHEMAS: tuple[str, ...] = (
    "corral-synthetic-v1",
    "agy-json-v1",
    "codex-jsonl-v1",
    "qwen-code-stream-v1",
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
    runtime_home: str | None = None
    synthetic: bool = False
    launch_authorized: bool = False
    version: str | None = None
    notes: str = ""

    def as_dict(self) -> dict:
        value = {name: getattr(self, name) for name in self.__dataclass_fields__}
        value["argv"] = list(self.argv)
        for name in ("supported_models", "supported_efforts", "credential_env", "runtime_env",
                 "runtime_read"):
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
        credential_env=_as_tuple(raw.get("credential_env")),
        runtime_env=_as_tuple(raw.get("runtime_env")),
        runtime_read=_as_tuple(raw.get("runtime_read")),
        runtime_home=(str(raw["runtime_home"]) if raw.get("runtime_home") else None),
        synthetic=bool(raw.get("synthetic", False)),
        launch_authorized=bool(raw.get("launch_authorized", False)),
        version=raw.get("version"),
        notes=str(raw.get("notes") or ""),
    )


def _placeholders(item: str) -> list[str]:
    return [token for token in str(item).split() if token.startswith("{") and token.endswith("}")] \
        if "{" in str(item) else []


def declared_routes(host: dict) -> dict[str, NativeRoute]:
    raw_routes = host.get("native_routes") or {}
    if not isinstance(raw_routes, dict):
        raise PermissionError("host native_routes must be a mapping of route id to declaration")
    return {route_id: declare(route_id, raw) for route_id, raw in raw_routes.items()}


def resolve_binary(route: NativeRoute, *, forbidden_roots: tuple[str, ...] = ()) -> str:
    """Resolve the harness executable and refuse worker-writable or missing locations."""
    candidate = Path(route.binary).expanduser()
    if candidate.is_absolute():
        resolved = candidate
    else:
        found = shutil.which(route.binary)
        # `str(Path(""))` is ".", so an `or ""` fallback silently resolves to the cwd and the
        # not-installed refusal never fires. A missing harness is a refusal, not a guess.
        if not found:
            raise PermissionError(f"native route {route.id} binary is not installed: {route.binary}")
        resolved = Path(found)
    real = Path(os.path.realpath(str(resolved)))
    if not real.is_file():
        raise PermissionError(f"native route {route.id} binary is not a regular file: {real}")
    for root in forbidden_roots:
        root_path = Path(root).resolve()
        if real == root_path or root_path in real.parents:
            raise PermissionError(
                f"native route {route.id} binary must not live in a worker-writable path: {real} under {root_path}")
    return str(real)


def authorize(route: NativeRoute, profile, *, host_routes: tuple[str, ...]) -> None:
    """Refuse unregistered routes, unsupported model/effort pins and unauthorized launch."""
    if route.id not in set(host_routes):
        raise PermissionError(f"native route {route.id} is not authorized for this execution host")
    if profile.harness != route.harness:
        raise PermissionError(
            f"profile {profile.id} harness {profile.harness!r} does not match route harness {route.harness!r}")
    if profile.model not in route.supported_models:
        raise PermissionError(
            f"native route {route.id} does not serve model {profile.model!r}; no silent substitution")
    if profile.effort not in route.supported_efforts:
        raise PermissionError(
            f"native route {route.id} does not serve effort {profile.effort!r} for model {profile.model!r}")
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

    def as_dict(self) -> dict:
        return {"route": self.route.as_dict(), "binary": self.binary, "argv": list(self.argv),
                "model": self.model, "effort": self.effort, "home": self.home,
                "credential_env": list(self.credential_env), "evidence": self.evidence}


def plan(route: NativeRoute, profile, *, host_routes: tuple[str, ...],
         forbidden_roots: tuple[str, ...] = ()) -> LaunchPlan:
    authorize(route, profile, host_routes=host_routes)
    binary = resolve_binary(route, forbidden_roots=forbidden_roots)
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
        "notes": route.notes,
    }
    return LaunchPlan(route=route, binary=binary, argv=route.argv, model=profile.model,
                      effort=profile.effort, credential_env=route.credential_env,
                      home=route.runtime_home, evidence=evidence)
