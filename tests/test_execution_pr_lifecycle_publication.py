"""PRLifecycle delegates real-transport approval to the publication boundary."""

import pytest

from tests.advisory_http_fixture import ACTOR, BASE, HEAD, PR, environment
from corral.execution.demo import fixture_profiles
from corral.execution.github_advisory import GitHubAdvisoryTransport
from corral.execution.pr import PRLifecycle


class RecordingTransport(GitHubAdvisoryTransport):
    """A non-synthetic transport whose own advisory() performs no approval checks."""

    transport_name = "recording_comment"

    def advisory(self, pr, expected, intent, payload, lose_ack=False):
        self.advisory_calls.append(intent)
        return {"pr": pr, "intent": intent, "transport": self.transport_name,
                "advisory": True, "review_id": 1}


def _lifecycle(store, transport, policy):
    return PRLifecycle(
        store, transport, publishers={ACTOR: "actor-token"}, owner_token="owner",
        profiles=fixture_profiles(),
        policy={"version": policy, "routes": ["fixture"], "default_profile": "strong-low",
                "checks": [], "check_actors": [],
                "lenses": {"code": {"actors": [ACTOR], "scope": ["code"],
                                    "base_bound": True, "head_bound": True}}})


def _candidate(lifecycle):
    return lifecycle.candidate("owner", PR, {"head": HEAD, "base": BASE, "spec": "spec",
                                             "inputs": {}}, owner="corral", epoch=1)


@pytest.mark.parametrize("field,value", [("canonical_wire_hash", None), ("head", "f" * 40),
                                         ("authorized_by", "   ")])
def test_unbound_approval_never_reaches_a_non_verifying_transport(tmp_path, field, value):
    store, http, _transport, intent, payload = environment(tmp_path)
    transport = RecordingTransport(http_client=http, store=store, bridge_actor=ACTOR,
                                   authorized_bridge_actors=frozenset({ACTOR}),
                                   policy_inputs=_transport.policy_inputs)
    approval = store.get("advisory_approval", intent)
    store.replace("advisory_approval", intent, {**approval, field: value})
    lifecycle = _lifecycle(store, transport, payload["policy"])
    with pytest.raises(PermissionError):
        lifecycle.advisory("owner", PR, "corral", 1, expected=_candidate(lifecycle),
                           body=payload["body"], publisher_token="actor-token")
    assert transport.advisory_calls == [] and not http.posts
    assert store.get("advisory_intent", intent) is None


def test_proven_absent_advisory_does_not_block_transfer(tmp_path):
    store, http, transport, intent, payload = environment(tmp_path)
    lifecycle = _lifecycle(store, transport, payload["policy"])
    candidate = _candidate(lifecycle)

    def refuse_post(method, path, **kwargs):
        if method == "POST":
            raise ConnectionRefusedError("connection refused before the request was sent")
        return http(method, path, **kwargs)

    transport.http_client = refuse_post
    with pytest.raises(ConnectionRefusedError):
        lifecycle.advisory("owner", PR, "corral", 1, expected=candidate,
                           body=payload["body"], publisher_token="actor-token")
    assert lifecycle.transfer("owner", PR, "corral", 1, "legacy", stopped=True)["state"] == "uncertain"
    transport.http_client = http
    transport.absence_quiet_seconds = 0
    assert transport.reconcile(PR, intent, payload)["status"] == "absent"
    moved = lifecycle.transfer("owner", PR, "corral", 1, "legacy", stopped=True)
    assert moved == {"state": "active", "owner": "legacy", "epoch": 2}
    assert not http.posts
