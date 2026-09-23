"""Durable exact-candidate PR coordination tests."""
import base64
import hashlib
import json

import pytest

from corral.execution import continuation
from corral.execution.inspection_report import validate
from corral.execution.policy import compute_policy_digest
from corral.execution.pr_coordinator import PRCoordinator
from corral.execution.store import Store, digest


def setup_review(tmp_path, *, verdict="PASS"):
    store = Store(tmp_path / "state.sqlite")
    epoch = store.acquire("pr:fixture/repo#7", "corral")
    selected = {"change.py": {"digest": "d" * 64, "mode": 0o444}}
    export = {"repository": "fixture/repo", "pr_number": 7, "head": "a" * 40,
              "base": "b" * 40, "policy_id": "advisory",
              "policy_digest": "c" * 64, "workspace": str(tmp_path / "snapshot"),
              "selected_files": selected,
              "selected_files_digest": digest(selected), "diff_path": "change.py",
              "diff_sha256": "d" * 64, "export_digest": digest(selected),
              "auth_mode": "fixture-auth"}
    export = {"export_id": digest(export), **export}
    store.put_once("trusted_export", export["export_id"], export)
    task, attempt = "review-task", "review-attempt"
    selection = {"profile": {"id": "inspection-medium", "provider": "fixture",
                             "account_ref": "fixture", "route": "inspection-route",
                             "model": "review-model", "effort": "medium",
                             "harness": "corral-inspection-packet"}}
    store.put_once("request", task, {"role": "review",
                   "trusted_export_id": export["export_id"], "selection": selection})
    store.put_once("state", task, {"status": "completed", "attempt": attempt,
                                   "generation": 1})
    store.put_once("invocation", attempt, {"task": task, "generation": 1})
    provenance = {"repo": "fixture/repo", "pr": 7, "head": "a" * 40,
                  "base": "b" * 40, "export_id": export["export_id"],
                  "trusted_export_id": export["export_id"], "policy_id": "advisory",
                  "policy_digest": "c" * 64}
    provenance["digest"] = digest(provenance)
    documents = [{"path": "change.py", "kind": "source", "sha256": "d" * 64,
                  "bytes": 12}]
    provenance["documents_digest"] = digest(documents)
    capability = {"mode": "stateless-inspection-only", "tools_supplied": [],
                  "session_reuse": False,
                  "denied": ["candidate-code-execution", "shell", "tests", "imports",
                             "build-hooks", "package-commands"]}
    structured = {"verdict": verdict, "report": "Evidence.",
                  "packet_digest": "f" * 64, "provenance": provenance,
                  "capability": capability}
    packet = {"task": task, "attempt": attempt, "generation": 1,
              "digest": structured["packet_digest"], "provenance": provenance,
              "capability": capability, "documents": documents}
    adapter = {"schema": "corral-adapter-result-v1", "status": "completed",
               "task": task, "attempt": attempt, "structured": structured}
    validation = validate(adapter, packet, task_id=task, attempt=attempt, generation=1,
                          trusted_export_id=export["export_id"])
    store.put_once("inspection_validation", f"{task}:{attempt}:g1", validation)
    continuation.record_result(store, task, 1, {
        "accepted": True, "generation": 1, "selection": selection,
        "observed": {"model": "review-model", "harness": "inspection-packet-http"},
        "structured": structured, "inspection_validation": validation})
    enforcement = {"repo": "fixture/repo", "base_sha": "b" * 40,
                   "rulesets": [], "classic_protection": None}
    snapshot = {"repo": "fixture/repo", "base_sha": "b" * 40,
                "enforcement_contents": enforcement,
                "enforcement_digest": compute_policy_digest(enforcement)}
    coordinator = PRCoordinator(store=store, pr="fixture/repo#7", owner_epoch=epoch,
                                publisher="publisher", campaign_authorization="campaign",
                                publication_policy_snapshot=snapshot,
                                publisher_account_ref="publisher-account")
    return store, coordinator, task


def test_pass_review_prepares_exact_advisory_idempotently(tmp_path):
    store, coordinator, task = setup_review(tmp_path)
    accepted = coordinator.accept_review(task)
    assert accepted["stage"] == "reviewed"
    prepared = coordinator.prepare_advisory()
    assert prepared["stage"] == "advisory-prepared"
    intent = prepared["advisory_intent"]
    assert store.get("advisory_payload", intent)["body"] == "Evidence."
    assert store.get("advisory_approval", intent)["authorized_by"] == "campaign"
    assert coordinator.prepare_advisory() == prepared


def test_changes_required_prepares_honest_advisory_but_cannot_merge(tmp_path):
    store, coordinator, task = setup_review(tmp_path, verdict="CHANGES_REQUIRED")
    assert coordinator.accept_review(task)["review_verdict"] == "CHANGES_REQUIRED"
    prepared = coordinator.prepare_advisory()
    assert prepared["stage"] == "advisory-prepared"
    store.replace("pr_coordination", coordinator.pr,
                  {**prepared, "stage": "advisory-delivered"})

    class MergeMustNotRun:
        def merge(self, **_kwargs):
            raise AssertionError("CHANGES_REQUIRED reached merge transport")

    with pytest.raises(PermissionError, match="PASS"):
        coordinator.merge(MergeMustNotRun())


def test_repair_link_requires_exact_accepted_candidate_bytes(tmp_path):
    store, coordinator, review_task = setup_review(tmp_path, verdict="CHANGES_REQUIRED")
    current = coordinator.prepare_advisory() if coordinator.accept_review(
        review_task) else None
    store.replace("pr_coordination", coordinator.pr,
                  {**current, "stage": "advisory-delivered"})
    repair_task, attempt = "repair-task", "repair-attempt"
    candidate = b"VALUE = 2\n"
    candidate_digest = hashlib.sha256(candidate).hexdigest()
    artifact = tmp_path / "repair-artifact"
    artifact.mkdir()
    files = {"change.py": {"digest": candidate_digest,
                           "data": base64.b64encode(candidate).decode(), "mode": 0o644}}
    manifest = {"base": "a" * 40, "files": files}
    manifest["digest"] = digest(manifest)
    (artifact / "candidate_manifest.json").write_text(json.dumps(manifest))
    store.put_once("request", repair_task, {"role": "repair",
                   "candidate_paths": ["change.py"]})
    store.put_once("state", repair_task, {"status": "completed", "attempt": attempt,
                                          "generation": 1})
    store.put_once("invocation", attempt, {"task": repair_task, "generation": 1})
    continuation.record_result(store, repair_task, 1, {
        "accepted": True, "generation": 1, "artifact_directory": str(artifact),
        "receipt": {"candidate_post": manifest["digest"]}})
    selected = {"change.py": {"digest": candidate_digest, "mode": 0o444}}
    export = {"repository": "fixture/repo", "pr_number": 7, "head": "9" * 40,
              "base": "b" * 40, "policy_id": "advisory", "policy_digest": "c" * 64,
              "workspace": str(tmp_path / "replacement"),
              "selected_files": selected,
              "selected_files_digest": digest(selected), "diff_path": "change.py",
              "diff_sha256": candidate_digest, "export_digest": digest(selected),
              "auth_mode": "fixture-auth"}
    export = {"export_id": digest(export), **export}
    store.put_once("trusted_export", export["export_id"], export)
    linked = coordinator.link_repair(repair_task=repair_task,
                                     replacement_export_id=export["export_id"])
    assert linked["stage"] == "repair-linked"
    assert linked["replacement_export_id"] == export["export_id"]

    other, other_attempt = "other-repair", "other-attempt"
    store.put_once("request", other, {"role": "repair", "candidate_paths": ["change.py"]})
    store.put_once("state", other, {"status": "completed", "attempt": other_attempt,
                                    "generation": 1})
    store.put_once("invocation", other_attempt, {"task": other, "generation": 1})
    bad_artifact = tmp_path / "bad-artifact"
    bad_artifact.mkdir()
    bad_manifest = {"base": "a" * 40, "files": {"change.py": {
        "digest": hashlib.sha256(b"DIFFERENT").hexdigest(),
        "data": base64.b64encode(b"DIFFERENT").decode(), "mode": 0o644}}}
    bad_manifest["digest"] = digest(bad_manifest)
    (bad_artifact / "candidate_manifest.json").write_text(json.dumps(bad_manifest))
    continuation.record_result(store, other, 1, {
        "accepted": True, "generation": 1, "artifact_directory": str(bad_artifact),
        "receipt": {"candidate_post": bad_manifest["digest"]}})
    store.replace("pr_coordination", coordinator.pr,
                  {**current, "stage": "advisory-delivered"})
    with pytest.raises(PermissionError, match="bytes differ"):
        coordinator.link_repair(repair_task=other,
                                replacement_export_id=export["export_id"])


def test_stale_owner_epoch_blocks_transition(tmp_path):
    store, coordinator, task = setup_review(tmp_path)
    store.transition_owner("pr:fixture/repo#7", "corral", coordinator.owner_epoch, "released")
    store.acquire("pr:fixture/repo#7", "corral")
    try:
        coordinator.accept_review(task)
    except PermissionError as error:
        assert "epoch" in str(error)
    else:
        raise AssertionError("stale coordinator epoch accepted")


class ReadbackTransport:
    """Records the order of readback and publication; GitHub is never contacted."""

    def __init__(self, late_review):
        self.late_review, self.delivered, self.calls = late_review, False, []

    def _receipt(self):
        self.delivered = True
        return {"review_id": 5, "bridge_actor": "publisher"}

    def reconcile(self, pr, intent, payload):
        self.calls.append("reconcile")
        return self._receipt() if self.late_review else {"status": "absent"}

    def advisory(self, pr, expected, intent, payload):
        self.calls.append("advisory")
        return self._receipt()

    def has_advisory(self, intent):
        return self.delivered


@pytest.mark.parametrize("late_review,calls", [(True, ["reconcile"]),
                                               (False, ["reconcile", "advisory"])])
def test_absent_advisory_is_read_back_before_it_is_resent(tmp_path, late_review, calls):
    store, coordinator, task = setup_review(tmp_path)
    coordinator.accept_review(task)
    intent = coordinator.prepare_advisory()["advisory_intent"]
    payload = store.get("advisory_payload", intent)
    with store.transaction() as db:
        db.execute("INSERT INTO publication_intents VALUES (?,?,?,?,?,?,'absent',?,NULL,NULL,1,1,'a1')",
                   (intent, "pr:" + coordinator.pr, "corral", coordinator.owner_epoch,
                    payload["head"], payload["base"], json.dumps(payload)))
    transport = ReadbackTransport(late_review)
    assert coordinator.publish_advisory(transport)["review_id"] == 5
    assert transport.calls == calls
    assert store.get("pr_coordination", coordinator.pr)["stage"] == "advisory-delivered"
