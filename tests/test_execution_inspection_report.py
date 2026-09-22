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
              "documents": documents}
    result = {"schema": "corral-adapter-result-v1", "status": "completed",
              "structured": {"report": "One grounded finding.",
                             "packet_digest": packet["digest"],
                             "provenance": provenance, "capability": capability}}
    return result, packet


def test_accepts_exact_controller_bound_inspection_report():
    result, packet = _records()
    receipt = validate(result, packet)
    assert receipt["accepted"] is True
    assert receipt["errors"] == []
    assert receipt["digest"] == digest({key: value for key, value in receipt.items()
                                        if key != "digest"})


def test_rejects_candidate_binding_or_capability_substitution():
    result, packet = _records()
    result["structured"]["provenance"] = {"repo": "other/repo"}
    result["structured"]["capability"] = {"tools_supplied": ["shell"]}
    receipt = validate(result, packet)
    assert receipt["accepted"] is False
    assert any("provenance" in error for error in receipt["errors"])
    assert any("capability" in error for error in receipt["errors"])


def test_rejects_empty_or_failed_adapter_result():
    result, packet = _records()
    result.update(status="failed", structured={})
    receipt = validate(result, packet)
    assert receipt["accepted"] is False
    assert "adapter did not report completion" in receipt["errors"]
    assert "inspection report is empty" in receipt["errors"]
