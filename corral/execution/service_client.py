"""Thin authenticated local/SSH client for the authoritative Corral service endpoint."""
from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path


class ServiceClient:
    def __init__(self, config: dict):
        self.config = config

    def call(self, action: str, **payload):
        cfg = self.config
        development = cfg.get("development_mode") is True
        if cfg.get("source") and not development:
            raise ValueError("source checkout requires explicit development_mode")
        command = [cfg.get("python", sys.executable), *([] if development else ["-I"]),
                   "-m", "corral.execution.service_endpoint",
                   "--config", cfg["service_config"]]
        if cfg.get("transport", "local") == "ssh":
            remote = (("cd " + shlex.quote(cfg["source"]) + " && ") if development else "") \
                + shlex.join(command)
            command = ["ssh", "-o", "BatchMode=yes", *cfg.get("ssh_options", []),
                       cfg["ssh_host"], remote]
            cwd = None
        else:
            cwd = cfg.get("source") if development else "/"
        result = subprocess.run(command, input=json.dumps({"action": action, **payload}),
                                capture_output=True, text=True, cwd=cwd)
        if result.returncode:
            raise RuntimeError(result.stderr.strip())
        return json.loads(result.stdout)


def from_path(path: Path) -> ServiceClient:
    raw = json.loads(path.read_text())
    endpoint = raw.get("service_endpoint")
    if endpoint is None:
        endpoint = {"transport": "local", "service_config": str(path.resolve()),
                    "source": str(Path(__file__).parents[2]), "development_mode": True}
    return ServiceClient(endpoint)
