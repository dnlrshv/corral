"""Maintained argv contracts for full-capability coding workers.

Host profiles supply the executable, credential environment, provider configuration and
launch authorization. These argv fragments deliberately contain no account paths, tokens,
or artificial token/turn/time caps.
"""
from __future__ import annotations

import re


def argv_for(harness: str, *, model_provider: str | None = None) -> list[str]:
    """Return supported argv for a controller-declared coding harness.

    ``model_provider`` is a trusted host-route identifier, never task input. Codex's
    documented ``-c`` override binds both its provider and reasoning effort without
    inventing an unsupported ``--effort`` command-line flag.
    """
    if harness == "agy":
        return ["--add-dir", "{workspace}", "--model", "{model}", "--effort", "{effort}",
                "--output-format", "json", "--print-timeout", "0",
                "--dangerously-skip-permissions", "--print", "{prompt}"]
    if harness == "codex":
        if not isinstance(model_provider, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", model_provider):
            raise PermissionError("Codex coding route requires a trusted model_provider identifier")
        return ["exec", "--json", "--sandbox", "workspace-write", "-C", "{workspace}",
                "--model", "{model}", "-c", f'model_provider="{model_provider}"',
                "-c", 'model_reasoning_effort="{effort}"',
                "--output-last-message", "{result_file}", "{prompt}"]
    raise PermissionError(f"unsupported coding harness {harness!r}")
