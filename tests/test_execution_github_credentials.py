from types import SimpleNamespace

import pytest

from corral.execution.github_credentials import load


def test_loads_secret_env_without_returning_it_in_identity(monkeypatch):
    monkeypatch.setenv("FIXTURE_GH_TOKEN", "secret-value")
    token, identity = load({"mode": "secret-env", "env": "FIXTURE_GH_TOKEN",
                            "account_ref": "publisher-account"})
    assert token == "secret-value"
    assert identity == {"mode": "secret-env", "account_ref": "publisher-account"}
    assert "secret-value" not in repr(identity)


def test_loads_allowlisted_gh_account_without_exposing_token(tmp_path, monkeypatch):
    executable = tmp_path / "gh"
    executable.write_text("#!/bin/sh\n")
    executable.chmod(0o700)
    seen = []

    def run(command, **kwargs):
        seen.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="stored-token\n", stderr="")

    monkeypatch.setattr("corral.execution.github_credentials.subprocess.run", run)
    token, identity = load({"mode": "gh-cli", "executable": str(executable),
                            "hostname": "github.com", "account_ref": "mini2-user"})
    assert token == "stored-token"
    assert seen[0][0] == [str(executable), "auth", "token", "--hostname", "github.com"]
    assert seen[0][1]["capture_output"] is True
    assert identity == {"mode": "gh-cli", "account_ref": "mini2-user",
                        "hostname": "github.com"}
    assert "stored-token" not in repr(identity)


def test_refuses_ambient_or_relative_gh_loader(tmp_path):
    with pytest.raises(PermissionError, match="unsupported"):
        load({"mode": "ambient", "account_ref": "unknown"})
    with pytest.raises(PermissionError, match="absolute executable"):
        load({"mode": "gh-cli", "executable": "gh", "account_ref": "unknown"})
