"""Installed PR coordinator command exercises live publication policy validation."""
import json

from tests.advisory_http_fixture import ACTOR, BASE, POLICY_INPUTS, PR, REPO, environment
from corral.execution.github_advisory import GitHubAdvisoryTransport
from corral.execution.policy import fetch_policy_snapshot
from corral.execution import pr_coordinator_cli


def test_publish_advisory_passes_trusted_policy_inputs(tmp_path, monkeypatch, capsys):
    store, http, _transport, intent, _payload = environment(tmp_path)
    snapshot = fetch_policy_snapshot(
        REPO, BASE, base_ref="main", http_client=http, policy_inputs=POLICY_INPUTS)
    store.put_once("pr_coordination", PR, {
        "schema": "corral-pr-coordination-v1", "pr": PR, "owner_epoch": 1,
        "stage": "advisory-prepared", "advisory_intent": intent,
        "review_receipt": "bound-review-receipt", "evidence": [],
    })
    monkeypatch.setenv("FIXTURE_PUBLISHER_TOKEN", "fixture-token")
    config = tmp_path / "coordinator.json"
    config.write_text(json.dumps({
        "state": str(store.path), "pr": PR, "owner_epoch": 1, "publisher": ACTOR,
        "campaign_authorization": "test operator",
        "publication_policy_snapshot": snapshot,
        "publication_policy_inputs": POLICY_INPUTS,
        "publisher_auth": {"mode": "secret-env", "env": "FIXTURE_PUBLISHER_TOKEN",
                           "account_ref": "fixture-publisher"},
    }))

    def transport(**kwargs):
        kwargs.update(http_client=http, allow_network=False)
        return GitHubAdvisoryTransport(**kwargs)

    monkeypatch.setattr(pr_coordinator_cli, "GitHubAdvisoryTransport", transport)
    pr_coordinator_cli.main(["--config", str(config), "publish-advisory"])

    result = json.loads(capsys.readouterr().out)
    assert result["delivered"] is True
    assert result["bridge_actor"] == ACTOR
    assert len(http.posts) == 1
