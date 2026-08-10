"""Parsing helpers for preflight model output."""

from __future__ import annotations

import re
import textwrap
from typing import Any

import yaml

# Keep in sync with corral.preflight.retry.BRIEF_FIELDS. Importing it here
# would create a circular import because retry imports this parser module.
BRIEF_FIELD_NAMES = (
    "files_to_touch",
    "files_to_read_only",
    "surfaces_in_scope",
    "cross_cutting_concerns",
    "recent_related_prs",
    "invariants_to_preserve",
    "test_files",
    "estimated_blast_radius",
    "do_not_touch",
)

FENCE_RE = re.compile(r"```(?:ya?ml)?[ \t]*\r?\n(.*?)\r?\n```", re.DOTALL | re.IGNORECASE)
TOP_LEVEL_FIELD_RE = re.compile(rf"^\s*({'|'.join(BRIEF_FIELD_NAMES)}):(?:\s|$)")
SECRET_PATTERNS = (
    re.compile(r"(sk-ant-[A-Za-z0-9_-]+|ghp_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)"),
    re.compile(
        r"(?i)\b(api[_-]?key|auth[_-]?token|token|secret|password)"
        r"(\s*[:=]\s*)([^\s,;]+)"
    ),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._-]+"),
)


def parse_brief(raw_yaml: str) -> dict[str, Any]:
    """Parse a preflight brief from plain YAML or common LLM wrappers."""
    direct_error: Exception | None = None
    try:
        direct = yaml.safe_load(raw_yaml)
    except yaml.YAMLError as exc:
        direct_error = exc
    else:
        if isinstance(direct, dict):
            return direct
        if direct is not None:
            raise ValueError(f"Expected YAML mapping, got {type(direct).__name__}")

    mappings, parse_errors = _extract_candidate_mappings(raw_yaml)
    if len(mappings) == 1:
        return mappings[0]
    if len(mappings) > 1:
        raise ValueError("Ambiguous YAML mapping blocks in model output")
    if parse_errors:
        raise parse_errors[0]
    if direct_error is not None:
        raise direct_error
    raise ValueError("Expected YAML mapping in model output")


def sanitize_preflight_error(exc: Exception, max_length: int = 240) -> str:
    """Return a concise diagnostic that redacts obvious token-like secrets."""
    lines = [line.strip() for line in str(exc).splitlines() if line.strip()]
    summary = "; ".join(lines) if lines else repr(exc)
    message = f"{type(exc).__name__}: {summary}"
    for pattern in SECRET_PATTERNS:
        message = pattern.sub(_redact_match, message)
    if len(message) > max_length:
        return f"{message[: max_length - 3]}..."
    return message


def _extract_candidate_mappings(raw_yaml: str) -> tuple[list[dict[str, Any]], list[Exception]]:
    candidates = _fenced_blocks(raw_yaml)
    if not candidates:
        candidates = _prose_yaml_blocks(raw_yaml)

    mappings: list[dict[str, Any]] = []
    errors: list[Exception] = []
    for candidate in candidates:
        try:
            loaded = yaml.safe_load(candidate)
        except yaml.YAMLError as exc:
            errors.append(exc)
            continue
        if isinstance(loaded, dict):
            mappings.append(loaded)
    return mappings, errors


def _fenced_blocks(raw_yaml: str) -> list[str]:
    return [match.group(1).strip() for match in FENCE_RE.finditer(raw_yaml)]


def _prose_yaml_blocks(raw_yaml: str) -> list[str]:
    blocks: list[str] = []
    current: list[str] = []
    for line in raw_yaml.splitlines():
        if TOP_LEVEL_FIELD_RE.match(line) or (
            current and (line.startswith((" ", "\t")) or line.strip() == "")
        ):
            current.append(line)
        elif current:
            _append_non_empty_block(blocks, current)
            current = []
    if current:
        _append_non_empty_block(blocks, current)
    return blocks


def _append_non_empty_block(blocks: list[str], lines: list[str]) -> None:
    block = textwrap.dedent("\n".join(lines)).strip()
    if block:
        blocks.append(block)


def _redact_match(match: re.Match[str]) -> str:
    if match.lastindex and match.lastindex >= 2:
        return f"{match.group(1)}{match.group(2)}[REDACTED]"
    text = match.group(0)
    if text.lower().startswith("bearer"):
        return "Bearer [REDACTED]"
    return "[REDACTED]"
