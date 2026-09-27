"""Credential-name filtering at seat process boundaries."""
from __future__ import annotations


def is_credential(name: str) -> bool:
    upper = name.upper()
    return (upper.startswith(("AWS_", "ANTHROPIC_", "OPENAI_"))
            or upper in {"GITHUB_TOKEN", "GH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"}
            or upper.endswith(("_TOKEN", "_API_KEY"))
            or "_SECRET" in upper or "PASSWORD" in upper)


def scrub(env: dict[str, str], declared: tuple[str, ...] | list[str] = ()) -> dict[str, str]:
    allowed = set(declared)
    return {name: value for name, value in env.items()
            if not is_credential(name) or name in allowed}
