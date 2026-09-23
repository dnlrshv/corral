import json

import pytest

from corral.execution.secret_env import load, select


def test_private_secret_file_and_route_allowlist(tmp_path, monkeypatch):
    path = tmp_path / "provider-secrets.json"
    path.write_text(json.dumps({"PROVIDER_API_KEY": "fixture-private-value",
                                "UNRELATED_KEY": "must-not-forward"}))
    path.chmod(0o600)
    values = load(path)
    assert select(("PROVIDER_API_KEY",), values) == {"PROVIDER_API_KEY": "fixture-private-value"}
    assert "UNRELATED_KEY" not in select(("PROVIDER_API_KEY",), values)
    monkeypatch.setenv("PROVIDER_API_KEY", "development-fallback")
    assert select(("PROVIDER_API_KEY",), None) == {"PROVIDER_API_KEY": "development-fallback"}


def test_secret_file_rejects_broad_permissions_symlinks_and_bad_values(tmp_path):
    path = tmp_path / "provider-secrets.json"
    path.write_text(json.dumps({"PROVIDER_API_KEY": "fixture"}))
    path.chmod(0o644)
    with pytest.raises(PermissionError, match="0600"):
        load(path)
    path.chmod(0o600)
    link = tmp_path / "link.json"
    link.symlink_to(path)
    with pytest.raises(PermissionError, match="non-symlink"):
        load(link)
    path.write_text(json.dumps({"BAD-NAME": "fixture"}))
    with pytest.raises(ValueError, match="mapping"):
        load(path)
