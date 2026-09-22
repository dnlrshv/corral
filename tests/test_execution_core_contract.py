"""Candidate-bound import and lifecycle integration contract regression tests."""

import json
import sqlite3

import pytest

from tests.advisory_http_fixture import ACTOR, BASE, HEAD, PR, REPO, environment
from corral.execution.advisory_cli import main as cli_main
from corral.execution.demo import fixture_profiles
from corral.execution.pr import PRLifecycle
from corral.execution.store import Store


def test_cli_roundtrip_requires_explicit_approval(tmp_path):
    store, _, _, intent, payload = environment(tmp_path)
    proposal_path = tmp_path / "proposal.json"
    assert (
        cli_main(
            [
                "prepare",
                "--repo",
                REPO,
                "--pr",
                PR,
                "--head",
                HEAD,
                "--base",
                BASE,
                "--policy",
                payload["policy"],
                "--publisher",
                ACTOR,
                "--body",
                payload["body"],
                "--store",
                str(store.path),
                "--output-proposal",
                str(proposal_path),
            ]
        )
        == 0
    )
    proposal = json.loads(proposal_path.read_text())
    assert proposal["intent"] == intent and proposal["authorized"] is False
    with pytest.raises(PermissionError):
        cli_main(
            [
                "import-approved",
                "--store",
                str(store.path),
                "--artifact",
                str(proposal_path),
            ]
        )
    # Use a fresh local authority because existing approval is immutable.
    fresh = Store(tmp_path / "fresh.sqlite")
    fresh.acquire("pr:" + PR, "corral")
    proposal.update(authorized=True, authorized_by="test operator")
    proposal_path.write_text(json.dumps(proposal))
    assert (
        cli_main(
            [
                "import-approved",
                "--store",
                str(fresh.path),
                "--artifact",
                str(proposal_path),
            ]
        )
        == 0
    )
    assert fresh.get("advisory_payload", intent) == payload
    assert fresh.get("advisory_approval", intent)["publisher"] == ACTOR


@pytest.mark.parametrize("field", ["event", "repo", "pr", "head"])
def test_cli_prepare_rejects_silent_binding_discard(tmp_path, field):
    path = tmp_path / "payload.json"
    path.write_text(json.dumps({"body": "advisory", field: "bad"}))
    with pytest.raises(ValueError):
        cli_main(
            [
                "prepare",
                "--repo",
                REPO,
                "--pr",
                PR,
                "--head",
                HEAD,
                "--base",
                BASE,
                "--policy",
                "c" * 64,
                "--publisher",
                ACTOR,
                "--payload-file",
                str(path),
                "--store",
                str(tmp_path / "store.sqlite"),
            ]
        )


def lifecycle(store, transport, policy):
    return PRLifecycle(
        store,
        transport,
        publishers={ACTOR: "actor-token"},
        owner_token="owner",
        profiles=fixture_profiles(),
        policy={
            "version": policy,
            "routes": ["fixture"],
            "default_profile": "strong-low",
            "checks": [],
            "check_actors": [],
            "lenses": {
                "code": {
                    "actors": [ACTOR],
                    "scope": ["code"],
                    "base_bound": True,
                    "head_bound": True,
                }
            },
        },
    )


def test_lifecycle_real_path_revalidates_and_synthetic_receipt_cannot_bypass(tmp_path):
    store, http, transport, intent, payload = environment(tmp_path)
    lc = lifecycle(store, transport, payload["policy"])
    candidate = lc.candidate(
        "owner",
        PR,
        {"head": HEAD, "base": BASE, "spec": "spec", "inputs": {}},
        owner="corral",
        epoch=1,
    )
    store.put_once(
        "advisory_receipt", intent, {"transport": "synthetic_comment", "advisory": True}
    )
    http.rules[0]["enforcement"] = "disabled"
    with pytest.raises(PermissionError, match="policy"):
        lc.advisory(
            "owner",
            PR,
            "corral",
            1,
            expected=candidate,
            body=payload["body"],
            publisher_token="actor-token",
        )
    assert not http.posts
    http.rules[0]["enforcement"] = "active"
    receipt = lc.advisory(
        "owner",
        PR,
        "corral",
        1,
        expected=candidate,
        body=payload["body"],
        publisher_token="actor-token",
    )
    assert receipt["review_id"] == 901 and len(http.posts) == 1


def test_lifecycle_lost_ack_blocks_transfer_then_reconciles(tmp_path):
    store, _, transport, intent, payload = environment(tmp_path)
    lc = lifecycle(store, transport, payload["policy"])
    candidate = lc.candidate(
        "owner",
        PR,
        {"head": HEAD, "base": BASE, "spec": "spec", "inputs": {}},
        owner="corral",
        epoch=1,
    )
    with pytest.raises(ConnectionError):
        lc.advisory(
            "owner",
            PR,
            "corral",
            1,
            expected=candidate,
            body=payload["body"],
            publisher_token="actor-token",
            lose_ack=True,
        )
    assert (
        lc.transfer("owner", PR, "corral", 1, "legacy", stopped=True)["state"]
        == "uncertain"
    )
    receipt = transport.reconcile(PR, intent, payload)
    assert receipt["reconciled"] and not receipt["current_candidate_verified"]


def test_old_schema_migration_keeps_unknown_attempt_unbound(tmp_path):
    path = tmp_path / "old.sqlite"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE publication_intents(intent TEXT PRIMARY KEY, resource TEXT NOT NULL, owner TEXT NOT NULL, epoch INTEGER NOT NULL, head_sha TEXT NOT NULL, base_sha TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL, error TEXT, review_id INTEGER, created_at REAL NOT NULL, updated_at REAL NOT NULL)"
        )
    store = Store(path)
    with store.transaction() as db:
        columns = {
            row[1] for row in db.execute("PRAGMA table_info(publication_intents)")
        }
    assert "attempt_id" in columns
