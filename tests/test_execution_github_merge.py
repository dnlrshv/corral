"""Production merge transport contracts against a fully local GitHub HTTP fixture."""
import pytest

import corral.execution.github_merge as merge_module
from corral.execution.github_merge import GitHubMergeTransport
from corral.execution.policy import compute_policy_digest
from corral.execution.store import Store


class GitHub:
    def __init__(self):
        self.merged = False
        self.head = "a" * 40
        self.puts = 0
        self.lose_ack = False
        self.merged_by = "fixture-merge"
        self.reviews = [{"id": 1, "commit_id": "a" * 40, "state": "APPROVED", "user": {"login": "reviewer"}}]
        self.checks = [{"id": 1, "name": "test", "status": "completed", "conclusion": "success"}]
        self.rulesets = []
        self.protection = {"required_status_checks": {"strict": True}}

    def __call__(self, method, path, *, headers, json):
        assert headers["Authorization"] == "Bearer fixture-token"
        if path == "/user":
            return {"login": "fixture-merge"}
        if path == "/repos/fixture/repo/pulls/1":
            return {"state": "open", "head": {"sha": self.head}, "base": {"sha": "b" * 40},
                    "merged": self.merged, "merged_by": {"login": self.merged_by} if self.merged else None,
                    "merge_commit_sha": "c" * 40 if self.merged else None}
        if path == "/repos/fixture/repo/rulesets?per_page=100&page=1&includes_parents=true":
            return [{"id": item["id"]} for item in self.rulesets]
        if path.startswith("/repos/fixture/repo/rulesets/"):
            rule_id = int(path.rsplit("/", 1)[1])
            return next(item for item in self.rulesets if item["id"] == rule_id)
        if path == "/repos/fixture/repo/branches/main/protection":
            return self.protection
        if path == "/repos/fixture/repo/commits/" + "a" * 40 + "/check-runs?per_page=100&page=1":
            return {"total_count": len(self.checks), "check_runs": self.checks}
        if path == "/repos/fixture/repo/pulls/1/reviews?per_page=100&page=1":
            return self.reviews
        if path == "/repos/fixture/repo/pulls/1/merge" and method == "PUT":
            assert json == {"sha": "a" * 40}
            self.puts += 1
            self.merged = True
            if self.lose_ack:
                raise ConnectionError("lost acknowledgement")
            return {"merged": True, "sha": "c" * 40}
        raise AssertionError((method, path))


def snapshot(github):
    enforcement = {"repo": "fixture/repo", "base_sha": "b" * 40, "rulesets": github.rulesets,
                   "classic_protection": github.protection}
    return {"schema_version": "1.0", "repo": "fixture/repo", "base_sha": "b" * 40,
            "base_ref": "main", "rulesets": github.rulesets, "classic_protection": github.protection,
            "enforcement_contents": enforcement,
            "enforcement_digest": compute_policy_digest(enforcement)}


def transport(tmp_path, github):
    store = Store(tmp_path / "state")
    epoch = store.acquire("pr:fixture/repo#1", "corral")
    policy = {"repo": "fixture/repo", "base": "b" * 40, "base_ref": "main", "campaign_authorization": "campaign-1",
              "account_ref": "fixture-merge-account",
              "required_checks": ["test"], "required_reviewers": ["reviewer"], "policy_snapshot": snapshot(github),
              "required_internal_reviews": [],
              "live_policy_digest": compute_policy_digest({"rulesets": github.rulesets, "classic_protection": github.protection})}
    return GitHubMergeTransport(store=store, token="fixture-token", actor="fixture-merge",
                                http_client=github, merge_policy=policy), store, epoch


def payload(epoch):
    return {"pr": "fixture/repo#1", "owner": "corral", "epoch": epoch, "head": "a" * 40,
            "base": "b" * 40}


def test_authenticated_merge_re_reads_candidate_checks_reviews_and_receipt(tmp_path):
    github = GitHub()
    client, store, epoch = transport(tmp_path, github)
    result = client.merge(**payload(epoch))
    assert result["merged"] and result["merge_commit_sha"] == "c" * 40 and result["merged_by"] == "fixture-merge"
    assert github.puts == 1
    assert client.merge(**payload(epoch)) == result
    assert github.puts == 1 and store.records("merge_intent")


def test_lost_ack_is_reconciled_without_second_merge_put(tmp_path):
    github = GitHub()
    client, _store, epoch = transport(tmp_path, github)
    github.lose_ack = True
    with pytest.raises(ConnectionError):
        client.merge(**payload(epoch))
    assert github.puts == 1
    assert client.merge(**payload(epoch))["merged"]
    assert github.puts == 1


def test_failed_read_only_preflight_keeps_history_but_can_later_merge_once(tmp_path):
    github = GitHub()
    client, store, epoch = transport(tmp_path, github)
    github.checks[0]["conclusion"] = "failure"
    with pytest.raises(PermissionError, match="required GitHub check"):
        client.merge(**payload(epoch))
    assert not store.records("merge_intent") and store.records("merge_preflight_failure")
    github.checks[0]["conclusion"] = "success"
    assert client.merge(**payload(epoch))["merged"] and github.puts == 1


def test_merge_refuses_missing_required_evidence_empty_token_and_installation_identity(tmp_path, monkeypatch):
    github = GitHub()
    client, _store, epoch = transport(tmp_path, github)
    github.reviews = []
    with pytest.raises(PermissionError, match="required GitHub review"):
        client.merge(**payload(epoch))
    monkeypatch.setattr(merge_module, "_get_auth_token", lambda token: None)
    with pytest.raises(PermissionError, match="authenticated GitHub merge token"):
        GitHubMergeTransport(store=Store(tmp_path / "empty"), token="", actor="fixture-merge",
                             http_client=github, merge_policy=client.merge_policy)
    with pytest.raises(PermissionError, match="installation-token"):
        GitHubMergeTransport(store=Store(tmp_path / "other"), token="fixture-token", actor="fixture-merge",
                             http_client=github, auth_mode="installation-token", merge_policy=client.merge_policy)


def test_internal_review_can_satisfy_explicit_policy_without_remote_approval(tmp_path):
    from corral.execution.internal_review import SCHEMA
    from corral.execution.store import digest

    github = GitHub()
    client, store, epoch = transport(tmp_path, github)
    github.reviews = []
    policy = dict(client.merge_policy)
    policy.update(required_reviewers=[],
                  required_internal_reviews=[{"profile_id": "inspection-medium",
                                              "policy_id": "advisory"}],
                  review_policy_digest="d" * 64)
    receipt = {"schema": SCHEMA, "task": "review-task", "attempt": "review-attempt",
               "generation": 1, "export_id": "e" * 64, "repo": "fixture/repo",
               "pr": "fixture/repo#1", "head": "a" * 40, "base": "b" * 40,
               "policy_id": "advisory", "review_policy_digest": "d" * 64,
               "profile_id": "inspection-medium", "verdict": "PASS",
               "identity": {"configured": {"provider": "fixture", "model": "reviewer",
                                            "account_ref": "account", "route": "inspection"},
                            "observed": {"model": "reviewer",
                                         "harness": "inspection-packet-http"}},
               "report_digest": "f" * 64, "packet_digest": "1" * 64}
    receipt = {"receipt_id": digest(receipt), **receipt}
    store.put_once("internal_review_receipt", receipt["receipt_id"], receipt)
    client = GitHubMergeTransport(store=store, token="fixture-token", actor="fixture-merge",
                                  http_client=github, merge_policy=policy)
    assert client.merge(**payload(epoch))["merged"] is True


def test_internal_review_policy_rejects_missing_or_tampered_receipt(tmp_path):
    github = GitHub()
    client, store, epoch = transport(tmp_path, github)
    github.reviews = []
    policy = dict(client.merge_policy)
    policy.update(required_reviewers=[],
                  required_internal_reviews=[{"profile_id": "inspection-medium"}],
                  review_policy_digest="d" * 64)
    client = GitHubMergeTransport(store=store, token="fixture-token", actor="fixture-merge",
                                  http_client=github, merge_policy=policy)
    with pytest.raises(PermissionError, match="internal model review"):
        client.merge(**payload(epoch))
    assert github.puts == 0 and not store.records("merge_intent")
    store.put_once("internal_review_receipt", "bad", {
        "receipt_id": "bad", "schema": "corral-internal-review-v1",
        "pr": "fixture/repo#1", "head": "a" * 40, "base": "b" * 40,
        "review_policy_digest": "d" * 64, "profile_id": "inspection-medium",
        "verdict": "PASS",
    })
    with pytest.raises(PermissionError, match="internal model review"):
        client.merge(**payload(epoch))
    assert github.puts == 0 and not store.records("merge_intent")


def test_latest_review_and_check_run_states_override_older_success(tmp_path):
    github = GitHub()
    github.reviews.append({"id": 2, "commit_id": "a" * 40, "state": "CHANGES_REQUESTED", "user": {"login": "reviewer"}})
    client, _store, epoch = transport(tmp_path, github)
    with pytest.raises(PermissionError, match="required GitHub review"):
        client.merge(**payload(epoch))
    github.reviews.pop()
    github.checks.append({"id": 2, "name": "test", "status": "completed", "conclusion": "failure"})
    client, _store, epoch = transport(tmp_path / "check", github)
    with pytest.raises(PermissionError, match="required GitHub check"):
        client.merge(**payload(epoch))


def test_live_detailed_ruleset_or_protection_drift_refuses_merge(tmp_path):
    github = GitHub()
    client, _store, epoch = transport(tmp_path, github)
    github.rulesets.append({"id": 1, "name": "new-rule", "target": "branch", "enforcement": "active",
                            "conditions": {}, "rules": [], "bypass_actors": []})
    with pytest.raises(PermissionError, match="protection or rulesets drifted"):
        client.merge(**payload(epoch))


def test_reconcile_refuses_different_head_or_authenticated_merger(tmp_path):
    github = GitHub()
    client, store, epoch = transport(tmp_path, github)
    github.lose_ack = True
    with pytest.raises(ConnectionError):
        client.merge(**payload(epoch))
    github.head = "d" * 40
    with pytest.raises(PermissionError, match="unresolved external effect"):
        client.merge(**payload(epoch))
    assert not store.records("merge_receipt")
    github.head, github.merged_by = "a" * 40, "another-actor"
    with pytest.raises(PermissionError, match="unresolved external effect"):
        client.merge(**payload(epoch))
    assert not store.records("merge_receipt")


def test_corrupted_snapshot_is_refused_before_authentication(tmp_path):
    github = GitHub()
    client, _store, _epoch = transport(tmp_path, github)
    policy = dict(client.merge_policy)
    policy["policy_snapshot"] = {**policy["policy_snapshot"], "enforcement_digest": "bad"}
    with pytest.raises(PermissionError, match="corrupted"):
        GitHubMergeTransport(store=Store(tmp_path / "bad"), token="fixture-token", actor="fixture-merge",
                             http_client=github, merge_policy=policy)


def test_non_sha_merge_ack_is_not_accepted(tmp_path):
    github = GitHub()
    client, store, epoch = transport(tmp_path, github)
    original = github.__call__

    def bad_ack(method, path, *, headers, json):
        response = original(method, path, headers=headers, json=json)
        if method == "PUT":
            return {"merged": True, "sha": "z" * 40}
        return response

    client.http_client = bad_ack
    with pytest.raises(PermissionError, match="merge response"):
        client.merge(**payload(epoch))
    assert store.records("merge_intent") and not store.records("merge_receipt")


@pytest.mark.parametrize("ack", [["merged"], "merged", None, 1])
def test_non_object_merge_ack_is_refused_and_left_for_reconciliation(tmp_path, ack):
    github = GitHub()
    client, store, epoch = transport(tmp_path, github)
    original = github.__call__

    def odd_ack(method, path, *, headers, json):
        response = original(method, path, headers=headers, json=json)
        return ack if method == "PUT" else response

    client.http_client = odd_ack
    with pytest.raises(PermissionError, match="merge response"):
        client.merge(**payload(epoch))
    assert store.records("merge_intent") and not store.records("merge_receipt")
    assert not store.records("merge_ack")
    # The PUT did merge; the retry resolves the persisted intent by authenticated readback.
    client.http_client = original
    assert client.merge(**payload(epoch))["merged"] is True
    assert github.puts == 1
