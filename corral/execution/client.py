"""Thin local/SSH JSON clients. SSH authentication protects the remote channel."""
import argparse
import json
import shlex
import subprocess
import sys
from pathlib import Path


class Client:
    def __init__(self, config):
        self.config = config

    def call(self, action, **payload):
        cfg = self.config
        development = cfg.get("development_mode") is True
        if cfg.get("source") and not development:
            raise ValueError("source checkout requires explicit development_mode")
        command = [cfg["python"], *([] if development else ["-I"]),
                   "-m", "corral.execution.cli", "--config", cfg["controller_config"]]
        if cfg.get("transport", "local") == "ssh":
            remote = (("cd " + shlex.quote(cfg["source"]) + " && ") if development else "") \
                + shlex.join(command)
            command = ["ssh", "-o", "BatchMode=yes", *cfg.get("ssh_options", []), cfg["ssh_host"], remote]
            cwd = None
        else:
            cwd = cfg.get("source") if development else "/"
        result = subprocess.run(command, input=json.dumps({"action": action, **payload}),
                                capture_output=True, text=True, cwd=cwd)
        if result.returncode:
            raise RuntimeError(result.stderr.strip())
        return json.loads(result.stdout)

    def reconcile(self, task_id):
        """Request installed controller reconciliation without accepting caller observations."""
        return self.call("reconcile", task_id=task_id)


def main(repo=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--request", required=True, type=Path)
    args = parser.parse_args()
    request = json.loads(args.request.read_text())
    if repo is not None and request.get("action") == "submit":
        request.setdefault("spec", {})["repo"] = repo
    result = Client(json.loads(args.config.read_text())).call(**request)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
