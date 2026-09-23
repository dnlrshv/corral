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
        if allowlist.get("regexTarget", "secret") == "secret":
            # Secret-target entries are exact values (or an anchored false-positive prefix).
            assert all(regex.startswith("^") for regex in allowlist["regexes"]), \
                allowlist["description"]
