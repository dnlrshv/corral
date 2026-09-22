"""Maintained full-capability coding launch contracts without provider invocation."""
import pytest

from corral.execution.coding_launcher import argv_for
from corral.execution.adapter import _substitute
from corral.execution.routes import declare


def test_agy_coding_contract_uses_supported_flags_and_unbounded_wait():
    argv = argv_for("agy")
    assert "--dangerously-skip-permissions" in argv
    assert argv[argv.index("--print-timeout") + 1] == "0"
    assert "{prompt}" in argv and "{workspace}" in argv
    route = declare("coding", {"harness": "agy", "binary": "agy", "argv": argv,
                                "envelope": "agy-json-v1", "provider": "google",
                                "account_ref": "host-private", "endpoint": "host-private",
                                "supported_models": ["gemini-3.8-flash"], "supported_efforts": ["medium"]})
    assert route.argv[-1] == "{prompt}"


def test_codex_coding_contract_binds_provider_and_effort_without_an_effort_flag():
    argv = argv_for("codex", model_provider="approved_baba")
    assert "--sandbox" in argv and "workspace-write" in argv
    assert "--effort" not in argv
    assert 'model_provider="approved_baba"' in argv
    assert 'model_reasoning_effort="{effort}"' in argv


def test_codex_coding_contract_refuses_an_implicit_provider():
    with pytest.raises(PermissionError, match="model_provider"):
        argv_for("codex")


def test_prompt_contents_are_not_reinterpreted_as_route_placeholders():
    assert _substitute(["{prompt}"], {"prompt": "preserve {workspace} literally"}) == ["preserve {workspace} literally"]
