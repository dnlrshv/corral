"""Hardened argv-only local command adapter."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from corral.retro.providers.base import (
    Availability,
    SeatResult,
    SeatRunner,
    SeatStatus,
    availability,
    result,
)
from corral.retro.seats import Seat

_ENV_ALLOWLIST = ("PATH", "HOME", "LANG")


def _environment(seat: Seat) -> dict[str, str]:
    names = [*_ENV_ALLOWLIST, *([seat.auth_env] if seat.auth_env else [])]
    return {name: os.environ[name] for name in names if name in os.environ}


def _argv(seat: Seat, prompt_path: Path | None = None) -> list[str]:
    prompt_file = str(prompt_path) if prompt_path is not None else ""
    # Substitute the controlled value first and the seat model LAST so model
    # text is never re-scanned for placeholders (a model id containing the
    # literal "{prompt_file}" must not be replaced by the temp path).
    return [
        part.replace("{prompt_file}", prompt_file).replace("{model}", seat.model)
        for part in seat.options["argv"]
    ]


class ShellSeatRunner(SeatRunner):
    """Run a local provider command without a shell or ambient environment."""

    def probe(self, seat: Seat) -> Availability:
        if seat.auth_env and not os.environ.get(seat.auth_env):
            return availability(
                seat,
                SeatStatus.UNAVAILABLE,
                f"credential environment variable {seat.auth_env} is not set",
            )
        argv = _argv(seat)
        path = os.environ.get("PATH")
        try:
            found = bool(argv) and shutil.which(argv[0], path=path) is not None
        except Exception as exc:
            return availability(seat, SeatStatus.UNAVAILABLE, f"command probe failed: {exc}")
        if not found:
            binary = argv[0] if argv else "(empty argv)"
            return availability(seat, SeatStatus.UNAVAILABLE, f"command not found: {binary}")
        return availability(seat, SeatStatus.OK)

    def complete(
        self,
        seat: Seat,
        prompt: str,
        *,
        timeout: float,
        max_tokens: int,
    ) -> SeatResult:
        del max_tokens  # Local commands own their token flags through argv.
        probe = self.probe(seat)
        if not probe.available:
            return result(seat, SeatStatus.UNAVAILABLE, detail=probe.detail)

        uses_prompt_file = any("{prompt_file}" in part for part in seat.options["argv"])
        try:
            with tempfile.TemporaryDirectory(prefix="corral-seat-") as tmp:
                prompt_path = Path(tmp) / "prompt.txt"
                if uses_prompt_file:
                    # 0o600 at creation (the 0o700 tempdir already isolates
                    # it); never leave the prompt world-readable.
                    handle = os.open(
                        prompt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                    )
                    with os.fdopen(handle, "w", encoding="utf-8") as prompt_file_handle:
                        prompt_file_handle.write(prompt)
                completed = subprocess.run(
                    _argv(seat, prompt_path),
                    input=None if uses_prompt_file else prompt,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                    shell=False,
                    env=_environment(seat),
                )
        except subprocess.TimeoutExpired:
            return result(seat, SeatStatus.TIMEOUT, detail=f"command timed out after {timeout}s")
        except OSError as exc:
            return result(seat, SeatStatus.ERROR, detail=f"command launch failed: {exc}")
        except Exception as exc:
            return result(seat, SeatStatus.ERROR, detail=f"command failed: {exc}")

        if completed.returncode != 0:
            detail = (completed.stderr or "").strip()
            suffix = f": {detail}" if detail else ""
            return result(
                seat,
                SeatStatus.ERROR,
                detail=f"command exited {completed.returncode}{suffix}",
            )
        return result(seat, SeatStatus.OK, text=completed.stdout.strip())


__all__ = ["ShellSeatRunner"]
