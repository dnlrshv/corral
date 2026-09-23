"""Repository-owned policy sources are explicit, validated, and digest bound."""
import pytest

from corral.execution.policy import fetch_policy_snapshot
from corral.execution.policy_inputs import normalize
from tests.advisory_http_fixture import BASE, HTTP, POLICY_INPUTS, REPO, environment


def test_default_policy_has_no_repository_specific_source_or_runner():
    seen = []
    http = HTTP()

    def request(method, path):
        seen.append(path)
        return http(method, path)

    snapshot = fetch_policy_snapshot(REPO, BASE, http_client=request)
    assert snapshot["required_sources"] == []
    assert snapshot["runner"] == {}
    assert snapshot["selected_workflow"] is None
    assert not any("/contents/scripts/" in path for path in seen)


@pytest.mark.parametrize("value", [
    {"required_sources": "script.py"}, {"required_sources": ["../private"]},
    {"required_sources": ["/tmp/private"]}, {"required_sources": ["a?ref=other"]},
    {"required_sources": ["a\\b"]}, {"required_sources": [None]},
    {"required_sources": ["a//b"]}, {"selected_workflow": "unbound.yml"},
    {"runner": []}, {"unexpected": True},
])
def test_malformed_inputs_fail_before_network(value):
    def forbidden(*args):
        pytest.fail("malformed configuration reached GitHub")
    with pytest.raises((ValueError, TypeError)):
        fetch_policy_snapshot(REPO, BASE, http_client=forbidden, policy_inputs=value)


def test_sources_and_runner_affect_policy_digest():
    http = HTTP()
    first = fetch_policy_snapshot(REPO, BASE, http_client=http, policy_inputs=POLICY_INPUTS)
    other = {**POLICY_INPUTS, "runner": {"runs_on": ["another-executor"]}}
    second = fetch_policy_snapshot(REPO, BASE, http_client=http, policy_inputs=other)
    assert first["enforcement_digest"] != second["enforcement_digest"]
    assert normalize({"required_sources": ["a", "a", "b"]})["required_sources"] == ["a", "b"]


def test_publisher_rechecks_explicit_policy_inputs(tmp_path):
    _, http, transport, intent, payload = environment(tmp_path)
    transport.policy_inputs = {**POLICY_INPUTS, "runner": {"runs_on": ["changed"]}}
    with pytest.raises(PermissionError, match="live policy digest changed"):
        transport.advisory(payload["pr"], payload["head"], intent, payload)
    assert http.posts == []


def test_configured_non_main_branch_publishes(tmp_path):
    _, http, transport, intent, payload = environment(tmp_path, base_ref="release/stable")
    transport.advisory(payload["pr"], payload["head"], intent, payload)
    assert len(http.posts) == 1


def test_same_sha_different_branch_is_refused(tmp_path):
    _, http, transport, intent, payload = environment(tmp_path, base_ref="release/stable")
    http.pull["base"]["ref"] = "other"
    with pytest.raises(PermissionError, match="trusted repository policy"):
        transport.advisory(payload["pr"], payload["head"], intent, payload)
    assert http.posts == []
