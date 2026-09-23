"""The secret-scan allowlist stays scoped to synthetic values, never to whole files."""
from __future__ import annotations

from pathlib import Path

import pytest

tomllib = pytest.importorskip("tomllib")

CONFIG = Path(__file__).resolve().parents[1] / ".gitleaks.toml"


def test_global_allowlists_name_values_not_paths():
    config = tomllib.loads(CONFIG.read_text(encoding="utf-8"))
    allowlists = config["allowlists"]
    assert allowlists
    for allowlist in allowlists:
        # A global allowlist path skips the whole file regardless of `condition`, so a
        # real secret added to an allowlisted test file would go unreported.
        assert "paths" not in allowlist, allowlist["description"]
        assert allowlist["regexes"], allowlist["description"]
        # Entries are tested against the captured secret alone. A `match` or `line` target
        # would also accept a finding whose surrounding text merely contains a fixture value.
        assert allowlist.get("regexTarget", "secret") == "secret", allowlist["description"]
        # Secret-target entries are exact values (or an anchored false-positive prefix).
        assert all(regex.startswith("^") for regex in allowlist["regexes"]), \
            allowlist["description"]


def test_synthetic_fixture_allowlists_are_exact_values():
    config = tomllib.loads(CONFIG.read_text(encoding="utf-8"))
    synthetic = [allowlist for allowlist in config["allowlists"]
                 if allowlist["description"].startswith("Synthetic")]
    assert synthetic
    for allowlist in synthetic:
        assert all(regex.startswith("^") and regex.endswith("$") for regex in allowlist["regexes"]), \
            allowlist["description"]
