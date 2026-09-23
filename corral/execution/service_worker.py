"""Detached service-owned controller dispatch for one explicit task and host."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .controller import Controller


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--task", required=True)
    parser.add_argument("--host", required=True)
    args = parser.parse_args(argv)
    raw = json.loads(args.config.read_text())
    controller = Controller(raw["state"], raw["token"], raw["hosts"],
                            default_host=raw["default_host"], profiles=raw.get("profiles", []),
                            secret_env=raw.get("secret_env"))
    result = controller.run(raw["token"], args.task, execution_host=args.host)
    print(json.dumps({"task": args.task, "state": result.get("state")}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
