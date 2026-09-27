"""Read-only checks for a service configuration and its existing store."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from corral.protocol import STORE_SCHEMA_VERSION

from . import routes
from .profiles import STANDARD_NATIVE_PROFILES, registered_profiles
from .secret_env import load as load_secret_env
from .service import load_service_config


def validate(config: str | Path) -> dict:
    errors: list[str] = []
    warnings: list[str] = []
    store = {"found": None, "supported": STORE_SCHEMA_VERSION, "compatible": True}
    try:
        _, service, _, controller = load_service_config(config)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return {"valid": False, "errors": [f"configuration: {exc}"],
                "warnings": warnings, "store": store}
    if not isinstance(service, dict) or not isinstance(controller, dict):
        return {"valid": False, "errors": ["service and controller configs must be objects"],
                "warnings": warnings, "store": store}

    hosts = controller.get("hosts")
    if not isinstance(hosts, dict):
        errors.append("controller hosts must be a mapping")
        hosts = {}
    declared = set()
    host_routes = {}
    for name, host in hosts.items():
        try:
            host_routes[name] = routes.declared_routes(host)
            declared.update(host_routes[name])
        except (AttributeError, TypeError, ValueError, PermissionError) as exc:
            errors.append(f"host {name}: {exc}")
    if not isinstance(controller.get("default_host"), str) or controller["default_host"] not in hosts:
        errors.append("controller default_host is not registered")

    try:
        profiles = registered_profiles(controller.get("profiles", ()))
    except (TypeError, ValueError, KeyError) as exc:
        errors.append(f"controller profiles: {exc}")
        profiles = []
    profile_ids = {profile.id for profile in profiles}
    standard_routes = {profile.route for profile in STANDARD_NATIVE_PROFILES} | {"deterministic"}
    for profile in profiles:
        if not isinstance(profile.route, str) or profile.route not in declared | standard_routes:
            errors.append(f"profile {profile.id} route {profile.route!r} is undeclared")
        if set(profile.roles) & {"implementation", "repair"}:
            for host_name, route_map in host_routes.items():
                route = route_map.get(profile.route)
                if route and not (route.allowed_repositories or route.allowed_workdirs):
                    warnings.append(f"host {host_name} write-capable route {route.id} has no scoping")
    warnings = sorted(set(warnings))
    profiles_by_id = {profile.id: profile for profile in profiles}

    repositories = service.get("repositories", {})
    if not isinstance(repositories, dict):
        errors.append("repositories must be a mapping")
        repositories = {}
    for name, repo in repositories.items():
        if not isinstance(repo, dict):
            errors.append(f"repository {name} must be a mapping")
            continue
        _check_hosts(repo, f"repository {name}", hosts, errors)
        _check_packet(repo, f"repository {name}", errors)
        defaults = repo.get("task_defaults") or {}
        if isinstance(defaults, dict):
            _check_packet(defaults, f"repository {name} task_defaults", errors)
            _check_route_reference(name, defaults.get("profile_id"), repo, host_routes,
                                   profiles_by_id, errors)
        for kind in ("review_policies", "repair_policies"):
            policies = repo.get(kind) or {}
            if not isinstance(policies, dict):
                errors.append(f"repository {name} {kind} must be a mapping")
                continue
            for policy_id, policy in policies.items():
                label = f"repository {name} {kind} {policy_id}"
                if not isinstance(policy, dict):
                    errors.append(f"{label} must be a mapping")
                    continue
                profile_id = policy.get("profile_id")
                if profile_id is not None and (not isinstance(profile_id, str)
                                               or profile_id not in profile_ids):
                    errors.append(f"{label} profile_id {profile_id!r} is not registered")
                _check_hosts(policy, label, hosts, errors)
                _check_packet(policy, label, errors)
                _check_route_reference(name, profile_id, repo, host_routes,
                                       profiles_by_id, errors)

    plans = service.get("wave_plans") or {}
    if isinstance(plans, dict):
        for plan_name, plan in plans.items():
            for task in plan.get("tasks", ()) if isinstance(plan, dict) else ():
                if not isinstance(task, dict):
                    continue
                repo_name = task.get("repository")
                repo = repositories.get(repo_name)
                if isinstance(repo, dict):
                    profile_id = task.get("profile_id", (repo.get("task_defaults") or {}).get("profile_id"))
                    _check_route_reference(repo_name, profile_id, repo, host_routes,
                                           profiles_by_id, errors)

    try:
        if int(service.get("max_dispatch_per_tick", 1)) < 1:
            errors.append("max_dispatch_per_tick must be positive")
    except (TypeError, ValueError):
        errors.append("max_dispatch_per_tick must be a positive integer")

    if controller.get("secret_env") is not None:
        try:
            load_secret_env(controller["secret_env"])
        except (OSError, ValueError, TypeError) as exc:
            errors.append(f"secret_env: {type(exc).__name__}: invalid private file")

    state = controller.get("state")
    if state is None:
        errors.append("controller state is required")
    else:
        try:
            path = Path(state) / "controller.sqlite"
            if path.exists():
                store["found"] = _store_version(path)
                store["compatible"] = store["found"] <= STORE_SCHEMA_VERSION
                if not store["compatible"]:
                    errors.append(f"store schema {store['found']} exceeds supported {STORE_SCHEMA_VERSION}")
        except (OSError, TypeError, ValueError, sqlite3.Error) as exc:
            store["compatible"] = False
            errors.append(f"store cannot be read: {exc}")

    return {"valid": not errors, "errors": errors, "warnings": warnings, "store": store}


def _check_packet(value: dict, label: str, errors: list[str]) -> None:
    if "max_packet_bytes" in value:
        limit = value["max_packet_bytes"]
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            errors.append(f"{label} max_packet_bytes must be a positive integer")


def _check_route_reference(name: str, profile_id, repo: dict, host_routes: dict,
                           profiles: dict, errors: list[str]) -> None:
    if not isinstance(profile_id, str):
        return
    profile = profiles.get(profile_id)
    if profile is None:
        return
    workspaces = repo.get("workspaces") or {}
    if not isinstance(workspaces, dict):
        errors.append(f"repository {name} workspaces must be a mapping")
        return
    for host_name in workspaces:
        route = host_routes.get(host_name, {}).get(profile.route)
        if route and route.allowed_repositories and name not in route.allowed_repositories:
            errors.append(f"repository {name} profile {profile_id} route {route.id} excludes it")


def _check_hosts(value: dict, label: str, hosts: dict, errors: list[str]) -> None:
    default = value.get("default_host")
    if default is not None and (not isinstance(default, str) or default not in hosts):
        errors.append(f"{label} default_host {default!r} is not registered")
    allowed = value.get("allowed_hosts")
    if allowed is not None:
        if not isinstance(allowed, (list, tuple)):
            errors.append(f"{label} allowed_hosts must be a list of host names")
        else:
            for host in allowed:
                if not isinstance(host, str) or host not in hosts:
                    errors.append(f"{label} allowed_host {host!r} is not registered")


def _store_version(path: Path) -> int:
    """Read the store's user_version without creating SQLite sidecar files.

    With a live write-ahead log the newest header may still be in the log, so SQLite
    reads it under its normal shared lock. Without one, the main file header is
    authoritative and is read directly: opening a WAL-mode database through SQLite
    would create -wal/-shm files, and ``immutable=1`` would skip locking against a
    live writer.
    """
    wal = Path(str(path) + "-wal")
    shm = Path(str(path) + "-shm")
    if wal.exists():
        if not shm.exists():
            raise OSError("WAL exists without its shared-memory file")
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
            return db.execute("PRAGMA user_version").fetchone()[0]
    with path.open("rb") as handle:
        header = handle.read(100)
    if len(header) == 0:
        return 0
    if len(header) < 100 or not header.startswith(b"SQLite format 3\x00"):
        raise ValueError("store is not a SQLite database")
    # user_version is the big-endian integer at offset 60 of the database header.
    return int.from_bytes(header[60:64], "big", signed=True)
