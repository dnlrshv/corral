"""Minimum complete schema needed to bind authenticated policy API observations."""

import re
from typing import Any


def validate_file(
    value: Any, *, workflow: bool = False, expected_path: str | None = None
) -> None:
    if not isinstance(value, dict):
        raise ValueError("Malformed policy source entry")
    sha, path = value.get("sha"), value.get("path")
    if not isinstance(sha, str) or not re.fullmatch("[0-9a-f]{40}", sha):
        raise ValueError("Policy source requires complete blob SHA")
    if not isinstance(path, str) or not path or value.get("type", "file") != "file":
        raise ValueError("Policy source must be a file with a path")
    if expected_path is not None and path != expected_path:
        raise ValueError("Policy source path mismatch")
    if workflow:
        name = value.get("name")
        if not isinstance(name, str) or not name or path != ".github/workflows/" + name:
            raise ValueError("Malformed workflow source entry")
    elif type(value.get("size")) is not int or value["size"] < 0:
        raise ValueError("Policy source requires nonnegative integer size")


def validate_ruleset(value: dict[str, Any], expected_id: int) -> None:
    if (
        value.get("id") != expected_id
        or not isinstance(value.get("name"), str)
        or not value["name"]
        or value.get("target") not in ("branch", "tag", "push")
        or value.get("enforcement") not in ("active", "evaluate", "disabled")
        or not isinstance(value.get("conditions"), dict)
        or not isinstance(value.get("rules"), list)
    ):
        raise ValueError("Incomplete ruleset detail")
    for rule in value["rules"]:
        if (
            not isinstance(rule, dict)
            or not isinstance(rule.get("type"), str)
            or not rule["type"]
            or not isinstance(rule.get("parameters", {}), dict)
        ):
            raise ValueError("Malformed ruleset rule")
    bypass = value.get("bypass_actors", [])
    if not isinstance(bypass, list) or any(
        not isinstance(actor, dict) for actor in bypass
    ):
        raise ValueError("Malformed ruleset bypass actors")


def validate_protection(value: dict[str, Any]) -> None:
    """HTTP 200 error/empty objects must never become an absence observation."""
    fields = {
        "required_status_checks", "required_pull_request_reviews", "enforce_admins",
        "required_signatures", "required_linear_history", "allow_force_pushes",
        "allow_deletions", "block_creations", "required_conversation_resolution",
        "lock_branch", "allow_fork_syncing", "restrictions",
    }
    present = fields & value.keys()
    if not present or not any(isinstance(value[k], dict) and value[k] for k in present):
        raise ValueError("Malformed HTTP 200 branch protection response")
    if any(value[k] is not None and not isinstance(value[k], dict) for k in present):
        raise ValueError("Malformed branch protection field")
