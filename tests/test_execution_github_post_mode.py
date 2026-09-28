"""Dry-run writes retain exact payloads without external effects."""
import pytest
from types import SimpleNamespace

from corral.execution.advisory_cli import cmd_publish
from .advisory_http_fixture import PR, environment
from .test_execution_github_merge import GitHub, payload, transport


def test_advisory_default_dry_run_records_exact_review(tmp_path):
    store, http, publisher, intent, advisory = environment(tmp_path)
    publisher.post_mode = "dry-run"
    result = publisher.advisory(PR, "candidate", intent, advisory)
    assert result["status"] == "dry-run"
    assert result["method"] == "POST"
    assert result["endpoint"] == "/repos/test/repo/pulls/101/reviews"
    assert result["body"]["event"] == "COMMENT"
    assert result["body"]["body"] == advisory["body"]
    assert http.posts == []
    assert store.get("github_dry_run", intent) == result
    assert store.get("advisory_receipt", intent) is None


def test_advisory_cli_default_records_without_network(tmp_path, capsys):
    store, http, _publisher, intent, _advisory = environment(tmp_path)
    args = SimpleNamespace(repo="test/repo", pr=PR, intent=intent, store=store.path,
                           post_mode="dry-run", allow_network=False, candidate="candidate")
    assert cmd_publish(args) == 0
    assert '"status": "dry-run"' in capsys.readouterr().out
    assert http.posts == []
    assert store.get("github_dry_run", intent)["method"] == "POST"


def test_merge_dry_run_and_explicit_flag(tmp_path):
    github = GitHub()
    merger, store, epoch = transport(tmp_path, github)
    merger.post_mode = "dry-run"
    result = merger.merge(**payload(epoch))
    assert result == store.get("github_dry_run", result["intent"])
    assert result["endpoint"] == "/repos/fixture/repo/pulls/1/merge"
    assert result["body"] == {"sha": "a" * 40}
    assert github.puts == 0
    assert store.records("merge_intent") == {}

    merger.post_mode = "comment"
    merger.allow_merge = False
    with pytest.raises(PermissionError, match="allow_merge"):
        merger.merge(**payload(epoch))
    assert github.puts == 0
