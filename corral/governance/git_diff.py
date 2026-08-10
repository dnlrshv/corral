"""Read-only git plumbing for base-versus-head governance evaluation."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GitRepository:
    root: Path

    def run(self, args: list[str], *, allow_missing: bool = False) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=self.root,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            return result.stdout
        if allow_missing:
            return ""
        raise RuntimeError(result.stderr.strip() or f"git {' '.join(args)} failed")

    def changed_paths(self, base_ref: str, head_ref: str) -> list[str]:
        output = self.run(["diff", "--name-only", f"{base_ref}...{head_ref}"])
        return sorted(path for path in output.splitlines() if path)

    def read_ref_file(self, ref: str, path: str) -> str:
        return self.run(["show", f"{ref}:{path}"], allow_missing=True)

    def ref_reader(self, ref: str) -> Callable[[str], str | None]:
        def read(path: str) -> str | None:
            text = self.read_ref_file(ref, path)
            return text if text else None

        return read

    def added_lines_by_file(
        self, base_ref: str, head_ref: str, paths: list[str]
    ) -> dict[str, list[str]]:
        if not paths:
            return {}
        output = self.run(
            ["diff", "--unified=0", f"{base_ref}...{head_ref}", "--", *paths]
        )
        added: dict[str, list[str]] = {}
        current: str | None = None
        for line in output.splitlines():
            if line.startswith("+++ b/"):
                current = line[len("+++ b/") :]
            elif line.startswith("+++ "):
                current = None
            elif current and line.startswith("+") and not line.startswith("+++"):
                added.setdefault(current, []).append(line[1:])
        return added
