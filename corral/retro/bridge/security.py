"""Credential redaction and outbound gates for retrospective bridge records."""

from __future__ import annotations

from corral.redaction import check_outbound_safe, redact_text
from corral.retro.types import BridgeEvidence


class UnsafeBridgeRecordError(ValueError):
    """A bridge record failed its outbound credential/containment gate."""


def sanitize_text(text: str) -> str:
    """Sanitize text destined for the retrospective bridge using <redacted> markers."""
    return redact_text(text, marker="<redacted>")


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
    offenders = check_outbound_safe(serialized)
    if offenders:
        raise UnsafeBridgeRecordError(
            f"credential scrub failed for bridge evidence: {offenders[:1]}"
        )


__all__ = ["UnsafeBridgeRecordError", "assert_safe_record", "sanitize_text"]
