"""Controller-derived receipts for tool-free model reviews.

The receipt is created only from records already held by the authoritative Store.  A caller
selects the completed task; it cannot provide candidate, model, verdict, or policy labels.
"""
from __future__ import annotations

import re
from typing import Any

from . import continuation
from .store import digest

SCHEMA = "corral-internal-review-v1"
_HARNESS_OBSERVATIONS = {
    "corral-inspection-packet": frozenset({"inspection-packet-http"}),
    # Controller-only fixture proves the same split without claiming a live provider.
    "packet-fixture": frozenset({"packet-fixture"}),
}
_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
def _trusted_export(store, export_id: Any) -> dict[str, Any]:
    if not isinstance(export_id, str) or not _SHA256.fullmatch(export_id):
        raise PermissionError("review task does not select a trusted export")
    record = store.get("trusted_export", export_id)
    if not isinstance(record, dict) or record.get("export_id") != export_id:
        raise PermissionError("review trusted export is unavailable")
    if digest({key: value for key, value in record.items() if key != "export_id"}) != export_id:
        raise PermissionError("review trusted export identity is invalid")
    required = ("repository", "pr_number", "head", "base", "policy_id", "policy_digest")
    if any(key not in record for key in required):
        raise PermissionError("review trusted export binding is incomplete")
    if (not isinstance(record["repository"], str) or not record["repository"]
            or isinstance(record["pr_number"], bool)
            or not isinstance(record["pr_number"], int) or record["pr_number"] <= 0
            or not _SHA.fullmatch(str(record["head"]))
            or not _SHA.fullmatch(str(record["base"]))
            or not isinstance(record["policy_id"], str) or not record["policy_id"]
            or not _SHA256.fullmatch(str(record["policy_digest"]))):
        raise PermissionError("review trusted export candidate is invalid")
    files = record.get("selected_files")
    diff_path = record.get("diff_path")
    if (not isinstance(files, dict) or not files or not isinstance(diff_path, str)
            or diff_path not in files or record.get("selected_files_digest") != digest(files)
            or record.get("export_digest") != record.get("selected_files_digest")
            or record.get("diff_sha256") != files[diff_path].get("digest")):
        raise PermissionError("review trusted export file binding is invalid")
    return record


def _structured(store, result: dict[str, Any], export: dict[str, Any], task_id: str,
                attempt: str, generation: int) -> tuple[str, dict[str, Any]]:
    structured = result.get("structured")
    if not isinstance(structured, dict):
        raise PermissionError("accepted inspection result has no structured report")
    report = structured.get("report")
    validation = result.get("inspection_validation")
    if not isinstance(validation, dict):
        raise PermissionError("accepted inspection result has no controller validation")
    validation_key = f"{task_id}:{attempt}:g{generation}"
    if store.get("inspection_validation", validation_key) != validation:
        raise PermissionError("internal review validation is not the controller Store receipt")
    validation_fields = {
        "schema", "accepted", "packet_digest", "candidate", "task", "attempt",
        "generation", "trusted_export_id", "verdict", "report", "report_digest", "errors",
    }
    if set(validation) != validation_fields | {"digest"}:
        raise PermissionError("inspection controller validation schema is not canonical")
    validation_core = {key: validation[key] for key in validation_fields}
    if (validation_core["schema"] != "corral-inspection-report-validation-v1"
            or validation_core["accepted"] is not True or validation_core["errors"] != []
            or validation["digest"] != digest(validation_core)
            or validation["task"] != task_id or validation["attempt"] != attempt
            or validation["generation"] != generation
            or validation["trusted_export_id"] != export["export_id"]):
        raise PermissionError("inspection controller validation is missing, rejected, or unbound")
    verdict = validation_core["verdict"]
    if verdict not in {"PASS", "CHANGES_REQUIRED"}:
        raise PermissionError("inspection controller validation has an invalid verdict")
    packet = structured.get("packet_digest")
    provenance = structured.get("provenance")
    capability = structured.get("capability")
    if not isinstance(packet, str) or not _SHA256.fullmatch(packet):
        raise PermissionError("inspection report has no packet digest")
    if (not isinstance(provenance, dict)
            or provenance.get("trusted_export_id") != export["export_id"]
            or provenance.get("export_id") != export["export_id"]
            or provenance.get("repo") != export["repository"]
            or provenance.get("pr") != export["pr_number"]
            or provenance.get("head") != export["head"]
            or provenance.get("base") != export["base"]
            or provenance.get("policy_id") != export["policy_id"]
            or provenance.get("policy_digest") != export["policy_digest"]):
        raise PermissionError("inspection report provenance differs from its trusted export")
    if (validation_core["candidate"] != provenance
            or validation_core["packet_digest"] != packet
            or validation_core["report"] != str(report).strip()
            or validation_core["report_digest"] != digest({"report": str(report).strip()})):
        raise PermissionError("inspection controller validation differs from report evidence")
    if (not isinstance(capability, dict)
            or capability.get("mode") != "stateless-inspection-only"
            or capability.get("tools_supplied") != []
            or capability.get("session_reuse") is not False):
        raise PermissionError("inspection report does not prove the tool-free stateless profile")
    if provenance.get("digest") != digest(
            {key: value for key, value in provenance.items()
             if key not in {"digest", "documents_digest"}}):
        raise PermissionError("inspection candidate provenance digest is invalid")
    if (provenance.get("task") not in (None, task_id)
            or provenance.get("attempt") not in (None, attempt)
            or provenance.get("generation") not in (None, generation)):
        raise PermissionError("inspection report provenance differs from its invocation")
    return verdict, {"report": report.strip(), "packet_digest": packet,
                     "provenance_digest": provenance["digest"]}


def record(store, task_id: str) -> dict[str, Any]:
    """Create or replay one immutable exact-candidate internal review receipt."""
    request = store.get("request", task_id)
    state = store.get("state", task_id)
    if not isinstance(request, dict) or request.get("role") != "review":
        raise PermissionError("internal review receipt requires a controller review task")
    export = _trusted_export(store, request.get("trusted_export_id"))
    if not isinstance(state, dict) or state.get("status") != "completed":
        raise PermissionError("internal review task is not terminal and completed")
    generation = continuation.generation_of(state)
    result = continuation.results(store, task_id).get(generation)
    if not isinstance(result, dict) or result.get("accepted") is not True:
        raise PermissionError("internal review result is not accepted for its state generation")
    if continuation.generation_of(result) != generation:
        raise PermissionError("internal review state and result generations differ")
    attempt = state.get("attempt")
    invocation = store.get("invocation", attempt) if isinstance(attempt, str) else None
    if (not isinstance(invocation, dict) or invocation.get("task") != task_id
            or continuation.generation_of(invocation) != generation):
        raise PermissionError("internal review invocation binding is unavailable")
    observed = result.get("observed")
    selection = result.get("selection") or request.get("selection")
    profile = (selection or {}).get("profile") if isinstance(selection, dict) else None
    if (not isinstance(observed, dict)
            or not all(isinstance(observed.get(key), str) and observed[key]
                       for key in ("model", "harness"))
            or not isinstance(profile, dict) or not profile.get("id")
            or observed.get("model") != profile.get("model")
            or observed.get("harness") not in _HARNESS_OBSERVATIONS.get(
                profile.get("harness"), frozenset())
            or not all(isinstance(profile.get(key), str) and profile[key]
                       for key in ("provider", "account_ref", "route", "harness"))):
        raise PermissionError("internal review observed identity or profile is incomplete")
    verdict, report = _structured(store, result, export, task_id, attempt, generation)
    receipt = {
        "schema": SCHEMA, "task": task_id, "attempt": attempt, "generation": generation,
        "export_id": export["export_id"], "repo": export["repository"],
        "pr": f"{export['repository']}#{export['pr_number']}",
        "head": export["head"], "base": export["base"],
        "policy_id": export["policy_id"], "review_policy_digest": export["policy_digest"],
        "profile_id": profile["id"], "verdict": verdict,
        "identity": {
            "configured": {key: profile.get(key) for key in
                           ("provider", "account_ref", "route", "model", "effort", "harness")},
            "observed": {key: observed.get(key) for key in ("model", "harness")},
            "coverage": {"provider": "controller-configured-not-provider-attested",
                         "account_ref": "controller-configured-not-provider-attested",
                         "model": "provider-response-observed",
                         "effort": "requested-not-provider-attested"},
        },
        "report_digest": digest(report), "packet_digest": report["packet_digest"],
    }
    receipt_id = digest(receipt)
    receipt = {"receipt_id": receipt_id, **receipt}
    store.put_once("internal_review_receipt", receipt_id, receipt)
    return receipt


def matching(store, *, pr: str, head: str, base: str, policy_digest: str,
             requirements: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Resolve each controller policy requirement to a distinct PASS receipt."""
    receipts = list(store.records("internal_review_receipt").values())
    matched, used = [], set()
    for requirement in requirements:
        found = next((item for item in receipts if item.get("receipt_id") not in used
                      and item.get("schema") == SCHEMA and item.get("pr") == pr
                      and item.get("head") == head and item.get("base") == base
                      and item.get("review_policy_digest") == policy_digest
                      and item.get("verdict") == "PASS"
                      and item.get("receipt_id") == digest(
                          {key: value for key, value in item.items() if key != "receipt_id"})
                      and all(item.get(key) == value for key, value in requirement.items())), None)
        if found is None:
            raise PermissionError("required internal model review is missing")
        used.add(found["receipt_id"])
        matched.append(found)
    return matched


def load(store, receipt_id: str) -> dict[str, Any]:
    """Load one digest-valid immutable receipt and rebind it to its accepted task result."""
    receipt = store.get("internal_review_receipt", receipt_id)
    if (not isinstance(receipt, dict) or receipt.get("receipt_id") != receipt_id
            or digest({key: value for key, value in receipt.items()
                       if key != "receipt_id"}) != receipt_id):
        raise PermissionError("internal review receipt is unavailable or corrupted")
    current = record(store, receipt.get("task"))
    if current != receipt:
        raise PermissionError("internal review receipt no longer matches controller evidence")
    return receipt


def report_body(store, receipt: dict[str, Any]) -> str:
    """Return the exact report whose digest is bound by a validated receipt."""
    result = continuation.results(store, receipt["task"]).get(receipt["generation"]) or {}
    structured = result.get("structured") or {}
    report = structured.get("report")
    bound = {"report": str(report).strip(), "packet_digest": structured.get("packet_digest"),
             "provenance_digest": (structured.get("provenance") or {}).get("digest")}
    if digest(bound) != receipt["report_digest"]:
        raise PermissionError("internal review report bytes differ from its receipt")
    return bound["report"]
