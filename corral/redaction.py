"""Robust redaction boundaries for error diagnostics, configs, and retro evidence."""

from __future__ import annotations

import re
from typing import Any

_CREDENTIAL_KEY = (
    r"[A-Za-z0-9_]*(?:access[_-]?token|api[_-]?key|auth[_-]?token|token|secret|password|passwd|"
    r"private[_-]?key|database[_-]?url|db[_-]?url|dsn|"
    r"connection[_-]?(?:string|uri|url))[A-Za-z0-9_]*"
)

# Supported real numeric token counter keys (input, output, total, cache, thinking, reasoning, candidates, etc.)
_SAFE_KEYS = re.compile(
    r"(?i)^[\"']?(?:"
    r"(?:[a-z0-9_]*_)?(?:input|output|total|candidate|candidates|cache(?:_read|_write)?|"
    r"cached_?content|thinking|thoughts?|reasoning|prompt|completion|estimated)[_-]?tokens?(?:[_-]?(?:count|usage))?|"
    r"token[_-]?(?:count|usage)"
    r")[\"']?$"
)
_NUMERIC_VALUE = re.compile(r"^-?\d+(?:\.\d+)?$")

_CREDENTIAL_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
    re.compile(r"\bgh[oprsu]_[A-Za-z0-9_]{8,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{3,}\b"),
    re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bxox[baprsce]-[A-Za-z0-9-]{8,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]*\.[A-Za-z0-9_-]*\b"),
    re.compile(r"\b(?:glpat-|hf_|xai-|pat_)[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bya29\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^/\s@]+@"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}\b"),
    re.compile(
        rf"(?im)^[ \t]*(?P<key>[\"']?{_CREDENTIAL_KEY}[\"']?)\s*:\s*"
        r"[>|][-+0-9]*[^\n]*\n(?:(?:[ \t]+[^\n]*|[ \t]*)\n|[ \t]+[^\n]*\Z)+"
    ),
    re.compile(
        rf"(?i)(?<![A-Za-z0-9_])(?P<key>[\"']?{_CREDENTIAL_KEY}[\"']?)"
        r"(?P<sep_space>\s*(?P<sep>[:=])\s*)"
        r"(?![\"']?(?:<redacted>|\[REDACTED\])[\"']?)"
        r"(?P<val>\"[^\"]*\"|'[^']*'|[^\s\"',}]+)"
    ),
)

_OUTBOUND_CREDENTIAL_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", re.IGNORECASE),
    re.compile(
        r"\b(?:gh[oprsu]_|github_pat_|sk-(?:ant-|proj-)?|xox[baprsce]-|"
        r"glpat-|hf_|xai-|ya29\.)[A-Za-z0-9_-]{8,}\b"
    ),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]*\.[A-Za-z0-9_-]*\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}\b"),
    re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^/\s@]+@"),
    re.compile(
        rf"(?i)(?<![A-Za-z0-9_])(?P<key>[\"']?{_CREDENTIAL_KEY}[\"']?)"
        r"(?P<sep_space>\s*(?P<sep>[:=])\s*)"
        r"(?![\"']?(?:<redacted>|\[REDACTED\])[\"']?)"
        r"(?P<val>\"[^\"]*\"|'[^']*'|[^\s\"',}]+)"
    ),
)


def redact_text(text: str, *, marker: str = "[REDACTED]") -> str:
    """Redact secrets from text while preserving numeric token counters and valid JSON."""
    if not isinstance(text, str):
        return text

    def _replace(match: re.Match[str]) -> str:
        groupdict = match.groupdict()
        if "key" in groupdict and groupdict["key"]:
            key_raw = groupdict["key"]
            key_str = key_raw.strip("\"'")
            val_raw = groupdict.get("val", "")
            val_str = val_raw.strip("\"'")
            sep_space = groupdict.get("sep_space") or groupdict.get("sep") or "="

            # Real numeric token counters are preserved
            if _SAFE_KEYS.match(key_str) and _NUMERIC_VALUE.match(val_str):
                return match.group(0)

            # Already redacted values are preserved
            if val_str in ("<redacted>", "[REDACTED]"):
                return match.group(0)

            # Maintain valid JSON quoting and syntax
            if val_raw.startswith('"') and val_raw.endswith('"'):
                replacement_val = f'"{marker}"'
            elif val_raw.startswith("'") and val_raw.endswith("'"):
                replacement_val = f"'{marker}'"
            elif key_raw.startswith('"') and key_raw.endswith('"') and ":" in sep_space:
                replacement_val = f'"{marker}"'
            else:
                replacement_val = marker

            if "\n" in match.group(0):
                suffix = "\n" if match.group(0).endswith("\n") else ""
                return f"{key_raw}:{sep_space}{replacement_val}{suffix}"

            return f"{key_raw}{sep_space}{replacement_val}"

        raw = match.group(0)
        if raw.lower().startswith("bearer "):
            return f"Bearer {marker}"
        if raw.lower().startswith("bearer"):
            return f"Bearer {marker}"
        return marker

    for pattern in _CREDENTIAL_PATTERNS:
        text = pattern.sub(_replace, text)
    return text


def check_outbound_safe(text: str) -> list[str]:
    """Check text against outbound credential patterns, exempting numeric token counters."""
    offenders = []
    for pattern in _OUTBOUND_CREDENTIAL_PATTERNS:
        for match in pattern.finditer(text):
            groupdict = match.groupdict()
            if "key" in groupdict and groupdict["key"]:
                key_str = groupdict["key"].strip("\"'")
                val_raw = groupdict.get("val", "")
                val_str = val_raw.strip("\"'")
                # Exemption for real numeric token counters
                if _SAFE_KEYS.match(key_str) and _NUMERIC_VALUE.match(val_str):
                    continue
                # Exemption for already-redacted values
                if val_str in ("<redacted>", "[REDACTED]"):
                    continue
            offenders.append(pattern.pattern)
    return offenders


def safe_config_diagnostic(data: Any) -> Any:
    """Return a deep copy of config/diagnostic data with credentials redacted.
    
    Preserves non-secret configuration structure (e.g. hosts, paths, profiles,
    and supported numeric token counters) while redacting tokens, keys, passwords,
    and secret strings. Output is valid JSON-serializable structure.
    """
    if isinstance(data, dict):
        result = {}
        for k, v in data.items():
            k_str = str(k)
            if re.search(rf"(?i)^(?:{_CREDENTIAL_KEY})$", k_str):
                if _SAFE_KEYS.match(k_str) and isinstance(v, (int, float)) and not isinstance(v, bool):
                    result[k] = v
                elif _SAFE_KEYS.match(k_str) and isinstance(v, str) and _NUMERIC_VALUE.match(v):
                    result[k] = v
                else:
                    result[k] = "[REDACTED]"
            else:
                result[k] = safe_config_diagnostic(v)
        return result
    elif isinstance(data, list):
        return [safe_config_diagnostic(item) for item in data]
    elif isinstance(data, str):
        return redact_text(data, marker="[REDACTED]")
    return data
