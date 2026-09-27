"""Credential-name filtering at seat process boundaries."""
from __future__ import annotations


#: Names that grant access without looking like a key: agent sockets and credential files.
_EXACT = {"GITHUB_TOKEN", "GH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "SSH_AUTH_SOCK",
          "GPG_AGENT_INFO", "GOOGLE_APPLICATION_CREDENTIALS", "NETRC"}


def is_credential(name: str) -> bool:
    upper = name.upper()
    return (upper.startswith(("AWS_", "ANTHROPIC_", "OPENAI_"))
            or upper in _EXACT
            or upper.endswith(("_TOKEN", "_KEY", "_PAT", "_CREDENTIALS", "_PASSWD"))
            or "_SECRET" in upper or "PASSWORD" in upper)


def scrub(env: dict[str, str], declared: tuple[str, ...] | list[str] = ()) -> dict[str, str]:
    allowed = set(declared)
    return {name: value for name, value in env.items()
            if not is_credential(name) or name in allowed}
