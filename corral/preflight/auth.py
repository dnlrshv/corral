"""Auth helpers for preflight LLM calls.

Environment-variable precedence (preserved from the source implementation):

1. ``ANTHROPIC_API_KEY`` wins. When both ``ANTHROPIC_API_KEY`` and
   ``ANTHROPIC_AUTH_TOKEN`` are set, the SDK's API-key header is used and
   the ``Authorization`` header is suppressed.
2. ``ANTHROPIC_AUTH_TOKEN`` is used as a bearer auth token when no API key
   is set.
3. ``CLAUDE_CODE_OAUTH_TOKEN`` is the OAuth-token fallback when neither of
   the above is set.

``.env`` loading (only when ``python-dotenv`` is installed) never overrides
variables already present in the environment.

The ``anthropic`` SDK is imported lazily inside the call helper so that
importing this module — and running the deterministic fallback brief — works
without the ``corral[preflight]`` extra installed.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Any

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - optional dependency
    load_dotenv = None  # type: ignore[assignment]

#: Canonical Anthropic model ID for preflight brief generation. Preflight is
#: latency-sensitive and runs per-task, so it stays on the cheapest capable
#: model; import this constant rather than re-hardcoding it elsewhere.
#: Overridable via the ``preflight.model`` key in ``corral.yaml``.
DEFAULT_PREFLIGHT_MODEL = "claude-haiku-4-5-20251001"
API_TIMEOUT = 60.0
_DOTENV_LOADED = False


@dataclass(frozen=True)
class PreflightLLMResponse:
    """Raw model call result.

    ``stop_reason`` is the Anthropic Messages API stop reason (``end_turn``,
    ``max_tokens``, etc.), surfaced so callers can tell output-token
    truncation apart from other generation failures instead of guessing from
    the parsed text alone.
    """

    text: str
    stop_reason: str | None


def _load_dotenv_once() -> None:
    global _DOTENV_LOADED
    if load_dotenv is None or _DOTENV_LOADED:
        return
    load_dotenv(override=False)
    _DOTENV_LOADED = True


def get_llm_auth_token() -> str | None:
    _load_dotenv_once()
    return os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")


def has_llm_auth() -> bool:
    _load_dotenv_once()
    return bool(os.environ.get("ANTHROPIC_API_KEY") or get_llm_auth_token())


def _call_anthropic(
    model: str,
    prompt: str,
    max_tokens: int,
    history: list[dict[str, str]] | None = None,
) -> PreflightLLMResponse:
    _load_dotenv_once()
    import anthropic

    client_kwargs: dict[str, Any] = {"timeout": API_TIMEOUT}
    auth_token = get_llm_auth_token()
    if os.environ.get("ANTHROPIC_API_KEY"):
        if os.environ.get("ANTHROPIC_AUTH_TOKEN"):
            # API key wins: suppress the bearer Authorization header the SDK
            # would otherwise send alongside x-api-key.
            client_kwargs["default_headers"] = {"Authorization": anthropic.Omit()}
    elif auth_token is not None:
        client_kwargs["auth_token"] = auth_token
    client = anthropic.Anthropic(**client_kwargs)
    messages = [*(history or []), {"role": "user", "content": prompt}]
    message = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=messages,
    )
    text = message.content[0].text.strip() if message.content else ""
    return PreflightLLMResponse(text=text, stop_reason=message.stop_reason)


def call_llm_with_meta(
    prompt: str,
    max_tokens: int,
    history: list[dict[str, str]] | None = None,
    *,
    model: str = DEFAULT_PREFLIGHT_MODEL,
) -> PreflightLLMResponse:
    """Call the preflight model and return both the text and ``stop_reason``.

    ``history`` prepends prior turns (e.g. the model's own malformed reply)
    ahead of ``prompt`` as the newest user turn -- used by the one-shot retry
    in brief generation to feed a validation error back to the model within
    the same conversation instead of a stateless retry.
    """
    return _call_anthropic(model, prompt, max_tokens, history=history)


def call_llm(prompt: str, max_tokens: int, *, model: str = DEFAULT_PREFLIGHT_MODEL) -> str:
    """Text-only wrapper around :func:`call_llm_with_meta`."""
    return call_llm_with_meta(prompt, max_tokens, model=model).text


def warn_deprecated_fallback_on_error() -> None:
    print(
        "::warning::--fallback-on-error is deprecated and has no effect; "
        "deterministic fallback is now the default. Use --strict for hard failures.",
        file=sys.stderr,
    )
