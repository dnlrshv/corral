from corral.execution.inspection_report import validate
from corral.execution.store import digest


def _records():
    provenance = {"repo": "example/repo", "head": "a" * 40, "base": "b" * 40}
    provenance["digest"] = digest(provenance)
    documents = [{"path": "candidate.py", "kind": "source", "sha256": "d" * 64,
                  "bytes": 12}]
    provenance["documents_digest"] = digest(documents)
    capability = {"mode": "stateless-inspection-only", "tools_supplied": [],
                  "session_reuse": False,
                  "denied": ["candidate-code-execution", "shell", "tests", "imports",
                             "build-hooks", "package-commands"]}
    packet = {"digest": "c" * 64, "provenance": provenance, "capability": capability,
              "documents": documents, "task": "task-1", "attempt": "attempt-1", "generation": 1}
    result = {"schema": "corral-adapter-result-v1", "status": "completed",
              "task": "task-1", "attempt": "attempt-1",
              "structured": {"verdict": "PASS", "report": "One grounded finding.",
                             "packet_digest": packet["digest"],
                             "provenance": provenance, "capability": capability,
                             "task": "task-1", "attempt": "attempt-1", "generation": 1}}
    return result, packet


def test_accepts_exact_controller_bound_inspection_report():
    result, packet = _records()
    receipt = validate(result, packet, task_id="task-1", attempt="attempt-1", generation=1)
    assert receipt["accepted"] is True
    assert receipt["errors"] == []
    assert receipt["digest"] == digest({key: value for key, value in receipt.items()
                                        if key != "digest"})


def test_rejects_candidate_binding_or_capability_substitution():
    result, packet = _records()
    result["structured"]["provenance"] = {"repo": "other/repo"}
    result["structured"]["capability"] = {"tools_supplied": ["shell"]}
    receipt = validate(result, packet, task_id="task-1", attempt="attempt-1", generation=1)
    assert receipt["accepted"] is False
    assert any("provenance" in error for error in receipt["errors"])
    assert any("capability" in error for error in receipt["errors"])


def test_rejects_empty_or_failed_adapter_result():
    result, packet = _records()
    result.update(status="failed", structured={})
    receipt = validate(result, packet, task_id="task-1", attempt="attempt-1", generation=1)
    assert receipt["accepted"] is False
    assert "adapter did not report completion" in receipt["errors"]
    assert "inspection report is empty" in receipt["errors"]


def test_rejects_unversioned_prose_only_verdict():
    result, packet = _records()
    result["structured"].pop("verdict")
    result["structured"]["report"] = "PASS: looks good"
    receipt = validate(result, packet, task_id="task-1", attempt="attempt-1", generation=1)
    assert receipt["accepted"] is False
    assert "inspection verdict must be PASS or CHANGES_REQUIRED" in receipt["errors"]


def test_rejects_trusted_export_not_selected_by_controller():
    result, packet = _records()
    packet["provenance"]["trusted_export_id"] = "e" * 64
    packet["provenance"]["digest"] = digest({key: value for key, value in packet["provenance"].items()
                                             if key not in ("digest", "documents_digest")})
    result["structured"]["provenance"] = packet["provenance"]
    receipt = validate(result, packet, task_id="task-1", attempt="attempt-1", generation=1,
                       trusted_export_id="f" * 64)
    assert receipt["accepted"] is False
    assert "inspection trusted export does not match the controller request" in receipt["errors"]
