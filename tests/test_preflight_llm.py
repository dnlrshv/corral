"""LLM-path tests with a mocked Anthropic client (no network).

The scripted LLM mock patches ``corral.preflight.auth._call_anthropic`` so
retry orchestration runs without SDK construction. Auth resolution has a
separate fake-SDK test below.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from corral.cli import main
from corral.preflight import auth
from corral.preflight.parser import sanitize_preflight_error
from corral.preflight.retry import BriefResponseError

from .preflight_support import clean_preflight_env

pytestmark = pytest.mark.usefixtures("clean_preflight_env")

SURFACES_YAML = """
surfaces:
  real-surface:
    paths: [src/real.py]
"""

VALID_BRIEF_YAML = """
files_to_touch:
  - src/real.py
files_to_read_only:
  - src/real.py
surfaces_in_scope:
  - real-surface
  - ghost-surface
cross_cutting_concerns:
  - Keep threshold semantics stable.
recent_related_prs:
  - "#1023"
invariants_to_preserve:
  - Preserve threshold behavior.
test_files:
  - src/test_real.py
estimated_blast_radius: medium
do_not_touch: []
"""

WRAPPED_BRIEF_YAML = f"preflight_brief_v1:\n" + "\n".join(
    f"  {line}" if line else line for line in VALID_BRIEF_YAML.strip().splitlines()
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "real.py").write_text("VALUE = 1\n")
    (tmp_path / "surfaces.yaml").write_text(SURFACES_YAML)
    # Explicit recognized modules so create-intent validation allows
    # src/test_real.py without code-map artifacts.
    (tmp_path / "corral.yaml").write_text(
        "preflight:\n  recognized_modules: [src, tests]\n"
    )
    return tmp_path


class MockAnthropic:
    """Scripted stand-in for the Anthropic Messages API."""

    def __init__(self, responses: list[tuple[str, str]]) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def __call__(self, model, prompt, max_tokens, history=None):
        self.calls.append(
            {"model": model, "prompt": prompt, "max_tokens": max_tokens, "history": history}
        )
        text, stop_reason = self.responses.pop(0)
        return auth.PreflightLLMResponse(text=text, stop_reason=stop_reason)


def run_preflight(monkeypatch, repo: Path, mock: MockAnthropic, *extra: str):
    monkeypatch.setattr(auth, "_call_anthropic", mock)
    return main(
        [
            "preflight",
            "--root",
            str(repo),
            "--config",
            str(repo / "corral.yaml"),
            "--task",
            "Update src/real.py",
            *extra,
        ]
    )


def test_llm_happy_path(monkeypatch, repo: Path, capsys) -> None:
    mock = MockAnthropic([(VALID_BRIEF_YAML, "end_turn")])

    rc = run_preflight(monkeypatch, repo, mock)

    assert rc == 0
    assert len(mock.calls) == 1
    assert mock.calls[0]["model"] == auth.DEFAULT_PREFLIGHT_MODEL
    assert auth.DEFAULT_PREFLIGHT_MODEL == "claude-haiku-4-5-20251001"

    import yaml

    brief = yaml.safe_load(capsys.readouterr().out.partition("\n")[2])
    assert brief["preflight_status"] == "generated"
    assert brief["stop_reason"] == "end_turn"
    # Post-validation dropped the hallucinated surface but kept the real one.
    assert brief["surfaces_in_scope"] == ["real-surface"]
    assert brief["files_to_touch"] == ["src/real.py"]
    assert brief["test_files"] == ["src/test_real.py"]
    assert brief["estimated_blast_radius"] == "medium"


def test_schema_invalid_response_retries_once_with_error_feedback(
    monkeypatch, repo: Path, capsys
) -> None:
    mock = MockAnthropic(
        [(WRAPPED_BRIEF_YAML, "end_turn"), (VALID_BRIEF_YAML, "max_tokens")]
    )

    rc = run_preflight(monkeypatch, repo, mock)

    assert rc == 0
    assert len(mock.calls) == 2
    retry_call = mock.calls[1]
    # The retry feeds the validation error and the bad reply back in context.
    assert "failed validation" in retry_call["prompt"]
    assert "Missing required fields" in retry_call["prompt"]
    assert "files_to_touch" in retry_call["prompt"]
    assert retry_call["max_tokens"] == mock.calls[0]["max_tokens"] == 1500
    assert retry_call["history"] == [
        {"role": "user", "content": mock.calls[0]["prompt"]},
        {"role": "assistant", "content": WRAPPED_BRIEF_YAML},
    ]

    import yaml

    brief = yaml.safe_load(capsys.readouterr().out.partition("\n")[2])
    assert brief["preflight_status"] == "generated"
    assert brief["stop_reason"] == "max_tokens"


def test_auth_precedence_matches_reference(monkeypatch: pytest.MonkeyPatch) -> None:
    omitted = object()
    client_kwargs: list[dict] = []
    create_kwargs: list[dict] = []

    class FakeMessages:
        def create(self, **kwargs):
            create_kwargs.append(kwargs)
            return SimpleNamespace(
                content=[SimpleNamespace(text=" ok ")], stop_reason="end_turn"
            )

    class FakeClient:
        def __init__(self, **kwargs):
            client_kwargs.append(kwargs)
            self.messages = FakeMessages()

    fake_sdk = SimpleNamespace(Anthropic=FakeClient, Omit=lambda: omitted)
    monkeypatch.setitem(sys.modules, "anthropic", fake_sdk)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "api-key")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "auth-token")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-token")

    response = auth._call_anthropic("configured-model", "prompt", 123)

    assert auth.get_llm_auth_token() == "auth-token"
    assert auth.has_llm_auth() is True
    assert client_kwargs == [
        {
            "timeout": auth.API_TIMEOUT,
            "default_headers": {"Authorization": omitted},
        }
    ]
    assert create_kwargs == [
        {
            "model": "configured-model",
            "max_tokens": 123,
            "messages": [{"role": "user", "content": "prompt"}],
        }
    ]
    assert response == auth.PreflightLLMResponse(text="ok", stop_reason="end_turn")

    monkeypatch.delenv("ANTHROPIC_API_KEY")
    client_kwargs.clear()
    auth._call_anthropic("configured-model", "prompt", 123)
    assert client_kwargs == [{"timeout": auth.API_TIMEOUT, "auth_token": "auth-token"}]


def test_schema_invalid_twice_falls_back(monkeypatch, repo: Path, capsys) -> None:
    mock = MockAnthropic(
        [(WRAPPED_BRIEF_YAML, "end_turn"), (WRAPPED_BRIEF_YAML, "end_turn")]
    )

    rc = run_preflight(monkeypatch, repo, mock)

    assert rc == 0
    assert len(mock.calls) == 2

    import yaml

    brief = yaml.safe_load(capsys.readouterr().out.partition("\n")[2])
    assert brief["preflight_status"] == "fallback"
    assert brief["fallback_reason"] == "llm_response_invalid"
    assert brief["preflight_error"]


def test_schema_invalid_twice_strict_raises(monkeypatch, repo: Path) -> None:
    mock = MockAnthropic(
        [(WRAPPED_BRIEF_YAML, "end_turn"), (WRAPPED_BRIEF_YAML, "end_turn")]
    )

    with pytest.raises(BriefResponseError):
        run_preflight(monkeypatch, repo, mock, "--strict")


def test_semantic_quality_fallback_when_paths_hallucinated(
    monkeypatch, repo: Path, capsys
) -> None:
    hallucinated = VALID_BRIEF_YAML.replace("src/real.py", "src/ghost.py").replace(
        "src/test_real.py", "src/ghost_test.py"
    )
    mock = MockAnthropic([(hallucinated, "end_turn")])

    rc = run_preflight(monkeypatch, repo, mock)

    assert rc == 0

    import yaml

    brief = yaml.safe_load(capsys.readouterr().out.partition("\n")[2])
    assert brief["preflight_status"] == "fallback"
    assert brief["fallback_reason"] == "semantic_quality"


def test_auth_missing_falls_back_with_recorded_error(
    monkeypatch, repo: Path, capsys
) -> None:
    def raise_auth_error(model, prompt, max_tokens, history=None):
        raise RuntimeError("No Anthropic credentials found (api_key=sk-ant-live123)")

    monkeypatch.setattr(auth, "_call_anthropic", raise_auth_error)

    rc = main(["preflight", "--root", str(repo), "--task", "Update src/real.py"])

    assert rc == 0

    import yaml

    brief = yaml.safe_load(capsys.readouterr().out.partition("\n")[2])
    assert brief["preflight_status"] == "fallback"
    assert brief["fallback_reason"] == "preflight_llm_unavailable"
    # Secret redaction applies to the recorded error.
    assert "sk-ant-live123" not in brief["preflight_error"]
    assert "[REDACTED]" in brief["preflight_error"]


def test_sanitize_preflight_error_redacts_secret_patterns() -> None:
    message = (
        "auth failed: api_key=sk-ant-AbC123_xYz, token ghp_AbC123def, "
        "github_pat_Zz9, password: hunter2, Bearer eyJ.hb-c.d"
    )
    sanitized = sanitize_preflight_error(RuntimeError(message))

    for secret in ("sk-ant-AbC123_xYz", "ghp_AbC123def", "github_pat_Zz9", "hunter2", "eyJ.hb-c.d"):
        assert secret not in sanitized
    assert sanitized.count("[REDACTED]") >= 5
