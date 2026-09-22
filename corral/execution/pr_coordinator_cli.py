"""Installed trusted CLI for one configured review, repair, advisory, and merge lane."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .github_advisory import GitHubAdvisoryTransport
from .github_credentials import load as load_github_credential
from .github_merge import GitHubMergeTransport
from .pr_coordinator import PRCoordinator
from .store import Store


def _load(path: Path) -> dict:
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or "token" in value or "bridge_token" in value:
        raise PermissionError("coordinator config must not contain plaintext tokens")
    return value


def _coordinator(config: dict) -> PRCoordinator:
    return PRCoordinator(
        store=Store(config["state"]), pr=config["pr"],
        owner_epoch=config["owner_epoch"], publisher=config["publisher"],
        campaign_authorization=config["campaign_authorization"],
        publication_policy_snapshot=config["publication_policy_snapshot"],
        publisher_account_ref=config["publisher_auth"]["account_ref"])


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    actions = parser.add_subparsers(dest="action", required=True)
    review = actions.add_parser("accept-review")
    review.add_argument("--task-id", required=True)
    repair = actions.add_parser("link-repair")
    repair.add_argument("--task-id", required=True)
    repair.add_argument("--replacement-export-id", required=True)
    actions.add_parser("prepare-advisory")
    actions.add_parser("publish-advisory")
    actions.add_parser("merge")
    args = parser.parse_args(argv)
    config = _load(args.config)
    coordinator = _coordinator(config)
    if args.action == "accept-review":
        result = coordinator.accept_review(args.task_id)
    elif args.action == "link-repair":
        result = coordinator.link_repair(
            repair_task=args.task_id, replacement_export_id=args.replacement_export_id)
    elif args.action == "prepare-advisory":
        result = coordinator.prepare_advisory()
    elif args.action == "publish-advisory":
        token, identity = load_github_credential(config.get("publisher_auth"))
        if identity["account_ref"] != coordinator.publisher_account_ref:
            raise PermissionError("publisher account reference changed after coordinator binding")
        transport = GitHubAdvisoryTransport(
            store=coordinator.store, bridge_token=token, bridge_actor=config["publisher"],
            authorized_bridge_actors=frozenset({config["publisher"]}), allow_network=True,
            policy_inputs=config["publication_policy_inputs"])
        result = coordinator.publish_advisory(transport)
    elif args.action == "merge":
        merge = config["merge"]
        token, identity = load_github_credential(merge.get("auth"))
        policy = {**merge["policy"], "account_ref": identity["account_ref"]}
        transport = GitHubMergeTransport(
            store=coordinator.store, token=token, actor=merge["actor"], allow_network=True,
            auth_mode=merge.get("auth_mode", "user-token"),
            merge_policy=policy)
        result = coordinator.merge(transport)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
