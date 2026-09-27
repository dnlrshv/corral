"""Version the public execution contract and the SQLite store.

Bump PROTOCOL_VERSION for any backward-incompatible change to execution JSON
requests or responses (including executor actions and service CLI JSON), CLI
flags or subcommands of corral-service, corral-agent or corral-execution-client,
or controller, route, profile or secret-env configuration that makes an existing
valid configuration invalid or changes its meaning. Additive optional fields
are not a protocol break.
"""

PROTOCOL_VERSION = 1
STORE_SCHEMA_VERSION = 1


def installed_commit() -> str | None:
    """Return the VCS commit recorded by an installed distribution, if present."""
    import json
    from importlib import metadata

    try:
        raw = metadata.distribution("corral").read_text("direct_url.json")
        direct_url = json.loads(raw) if raw else {}
        vcs = direct_url.get("vcs_info")
        commit = vcs.get("commit_id") if isinstance(vcs, dict) else None
        if (isinstance(direct_url.get("url"), str) and direct_url["url"]
                and isinstance(vcs, dict) and isinstance(vcs.get("vcs"), str)
                and vcs["vcs"] and isinstance(commit, str) and commit):
            return commit
        return None
    except Exception:
        return None


def check_min_protocol(request: dict) -> None:
    """Consume the optional compatibility gate before dispatching an action."""
    if "min_protocol" not in request:
        return
    minimum = request.pop("min_protocol")
    if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 0:
        raise ValueError("min_protocol must be a non-negative integer")
    if minimum > PROTOCOL_VERSION:
        raise ValueError(
            f"request requires protocol {minimum}; this Corral supports {PROTOCOL_VERSION}"
        )


def response_with_protocol(result):
    """Add the version to object responses without replacing an existing key."""
    if isinstance(result, dict) and "protocol" not in result:
        return {**result, "protocol": PROTOCOL_VERSION}
    return result
