from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from corral.config import load_config
from corral.retro.seats import SeatRegistry, SeatRegistryError


GOOD = """
schema_version: 1
seats:
  drafter:
    provider: a
    model: draft-model
    auth_env: DRAFT_KEY
    adapter: anthropic-sdk
  verifier:
    provider: b
    model: verify-model
    auth_env: VERIFY_KEY
    adapter: openai-compatible-endpoint
    options:
      base_url_env: VERIFY_URL
      protocol: responses
  local:
    provider: local
    model: local-model
    auth_env: null
    adapter: shell-command
    options:
      argv: [provider-cli, --model, "{model}"]
"""


def test_registry_loads_and_preserves_adapter_options(tmp_path: Path) -> None:
    path = tmp_path / "seats.yaml"
    path.write_text(textwrap.dedent(GOOD))
    registry = SeatRegistry.load(path)

    assert list(registry) == ["drafter", "verifier", "local"]
    assert registry.require("verifier").options["protocol"] == "responses"
    assert registry["local"].options["argv"] == ["provider-cli", "--model", "{model}"]
    assert registry["local"].auth_env is None


@pytest.mark.parametrize(
    "body,match",
    [
        ("[]", "top level"),
        ("schema_version: 2\nseats: {}", "schema_version"),
        ("schema_version: 1\nseats: []", "'seats' must be a mapping"),
        (
            "schema_version: 1\nseats:\n  x:\n    provider: p\n    model: m\n"
            "    auth_env: null\n",
            "missing required field 'adapter'",
        ),
        (
            "schema_version: 1\nseats:\n  x:\n    provider: p\n    model: m\n"
            "    auth_env: null\n    adapter: mystery\n",
            "unknown adapter",
        ),
        (
            "schema_version: 1\nseats:\n  x:\n    provider: p\n    model: m\n"
            "    auth_env: K\n    adapter: openai-compatible-endpoint\n    options: {}\n",
            "base_url_env",
        ),
        (
            "schema_version: 1\nseats:\n  x:\n    provider: p\n    model: m\n"
            "    auth_env: null\n    adapter: shell-command\n    options:\n      argv: string\n",
            "options.argv",
        ),
        (
            "schema_version: 1\nseats:\n  dup:\n    provider: p\n    model: m\n"
            "    auth_env: null\n  dup:\n    provider: q\n    model: n\n    auth_env: null\n",
            "duplicate key",
        ),
        (
            "schema_version: 1\nseats:\n  x:\n    provider: p\n    model: m\n"
            "    model: other\n    auth_env: null\n    adapter: anthropic-sdk\n",
            "duplicate key",
        ),
    ],
)
def test_registry_rejects_bad_documents(tmp_path: Path, body: str, match: str) -> None:
    path = tmp_path / "seats.yaml"
    path.write_text(body)
    with pytest.raises(SeatRegistryError, match=match):
        SeatRegistry(path)


def test_registry_path_comes_from_corral_config(tmp_path: Path) -> None:
    (tmp_path / "policy").mkdir()
    (tmp_path / "policy" / "models.yaml").write_text(textwrap.dedent(GOOD))
    config_path = tmp_path / "corral.yaml"
    config_path.write_text(
        "seats_file: policy/models.yaml\nretro:\n  drafter_seat: drafter\n"
        "  verifier_seats: [verifier, local]\n  require_distinct_provider: false\n"
        "  verification_timeout_s: 12\n"
    )
    config = load_config(config_path)

    assert config.seats_file == "policy/models.yaml"
    assert config.retro.verifier_seats == ["verifier", "local"]
    assert config.retro.require_distinct_provider is False
    assert config.retro.verification_timeout_s == 12
    assert SeatRegistry.from_config(config)["drafter"].model == "draft-model"
