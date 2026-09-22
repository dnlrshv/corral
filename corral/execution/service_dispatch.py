"""Nonblocking service dispatch with durable, observable launcher identity."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from .runtime_identity import launched


def launch(controller_config: Path, state_dir: Path, task_id: str, host: str,
           *, development_mode: bool = False) -> dict:
    log_path = state_dir / "service-dispatch.log"
    command = [sys.executable, *([] if development_mode else ["-I"]),
               "-m", "corral.execution.service_worker", "--config",
               str(controller_config), "--task", task_id, "--host", host]
    with log_path.open("ab") as log:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                   start_new_session=True)
    return {**launched(process.pid, str(Path(sys.executable).resolve()), host),
            "argv_module": "corral.execution.service_worker", "log": str(log_path.resolve())}
