"""Stateless, tool-free HTTP transport for immutable inspection packets."""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

from .inspection_packet import PACKET_SCHEMA
from .store import digest

ENVELOPE_SCHEMA = "corral-inspection-report-v1"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        raise RuntimeError("HTTP redirects are prohibited for inspection credentials")


def _endpoint(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise PermissionError("inspection endpoint must be an HTTPS URL without userinfo")
    clean = value.rstrip("/")
    return clean if clean.endswith("/chat/completions") else clean + "/chat/completions"


def _packet(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise PermissionError("inspection packet is missing or invalid") from error
    if not isinstance(value, dict) or value.get("schema") != PACKET_SCHEMA:
        raise PermissionError("inspection packet schema mismatch")
    expected = value.get("digest")
    actual = digest({key: item for key, item in value.items() if key != "digest"})
    if not isinstance(expected, str) or expected != actual:
        raise PermissionError("inspection packet digest mismatch")
    capability = value.get("capability") or {}
    if capability.get("tools_supplied") != [] or capability.get("session_reuse") is not False:
        raise PermissionError("inspection packet requests a tool or reusable session")
    return value


def _post(url: str, token: str, body: dict[str, Any], opener=None) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                 "Accept": "application/json", "User-Agent": "Corral-Inspection-Packet/1"},
    )
    try:
        if opener is None:
            response = urllib.request.build_opener(_NoRedirect()).open(request)
        else:
            response = opener(request)
        with response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"inspection provider returned HTTP {error.code}") from None
    except (OSError, ValueError, RuntimeError) as error:
        raise RuntimeError(f"inspection provider request failed: {type(error).__name__}") from None
    if not isinstance(payload, dict):
        raise RuntimeError("inspection provider returned a non-object response")
    return payload


def _usage(raw: Any) -> dict[str, int]:
    if not isinstance(raw, dict):
        return {}
    mapping = {"prompt_tokens": "input_tokens", "completion_tokens": "output_tokens",
               "total_tokens": "total_tokens"}
    counters = {target: raw[source] for source, target in mapping.items()
                if isinstance(raw.get(source), int) and not isinstance(raw[source], bool) and raw[source] >= 0}
    prompt_details = raw.get("prompt_tokens_details")
    if isinstance(prompt_details, dict) and isinstance(prompt_details.get("cached_tokens"), int):
        counters["cache_read_tokens"] = prompt_details["cached_tokens"]
    completion_details = raw.get("completion_tokens_details")
    if isinstance(completion_details, dict) and isinstance(completion_details.get("reasoning_tokens"), int):
        counters["thinking_tokens"] = completion_details["reasoning_tokens"]
    return counters


def invoke(*, packet_path: Path, result_path: Path, endpoint: str, credential: str,
           model: str, effort: str | None, provider: str, account_ref: str, route: str,
           opener: Callable | None = None, synthetic: bool = False) -> dict[str, Any]:
    """Make exactly one stateless request and persist a report only after strict validation."""
    packet = _packet(packet_path)
    if not credential:
        raise PermissionError("inspection credential is unavailable")
    if not all(isinstance(item, str) and item for item in (model, provider, account_ref, route)):
        raise PermissionError("inspection route identity is incomplete")
    system = (
        "Inspect only the immutable packet supplied by the controller. You have no tools and "
        "must not claim to run tests, scripts, imports, builds, packages, or shell commands. "
        "Return a concise source review grounded only in packet contents."
    )
    request_body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": json.dumps(packet, sort_keys=True)}],
        "stream": False,
    }
    if effort:
        request_body["reasoning_effort"] = effort
    # There is deliberately no `tools`, `tool_choice`, previous-response or session field.
    response = _post(_endpoint(endpoint), credential, request_body, opener=opener)
    response_model = response.get("model")
    if response_model != model:
        raise PermissionError("inspection response model does not match the requested model")
    choices = response.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise PermissionError("inspection response must contain exactly one choice")
    choice = choices[0]
    message = choice.get("message")
    if not isinstance(message, dict):
        raise PermissionError("inspection response message is missing")
    # This rejection happens before content extraction and before any report is persisted.
    if message.get("tool_calls") or message.get("function_call") or choice.get("finish_reason") == "tool_calls":
        raise PermissionError("inspection response attempted a tool call; no execution is available")
    report = message.get("content")
    if not isinstance(report, str) or not report.strip():
        raise PermissionError("inspection response report is empty")
    counters = _usage(response.get("usage"))
    response_id = response.get("id") if isinstance(response.get("id"), str) else None
    result = {
        "schema": ENVELOPE_SCHEMA,
        "status": "completed",
        "synthetic": synthetic,
        "narrative": report.strip(),
        "result": {"report": report.strip(), "packet_digest": packet["digest"],
                   "provenance": packet["provenance"], "capability": packet["capability"]},
        "identity": {"model": response_model, "provider": provider, "account_ref": account_ref,
                     "route": route, "harness": "inspection-packet-http", "version": "1"},
        "requested": {"model": model, "effort": effort},
        "observed": {"response_model": response_model, "response_id": response_id,
                     "effort_attested": False, "session_mode": "stateless"},
        "usage": counters,
    }
    result_path.parent.mkdir(parents=True, exist_ok=True)
    partial = result_path.with_suffix(".partial")
    partial.write_text(json.dumps(result["result"], indent=2, sort_keys=True))
    partial.replace(result_path)
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packet", required=True, type=Path)
    parser.add_argument("--result", required=True, type=Path)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--credential-env", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--effort")
    parser.add_argument("--provider", required=True)
    parser.add_argument("--account-ref", required=True)
    parser.add_argument("--route", required=True)
    parser.add_argument("--synthetic", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = invoke(packet_path=args.packet, result_path=args.result,
                        endpoint=args.endpoint, credential=os.environ.get(args.credential_env, ""),
                        model=args.model, effort=args.effort, provider=args.provider,
                        account_ref=args.account_ref, route=args.route, synthetic=args.synthetic)
    except (PermissionError, RuntimeError) as error:
        print(json.dumps({"schema": ENVELOPE_SCHEMA, "status": "failed",
                          "error": str(error), "synthetic": args.synthetic}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
