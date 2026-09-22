"""Source tests for corral/execution/policy.py.

Verifies complete fail-closed contracts, ruleset pagination, required source bindings,
redirect prevention, token file mode restrictions, and snapshot schema validation.
"""

from __future__ import annotations

import io
import json
import os
import stat
import urllib.error
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from corral.execution.policy import (
    _get_auth_token,
    compute_policy_digest,
    fetch_policy_snapshot,
    load_policy_snapshot,
    save_policy_snapshot,
)

from tests.advisory_http_fixture import REQUIRED_SOURCE_PATHS, POLICY_INPUTS

REPO = "example/project"
BASE_SHA = "b" * 40
DUMMY_TOKEN = "TEST_AUTH_TOKEN_VALUE"


def _make_http_response(payload: Any, status: int = 200, url: str = "https://api.github.com/test"):
    resp = MagicMock()
    resp.status = status
    resp.geturl.return_value = url
    data = json.dumps(payload).encode("utf-8") if not isinstance(payload, bytes) else payload
    resp.read.return_value = data
    resp.__enter__.return_value = resp
    return resp


def test_contract_1_ruleset_detail_403_must_fail_closed():
    def mock_urlopen(req, timeout=15):
        url = req.full_url
        if "/rulesets" in url and not any(ch.isdigit() for ch in url.split("/")[-1]):
            return _make_http_response([{"id": 14170693, "name": "Advisory Rule", "target": "branch", "enforcement": "active"}])
        if "/rulesets/14170693" in url:
            raise urllib.error.HTTPError(url, 403, "Forbidden", {}, io.BytesIO(b'{"message": "Forbidden"}'))
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, io.BytesIO(b"{}"))

    with patch("urllib.request.urlopen", side_effect=mock_urlopen):
        with pytest.raises(PermissionError, match="Insufficient permissions to read ruleset detail"):
            fetch_policy_snapshot(REPO, BASE_SHA, token=DUMMY_TOKEN, policy_inputs=POLICY_INPUTS)


def test_contract_2_protection_403_and_500_fail_closed():
    def mock_urlopen_403(req, timeout=15):
        url = req.full_url
        if "/rulesets" in url and not any(ch.isdigit() for ch in url.split("/")[-1]):
            return _make_http_response([])
        if "/protection" in url:
            raise urllib.error.HTTPError(url, 403, "Forbidden", {}, io.BytesIO(b'{"message": "Must have admin rights"}'))
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, io.BytesIO(b"{}"))

    with patch("urllib.request.urlopen", side_effect=mock_urlopen_403):
        with pytest.raises(PermissionError, match="Insufficient permissions to read branch protection"):
            fetch_policy_snapshot(REPO, BASE_SHA, token=DUMMY_TOKEN, policy_inputs=POLICY_INPUTS)

    def mock_urlopen_500(req, timeout=15):
        url = req.full_url
        if "/rulesets" in url and not any(ch.isdigit() for ch in url.split("/")[-1]):
            return _make_http_response([])
        if "/protection" in url:
            raise urllib.error.HTTPError(url, 500, "Internal Error", {}, io.BytesIO(b"{}"))
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, io.BytesIO(b"{}"))

    with patch("urllib.request.urlopen", side_effect=mock_urlopen_500):
        with pytest.raises(RuntimeError, match="Failed to read branch protection"):
            fetch_policy_snapshot(REPO, BASE_SHA, token=DUMMY_TOKEN, policy_inputs=POLICY_INPUTS)


def test_contract_3_workflow_403_must_fail_closed():
    def mock_urlopen(req, timeout=15):
        url = req.full_url
        if "/rulesets" in url and not any(ch.isdigit() for ch in url.split("/")[-1]):
            return _make_http_response([])
        if "/protection" in url:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, io.BytesIO(b"{}"))
        if "/contents/.github/workflows?" in url:
            raise urllib.error.HTTPError(url, 403, "Forbidden", {}, io.BytesIO(b'{"message": "Forbidden"}'))
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, io.BytesIO(b"{}"))

    with patch("urllib.request.urlopen", side_effect=mock_urlopen):
        with pytest.raises(PermissionError, match="Insufficient permissions to read workflows"):
            fetch_policy_snapshot(REPO, BASE_SHA, token=DUMMY_TOKEN, policy_inputs=POLICY_INPUTS)


def test_contract_4_ruleset_pagination_and_detail_assembly():
    queried_urls: list[str] = []

    def mock_urlopen(req, timeout=15):
        url = req.full_url
        queried_urls.append(url)
        if "/rulesets" in url and not any(ch.isdigit() for ch in url.split("/")[-1]):
            return _make_http_response([
                {"id": i, "name": f"rule-{i}", "target": "branch", "enforcement": "active"} for i in range(1, 36)
            ])
        if "/rulesets/" in url:
            rid = int(url.split("/")[-1])
            return _make_http_response({"id": rid, "name": f"rule-{rid}", "target": "branch", "enforcement": "active", "conditions": {}, "rules": []})
        if "/protection" in url:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, io.BytesIO(b"{}"))
        if "/contents/.github/workflows?" in url:
            return _make_http_response([])
        if "/contents/" in url:
            for p in REQUIRED_SOURCE_PATHS:
                if p in url:
                    return _make_http_response({"path": p, "sha": "c" * 40, "size": 100})
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, io.BytesIO(b"{}"))

    with patch("urllib.request.urlopen", side_effect=mock_urlopen):
        snapshot = fetch_policy_snapshot(REPO, BASE_SHA, token=DUMMY_TOKEN, policy_inputs=POLICY_INPUTS)

    ruleset_requests = [u for u in queried_urls if "/rulesets" in u and "?" in u]
    assert any("per_page=" in u for u in ruleset_requests)
    assert len(snapshot["rulesets"]) == 35
    assert len(snapshot["required_sources"]) == 4


def test_contract_5_deterministic_binding_and_required_sources_404_refusal():
    def mock_urlopen_missing_script(req, timeout=15):
        url = req.full_url
        if "/rulesets" in url and not any(ch.isdigit() for ch in url.split("/")[-1]):
            return _make_http_response([])
        if "/protection" in url:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, io.BytesIO(b"{}"))
        if "/contents/.github/workflows?" in url:
            return _make_http_response([{"name": "review.yml", "path": ".github/workflows/review.yml", "sha": "c" * 40}])
        if "scripts/review_runner.py" in url:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, io.BytesIO(b"{}"))
        if "/contents/" in url:
            return _make_http_response({"path": url.split("/contents/")[1].split("?")[0], "sha": "c" * 40, "size": 100})
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, io.BytesIO(b"{}"))

    with patch("urllib.request.urlopen", side_effect=mock_urlopen_missing_script):
        with pytest.raises(RuntimeError, match="Required policy source 'scripts/review_runner.py' not found"):
            fetch_policy_snapshot(REPO, BASE_SHA, token=DUMMY_TOKEN, policy_inputs=POLICY_INPUTS)


def test_contract_6_url_redirect_blocks_credential_exposure():
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.geturl.return_value = "https://untrusted-third-party.example.com/leak"
        mock_resp.read.return_value = b"[]"
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        with pytest.raises(RuntimeError, match="HTTP redirects prohibited on authenticated policy endpoints"):
            fetch_policy_snapshot(REPO, BASE_SHA, token=DUMMY_TOKEN, policy_inputs=POLICY_INPUTS)


def test_contract_7_token_file_restricted_permissions(tmp_path: Path):
    token_path = tmp_path / "insecure_token.txt"
    token_path.write_text("TOKEN_SAMPLE_SECRET\n", encoding="utf-8")
    token_path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)

    with patch.dict(os.environ, {"GITHUB_TOKEN_FILE": str(token_path)}, clear=True):
        with pytest.raises(PermissionError, match="Insecure permissions on token file"):
            _get_auth_token()

    # Restricted permissions 0600 must be accepted
    token_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    with patch.dict(os.environ, {"GITHUB_TOKEN_FILE": str(token_path)}, clear=True):
        token = _get_auth_token()
        assert token == "TOKEN_SAMPLE_SECRET"


def test_contract_8_snapshot_loader_complete_schema_and_digest_validation(tmp_path: Path):
    missing_contents = tmp_path / "corrupted_policy.json"
    missing_contents.write_text(json.dumps({"schema_version": "1.0", "repo": REPO, "base_sha": BASE_SHA}), encoding="utf-8")
    with pytest.raises(ValueError, match="Missing required field 'enforcement_contents'"):
        load_policy_snapshot(missing_contents)

    enforcement = {
        "base_sha": BASE_SHA,
        "classic_protection": None,
        "repo": REPO,
        "required_sources": [],
        "rulesets": [],
        "runner": {"runs_on": ["self-hosted", "macOS", "dev"]},
        "selected_workflow": None,
        "workflows": [],
    }
    digest = compute_policy_digest(enforcement)
    valid_data = {
        "schema_version": "1.0",
        "repo": REPO,
        "base_sha": BASE_SHA,
        "enforcement_contents": enforcement,
        "enforcement_digest": digest,
    }
    valid_file = tmp_path / "valid_policy.json"
    save_policy_snapshot(valid_data, valid_file)

    loaded = load_policy_snapshot(valid_file)
    assert loaded["enforcement_digest"] == digest

    # Tampered digest raises ValueError
    valid_data["enforcement_digest"] = "bad" * 16
    tampered_file = tmp_path / "tampered_policy.json"
    tampered_file.write_text(json.dumps(valid_data), encoding="utf-8")
    with pytest.raises(ValueError, match="digest mismatch"):
        load_policy_snapshot(tampered_file)


@pytest.mark.parametrize("mutation", ["source-sha", "source-empty", "workflow", "ruleset"])
def test_authenticated_incomplete_snapshot_refused(mutation):
    from tests.advisory_http_fixture import HTTP, REPO, BASE
    http = HTTP()
    def broken(method, path):
        value = http(method, path)
        if mutation == "source-sha" and "/contents/scripts/publish_review.py" in path:
            value["sha"] = "invalid"
        if mutation == "source-empty" and "/contents/scripts/publish_review.py" in path:
            return {}
        if mutation == "workflow" and "/contents/.github/workflows?" in path:
            return [{"name": "only-name"}]
        if mutation == "ruleset" and "/rulesets/1" in path:
            value.pop("rules")
        return value
    with pytest.raises(ValueError):
        fetch_policy_snapshot(REPO, BASE, http_client=broken, policy_inputs=POLICY_INPUTS)


def test_client_403_body_containing_404_is_not_absence():
    from tests.advisory_http_fixture import HTTP, REPO, BASE
    http = HTTP()
    def denied(method, path):
        if "/protection" in path:
            raise urllib.error.HTTPError(path, 403, "example 404 is not status", {}, None)
        return http(method, path)
    with pytest.raises(PermissionError):
        fetch_policy_snapshot(REPO, BASE, http_client=denied, policy_inputs=POLICY_INPUTS)


@pytest.mark.parametrize("invalid", [{}, {"message": "not found"}, {"required_status_checks": "missing"}])
def test_protection_200_cannot_mean_absence(invalid):
    from tests.advisory_http_fixture import HTTP, REPO, BASE
    http = HTTP()
    def broken(method, path):
        return invalid if "/protection" in path else http(method, path)
    with pytest.raises(ValueError):
        fetch_policy_snapshot(REPO, BASE, http_client=broken, policy_inputs=POLICY_INPUTS)
