"""Trusted internal review receipt derivation tests."""
import pytest

from corral.execution import continuation
from corral.execution.inspection_report import validate
from corral.execution.internal_review import record
from corral.execution.store import Store, digest


def _records(tmp_path):
    store = Store(tmp_path / "state.sqlite")
    selected = {"candidate.py": {"digest": "d" * 64, "mode": 0o444}}
    export = {
        "repository": "fixture/repo", "pr_number": 7, "head": "a" * 40,
        "base": "b" * 40, "policy_id": "advisory", "policy_digest": "c" * 64,
        "workspace": str(tmp_path / "snapshot"), "selected_files": selected,
        "selected_files_digest": digest(selected), "diff_path": "candidate.py",
        "diff_sha256": "d" * 64, "export_digest": digest(selected),
        "auth_mode": "fixture-auth",
    }
    export = {"export_id": digest(export), **export}
    store.put_once("trusted_export", export["export_id"], export)
    task, attempt = "task-1", "attempt-1"
    profile = {"id": "inspection-medium", "provider": "fixture",
               "account_ref": "fixture-account", "route": "inspection-route",
               "model": "review-model", "effort": "medium",
               "harness": "corral-inspection-packet"}
    store.put_once("request", task, {
        "role": "review", "trusted_export_id": export["export_id"],
        "selection": {"profile": profile},
    })
    store.put_once("invocation", attempt, {"task": task, "generation": 1})
    store.put_once("state", task, {
        "status": "completed", "attempt": attempt, "generation": 1,
    })
    provenance = {
        "kind": "immutable_snapshot", "repo": export["repository"],
        "pr": export["pr_number"], "head": export["head"], "base": export["base"],
        "export_id": export["export_id"], "trusted_export_id": export["export_id"],
        "policy_id": export["policy_id"], "policy_digest": export["policy_digest"],
    }
    provenance["digest"] = digest(provenance)
    documents = [{"path": "candidate.py", "kind": "source", "sha256": "d" * 64,
                  "bytes": 12}]
    provenance["documents_digest"] = digest(documents)
    capability = {"mode": "stateless-inspection-only", "tools_supplied": [],
                  "session_reuse": False,
                  "denied": ["candidate-code-execution", "shell", "tests", "imports",
                             "build-hooks", "package-commands"]}
    structured = {
        "verdict": "PASS", "report": "No blocking findings.",
        "packet_digest": "f" * 64, "provenance": provenance,
        "capability": capability,
    }
    packet = {"task": task, "attempt": attempt, "generation": 1,
              "digest": structured["packet_digest"], "provenance": provenance,
              "capability": capability, "documents": documents}
    adapter = {"schema": "corral-adapter-result-v1", "status": "completed",
               "task": task, "attempt": attempt, "structured": structured}
    validation = validate(adapter, packet, task_id=task, attempt=attempt, generation=1,
                          trusted_export_id=export["export_id"])
    store.put_once("inspection_validation", f"{task}:{attempt}:g1", validation)
    result = {
        "accepted": True, "generation": 1,
        "selection": {"profile": profile},
        "observed": {"model": "review-model", "harness": "inspection-packet-http"},
        "structured": structured, "inspection_validation": validation,
    }
    continuation.record_result(store, task, 1, result)
    return store, task, export


def test_records_replayable_controller_derived_review_receipt(tmp_path):
    store, task, export = _records(tmp_path)
    receipt = record(store, task)
    assert receipt["pr"] == "fixture/repo#7"
    assert receipt["head"] == export["head"]
    assert receipt["verdict"] == "PASS"
    assert receipt["identity"]["configured"]["provider"] == "fixture"
    assert receipt["identity"]["observed"]["model"] == "review-model"
    assert record(store, task) == receipt


def test_refuses_prose_only_or_candidate_mismatched_review(tmp_path):
    store, task, _export = _records(tmp_path)
    result = continuation.current_result(store, task)
    result["structured"]["provenance"]["head"] = "9" * 40
    store.replace("result", task, result)
    with pytest.raises(PermissionError, match="provenance differs"):
        record(store, task)


def test_refuses_unknown_identity_or_executable_profile(tmp_path):
    store, task, _export = _records(tmp_path)
    result = continuation.current_result(store, task)
    result["observed"] = "unknown"
    store.replace("result", task, result)
    with pytest.raises(PermissionError, match="identity or profile"):
        record(store, task)


def test_refuses_unregistered_configured_to_observed_harness_mapping(tmp_path):
    store, task, _export = _records(tmp_path)
    result = continuation.current_result(store, task)
    result["selection"]["profile"]["harness"] = "arbitrary-wrapper"
    store.replace("result", task, result)
    with pytest.raises(PermissionError, match="identity or profile"):
        record(store, task)
