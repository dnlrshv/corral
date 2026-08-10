"""Credential redaction and outbound gates for retrospective bridge records."""

from __future__ import annotations

import re

from corral.retro.types import BridgeEvidence

_CREDENTIAL_KEY = (
    r"[A-Za-z0-9_]*(?:access[_-]?token|api[_-]?key|token|secret|password|passwd|"
    r"private[_-]?key|database[_-]?url|db[_-]?url|dsn|"
    r"connection[_-]?(?:string|uri|url))[A-Za-z0-9_]*"
)
_CREDENTIAL_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
    re.compile(r"\bgh[oprsu]_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bxox[baprsce]-[A-Za-z0-9-]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
    re.compile(r"\b(?:glpat-|hf_|xai-|pat_)[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bya29\.[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^/\s@]+@"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{16,}\b"),
    re.compile(
        rf"(?im)^[ \t]*(?P<key>[\"']?{_CREDENTIAL_KEY}[\"']?)\s*:\s*"
        r"[>|][-+0-9]*[^\n]*\n(?:(?:[ \t]+[^\n]*|[ \t]*)\n|[ \t]+[^\n]*\Z)+"
    ),
    re.compile(
        rf"(?i)(?<![A-Za-z0-9_])(?P<key>[\"']?{_CREDENTIAL_KEY}[\"']?)"
        r"\s*[:=]\s*(?![\"']?<redacted>[\"']?)"
        r"(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s\"',}]+)"
    ),
)
_OUTBOUND_CREDENTIAL_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", re.IGNORECASE),
    re.compile(
        r"\b(?:gh[oprsu]_|github_pat_|sk-(?:ant-|proj-)?|xox[baprsce]-|"
        r"glpat-|hf_|xai-|ya29\.)[A-Za-z0-9_-]{16,}\b"
    ),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{16,}\b"),
    re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^/\s@]+@"),
    re.compile(
        rf"(?i)(?<![A-Za-z0-9_])[\"']?{_CREDENTIAL_KEY}[\"']?\s*[:=]\s*"
        r"(?![\"']?<redacted>[\"']?)(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s\"',}]+)"
    ),
)


class UnsafeBridgeRecordError(ValueError):
    """A bridge record failed its outbound credential/containment gate."""


def sanitize_text(text: str) -> str:
    sanitized = text
    for pattern in _CREDENTIAL_PATTERNS:
        sanitized = pattern.sub(_redaction, sanitized)
    return sanitized


def assert_safe_record(record: BridgeEvidence) -> None:
    """Fail closed over every field that can leave the bridge."""
    serialized = "\n".join(
        (
            record.source_ref,
            record.incident_ref,
            record.agent,
            record.area,
            record.summary,
            record.text,
            *record.repo_paths,
            record.modified or "",
        )
    )
    offenders = [
        pattern.pattern for pattern in _OUTBOUND_CREDENTIAL_PATTERNS if pattern.search(serialized)
    ]
    if offenders:
        raise UnsafeBridgeRecordError(
            f"credential scrub failed for bridge evidence: {offenders[:1]}"
        )


def _redaction(match: re.Match[str]) -> str:
    if "key" in match.re.groupindex:
        suffix = "\n" if match.group(0).endswith("\n") else ""
        return f"{match.group('key')}=<redacted>{suffix}"
    if match.lastindex:
        return f"{match.group(1)}=<redacted>"
    return "<redacted>"


__all__ = ["UnsafeBridgeRecordError", "assert_safe_record", "sanitize_text"]
