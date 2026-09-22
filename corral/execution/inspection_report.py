"""Controller-side validation for reports returned by inspection-only routes."""
from __future__ import annotations

from typing import Any

from .store import digest

SCHEMA = "corral-inspection-report-validation-v1"
_DENIED = {
    "candidate-code-execution", "shell", "tests", "imports", "build-hooks",
    "package-commands",
}


def validate(adapter_result: dict[str, Any] | None,
             packet_record: dict[str, Any] | None) -> dict[str, Any]:
    """Validate report content against the controller-created packet record.

    This validates data only.  It does not execute or import candidate content and is
    intended to replace a generic verification command for inspection-only tasks.
    """
    errors: list[str] = []
    adapter = adapter_result if isinstance(adapter_result, dict) else {}
    packet = packet_record if isinstance(packet_record, dict) else {}
    structured = adapter.get("structured")
    if adapter.get("schema") != "corral-adapter-result-v1":
        errors.append("adapter result schema mismatch")
    if adapter.get("status") != "completed":
        errors.append("adapter did not report completion")
    if not isinstance(structured, dict):
        errors.append("structured inspection result is missing")
        structured = {}

    report = structured.get("report")
    if not isinstance(report, str) or not report.strip():
        errors.append("inspection report is empty")
        report = ""
    packet_digest = packet.get("digest")
    if not isinstance(packet_digest, str) or structured.get("packet_digest") != packet_digest:
        errors.append("inspection result is not bound to the controller packet")

    provenance = packet.get("provenance")
    if not isinstance(provenance, dict) or structured.get("provenance") != provenance:
        errors.append("inspection result provenance does not match the controller packet")
    else:
        candidate = {key: value for key, value in provenance.items()
                     if key not in ("digest", "documents_digest")}
        if provenance.get("digest") != digest(candidate):
            errors.append("inspection candidate provenance digest is invalid")
        documents = packet.get("documents")
        if (not isinstance(documents, list)
                or provenance.get("documents_digest") != digest(documents)):
            errors.append("inspection document binding digest is invalid")

    capability = packet.get("capability")
    if not isinstance(capability, dict) or structured.get("capability") != capability:
        errors.append("inspection capability receipt does not match the controller packet")
    else:
        if capability.get("mode") != "stateless-inspection-only":
            errors.append("inspection capability mode is invalid")
        if capability.get("tools_supplied") != [] or capability.get("session_reuse") is not False:
            errors.append("inspection capability granted tools or session reuse")
        if not _DENIED.issubset(set(capability.get("denied") or ())):
            errors.append("inspection capability denial set is incomplete")

    normalized = {
        "schema": SCHEMA,
        "accepted": not errors,
        "packet_digest": packet_digest,
        "candidate": provenance,
        "report": report.strip(),
        "report_digest": digest({"report": report.strip()}),
        "errors": errors,
    }
    normalized["digest"] = digest(normalized)
    return normalized
