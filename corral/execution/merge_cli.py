"""Small stdin JSON seam for the installed merge coordinator."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .github_merge import GitHubMergeTransport
from .store import Store


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args(argv)
    config, request = json.loads(args.config.read_text()), json.loads(input())
    if "token" in config:
        raise PermissionError("merge CLI does not accept tokens in config; use the configured user-token source")
    transport = GitHubMergeTransport(store=Store(config["state"]), token=None, actor=config["actor"],
                                     allow_network=True, auth_mode=config.get("auth_mode", "user-token"),
                                     merge_policy=config["merge_policy"])
    if request.get("action") == "merge":
        result = transport.merge(**request["payload"])
    elif request.get("action") == "reconcile":
        result = transport.reconcile(request["intent"])
    else:
        raise ValueError("action must be merge or reconcile")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
