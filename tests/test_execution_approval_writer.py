"""The advisory approval writer refuses what the publication boundary would refuse."""

import pytest

from tests.advisory_http_fixture import ACTOR, BASE, HEAD, HTTP, POLICY_INPUTS, PR, REPO
from corral.execution.advisory import compute_advisory_intent
from corral.execution.github_advisory import GitHubAdvisoryTransport
from corral.execution.github_support import compute_canonical_wire_hash
from corral.execution.policy import fetch_policy_snapshot
from corral.execution.store import Store, record_advisory_approval


def _prepared(tmp_path, *, store_payload=True):
    """A stored payload without an approval, plus the arguments of a valid approval."""
    http = HTTP()
    store = Store(tmp_path / "authority.sqlite")
    store.acquire("pr:" + PR, "corral")
    snapshot = fetch_policy_snapshot(REPO, BASE, base_ref="main", http_client=http,
                                     policy_inputs=POLICY_INPUTS)
    policy = snapshot["enforcement_digest"]
    intent, payload = compute_advisory_intent(REPO, PR, HEAD, BASE, policy, ACTOR, "advisory", {})
    if store_payload:
        store.put_once("advisory_payload", intent, payload)
    good = dict(repo=REPO, pr=PR, head=HEAD, base=BASE, policy=policy, intent=intent, epoch=1,
                authorized_by="test operator", publisher=ACTOR,
                canonical_wire_hash=compute_canonical_wire_hash(HEAD, "advisory", [])[0])
    transport = GitHubAdvisoryTransport(http_client=http, store=store, bridge_actor=ACTOR,
                                        authorized_bridge_actors=frozenset({ACTOR}),
                                        policy_inputs=POLICY_INPUTS)
    return store, http, transport, intent, payload, good, snapshot


@pytest.mark.parametrize("delta", [
    {"intent": "local-intent"},
    {"canonical_wire_hash": None},
    {"canonical_wire_hash": compute_canonical_wire_hash(HEAD, "other body", [])[0]},
    {"policy": "c" * 64},
    {"policy_snapshot": {"enforcement_digest": "not-the-policy"}},
    {"authorized_by": ""},
    {"authorized_by": "   "},
    {"authorized_by": 1},
    {"head": "f" * 40},
    {"repo": "other/repo"},
    {"publisher": "someone-else"},
    {"epoch": 0},
    {"epoch": True},
], ids=lambda delta: next(iter(delta)))
def test_writer_refuses_unbound_approvals(tmp_path, delta):
    store, _http, _transport, intent, _payload, good, _snapshot = _prepared(tmp_path)
    arguments = {**good, **delta}
    with pytest.raises((PermissionError, ValueError)):
        record_advisory_approval(store, **arguments)
    assert store.get("advisory_approval", arguments["intent"]) is None
    assert store.get("advisory_approval", intent) is None


def test_writer_requires_the_stored_payload_and_an_explicit_policy(tmp_path):
    store, _http, _transport, intent, _payload, good, _snapshot = _prepared(
        tmp_path, store_payload=False)
    with pytest.raises(PermissionError):
        record_advisory_approval(store, **good)
    without_policy = {key: value for key, value in good.items() if key != "policy"}
    with pytest.raises(TypeError):
        record_advisory_approval(store, **without_policy)
    assert store.get("advisory_approval", intent) is None


def test_refused_write_leaves_the_intent_publishable_and_persists_the_snapshot(tmp_path):
    store, http, transport, intent, payload, good, snapshot = _prepared(tmp_path)
    with pytest.raises(ValueError):
        record_advisory_approval(store, **{**good, "canonical_wire_hash": None})
    approval = record_advisory_approval(store, **good, policy_snapshot=snapshot)
    assert record_advisory_approval(store, **good, policy_snapshot=snapshot) == approval
    assert store.get("advisory_approval", intent)["policy_snapshot"] == snapshot
    transport.advisory(PR, "candidate", intent, payload)
    assert len(http.posts) == 1


@pytest.mark.parametrize("provenance", ["   ", 1])
def test_publication_refuses_blank_or_non_string_provenance(tmp_path, provenance):
    store, http, transport, intent, payload, good, _snapshot = _prepared(tmp_path)
    record_advisory_approval(store, **good)
    approval = store.get("advisory_approval", intent)
    approval["authorized_by"] = provenance  # a row written before the writer validated it
    store.replace("advisory_approval", intent, approval)
    with pytest.raises(PermissionError, match="provenance"):
        transport.advisory(PR, "candidate", intent, payload)
    assert not http.posts
