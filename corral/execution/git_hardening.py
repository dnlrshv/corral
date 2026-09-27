"""Shared Git command and environment controls for trusted repository operations."""
from __future__ import annotations

import os

from . import credential_env

# Hooks, fsmonitor, external diff, user attributes/ignores and maintenance stay disabled.
# The empty credential.helper clears helpers inherited from repository configuration.
HARDENING = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
             "-c", "core.untrackedCache=false", "-c", "core.attributesFile=/dev/null",
             "-c", "core.excludesFile=/dev/null", "-c", "diff.external=",
             "-c", "gc.auto=0", "-c", "maintenance.auto=false",
             "-c", "credential.helper=")
# The service's own SSH transport settings are the only inherited Git variables.
INHERITED_GIT_ENV = frozenset({"GIT_SSH", "GIT_SSH_COMMAND", "GIT_SSH_VARIANT"})
LOCAL_TIMEOUT_SECONDS = 300


def git_environment(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Drop repository-redirecting Git variables and system/global configuration."""
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("GIT_") or key in INHERITED_GIT_ENV}
    # Git can reach configuration we do not control, so it gets no inherited credentials;
    # the SSH agent stays for the service's own transport, and callers pass tokens in `extra`.
    env = credential_env.scrub(env, ("SSH_AUTH_SOCK",))
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_ATTR_NOSYSTEM="1",
               GIT_NO_REPLACE_OBJECTS="1", GIT_NO_LAZY_FETCH="1", GIT_TERMINAL_PROMPT="0",
               GIT_OPTIONAL_LOCKS="0")
    env.update(extra or {})
    return env
