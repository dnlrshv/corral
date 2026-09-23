"""Native response semantics: mislabeled failures, usage provenance, identity, continuation.

Acceptance-level cases drive the real controller and the real synthetic harness executable;
parsing-level cases call the real versioned envelope parsers. Nothing is monkeypatched and
no provider is contacted: every payload here is explicitly synthetic fixture evidence.
"""
from __future__ import annotations

import json

import pytest

from corral.execution import containment, envelopes, usage
from corral.execution.workspace import manifest

from . import native_support as ns

pytestmark = pytest.mark.skipif(containment.sandbox_exec() is None,
                                reason="real worker containment requires macOS sandbox-exec")

FIXED_CANDIDATE = "def add(a, b):\n    return a + b\n"
GOOD_OPS = [
    f"write math_ops.py {ns.b64(FIXED_CANDIDATE)}",
    f"result {ns.b64(json.dumps({'answer': 5}))}",
    "narrative Implemented add().",
    f"usage {json.dumps({'input_tokens': 100, 'output_tokens': 20, 'total_tokens': 120})}",
]


def _dispatch(tmp_path, ops, request_id, **env_kwargs):
    env = ns.native_env(tmp_path, **env_kwargs)
    controller = env["controller"]
    spec = ns.native_spec(env, ops=ops)
    task = controller.submit("owner", request_id, spec)
    return env, controller, task, controller.run("owner", task, execution_host=ns.FAKE_HOST)


# ------------------------------------------------------- mislabeled failure / empty results

def test_error_text_at_the_head_of_a_successful_envelope_rejects_the_task(tmp_path):
    """Exit 0 plus status completed is not proof when the payload is a provider error."""
    ops = GOOD_OPS[:2] + ["narrative [API Error: 500] cause: fetch failed", "status completed"]
    env, controller, task, run = _dispatch(tmp_path, ops, "error-in-success")
    result = run["result"]
    assert result["accepted"] is False
    assert ns.adapter_result(run)["status"] == "failed"
    assert any("begins with a provider error marker" in item
               for item in result["adapter_errors"])
    # The workspace change the harness already made is retained, not rolled back silently.
    assert (env["workspace"] / "math_ops.py").read_text() == FIXED_CANDIDATE
    assert controller.store.get("state", task)["status"] == "completed"


#: Real prose long enough that a later error marker is not "the payload begins with" one.
SUBSTANTIVE_PREFIX = (
    "Implemented add(a, b) in math_ops.py, re-read the contract, ran the bound verifier and "
    "confirmed the digest of the candidate before and after verification was identical. "
    "The change is complete and the structured result file was written to the scratch channel. "
)
assert len(SUBSTANTIVE_PREFIX) > 200


def test_error_marker_with_zero_reported_output_tokens_rejects_the_task(tmp_path):
    """The captured qwen-code 0.15.9 signature: success labels, error text, all-zero usage."""
    ops = GOOD_OPS[:2] + [
        f"narrative {SUBSTANTIVE_PREFIX} [API Error: rate limit exceeded]",
        f"usage {json.dumps({'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0})}",
    ]
    _env, _controller, _task, run = _dispatch(tmp_path, ops, "zero-output-error")
    assert run["result"]["accepted"] is False
    assert any("mislabeled failure" in item for item in run["result"]["adapter_errors"])


def test_error_marker_inside_substantive_output_with_tokens_is_only_a_warning(tmp_path):
    """Substantive work that merely mentions an error must not be thrown away."""
    ops = GOOD_OPS[:2] + [
        f"narrative {SUBSTANTIVE_PREFIX} An earlier rate limit exceeded notice was retried.",
        f"usage {json.dumps({'input_tokens': 100, 'output_tokens': 20, 'total_tokens': 120})}",
    ]
    _env, _controller, _task, run = _dispatch(tmp_path, ops, "substantive-warning")
    result = run["result"]
    assert result["accepted"] is True
    assert result["adapter_errors"] == []
    assert any("retained as warning" in item for item in result["adapter_warnings"])


def test_error_marker_at_the_head_of_the_payload_is_rejected_whatever_the_length(tmp_path):
    head = "[API Error: 500] " + SUBSTANTIVE_PREFIX
    payload = json.dumps({"schema": "corral-synthetic-v1", "synthetic": True,
                          "status": "completed", "narrative": head, "result": {"answer": 5},
                          "usage": {"input_tokens": 100, "output_tokens": 20}})
    envelope = envelopes.parse("corral-synthetic-v1", stdout_text=payload, stderr_text="",
                               exit_code=0, invocation="inv", synthetic_expected=True)
    assert not envelope.ok
    assert any("begins with a provider error marker" in item for item in envelope.errors)


def test_empty_structured_result_rejects_the_task_even_with_exit_zero(tmp_path):
    _env, _c, _t, run = _dispatch(tmp_path, GOOD_OPS[:1] + [f"result {ns.b64('{}')}"],
                                  "empty-structured")
    assert run["result"]["accepted"] is False
    assert any("structured completion empty" in item for item in run["result"]["adapter_errors"])


def test_missing_structured_result_rejects_the_task_even_with_exit_zero(tmp_path):
    _env, _c, _t, run = _dispatch(tmp_path, GOOD_OPS[:1] + GOOD_OPS[2:], "missing-structured")
    assert run["result"]["accepted"] is False
    assert any("structured completion missing" in item for item in run["result"]["adapter_errors"])


def test_nonzero_exit_with_an_otherwise_clean_envelope_is_rejected(tmp_path):
    _env, _c, _t, run = _dispatch(tmp_path, GOOD_OPS + ["exit 3"], "nonzero-exit")
    assert run["result"]["accepted"] is False
    assert any("exited 3" in item for item in run["result"]["adapter_errors"])


def test_envelope_status_failed_with_exit_zero_is_rejected(tmp_path):
    _env, _c, _t, run = _dispatch(tmp_path, GOOD_OPS + ["status failed"], "status-failed")
    assert run["result"]["accepted"] is False
    assert any("is not a completion" in item for item in run["result"]["adapter_errors"])


def test_synthetic_envelope_on_a_non_synthetic_route_is_refused(tmp_path):
    """A fixture payload can never masquerade as a live provider response."""
    _env, _c, _t, run = _dispatch(tmp_path, GOOD_OPS, "synthetic-on-live",
                                  synthetic=False, launch_authorized=True)
    assert run["result"]["accepted"] is False
    assert any("non-synthetic route" in item for item in run["result"]["adapter_errors"])


def test_num_turns_alone_is_not_proof_of_completion():
    payload = json.dumps({"schema": "corral-synthetic-v1", "synthetic": True,
                          "status": "completed", "num_turns": 5, "narrative": "did things",
                          "usage": {"input_tokens": 10, "output_tokens": 2}})
    envelope = envelopes.parse("corral-synthetic-v1", stdout_text=payload, stderr_text="",
                               exit_code=0, invocation="inv", synthetic_expected=True)
    assert envelope.ok is False
    assert any("structured completion missing" in item for item in envelope.errors)
    assert envelope.detail["num_turns"] == 5


def test_unknown_envelope_schema_fails_closed():
    with pytest.raises(ValueError, match="unsupported envelope schema"):
        envelopes.parse("made-up-v9", stdout_text="{}", stderr_text="", exit_code=0,
                        invocation="inv")


# --------------------------------------------------------------------------- usage semantics

def test_corrupt_usage_file_preserves_the_candidate_and_records_unknown(tmp_path):
    """A telemetry failure must not discard real work, and unknown is never reported as zero."""
    env = ns.native_env(tmp_path)
    controller = env["controller"]
    spec = ns.native_spec(env, ops=GOOD_OPS)
    task = controller.submit("owner", "corrupt-usage", spec)
    artifacts = controller.artifacts / task
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "native-usage.json").write_text('{"not": "an event list"}')
    run = controller.run("owner", task, execution_host=ns.FAKE_HOST)
    result = run["result"]
    assert result["accepted"] is True
    assert (env["workspace"] / "math_ops.py").read_text() == FIXED_CANDIDATE
    summary = result["usage"]
    assert {"reason": "native event read/ingest errors", "classes": ["ValueError"]} in summary["unknown"]
    assert summary["coverage"] == "partial"
    # The good counters still arrived, attributed to their synthetic origin.
    assert summary["synthetic_fields"] == {"input_tokens": 100, "output_tokens": 20,
                                           "total_tokens": 120}
    assert summary["measured_fields"] == {} and summary["estimated_fields"] == {}
    assert summary["account_total"] is None and summary["combined_tokens"] is None
    # Raw buffered metadata is retained even though one ingest attempt failed.
    assert (artifacts / "harness.stdout").read_text().strip()
    assert ns.adapter_result(run)["usage_events"]
    assert ns.attempts(run)[0]["usage_events"] == 1


def test_missing_usage_alone_warns_and_still_accepts_a_good_candidate(tmp_path):
    ops = [op for op in GOOD_OPS if not op.startswith("usage")]
    _env, _c, _t, run = _dispatch(tmp_path, ops, "no-usage")
    result = run["result"]
    assert result["accepted"] is True
    assert any("usage stays unknown, not zero" in item for item in result["adapter_warnings"])
    assert result["usage"]["observed_fields"] == {}
    assert result["usage"]["unknown"] and result["usage"]["coverage"] == "partial"
    # Absence is never rendered as a zero counter.
    assert "input_tokens" not in result["usage"]["observed_fields"]


def _event(sequence, counters, *, mode="cumulative", origin="native-measured", scope="session",
           invocation="inv", epoch=0):
    return {"id": f"e{sequence}", "invocation": invocation, "scope": scope, "epoch": epoch,
            "sequence": sequence, "mode": mode, "counters": counters, "origin": origin,
            "schema": "test"}


def test_summarize_keeps_cumulative_snapshots_and_sums_deltas():
    cumulative = usage.summarize([_event(1, {"input_tokens": 10}), _event(2, {"input_tokens": 25})])
    assert cumulative["measured_fields"] == {"input_tokens": 25}
    deltas = usage.summarize([_event(1, {"input_tokens": 10}, mode="delta"),
                              _event(2, {"input_tokens": 15}, mode="delta")])
    assert deltas["measured_fields"] == {"input_tokens": 25}


def test_summarize_never_folds_cache_or_reasoning_subsets_into_input_output():
    summary = usage.summarize([_event(1, {"input_tokens": 100, "output_tokens": 20,
                                          "thinking_tokens": 8, "cache_read_tokens": 40,
                                          "cache_write_tokens": 5, "total_tokens": 173})])
    fields = summary["measured_fields"]
    assert fields == {"input_tokens": 100, "output_tokens": 20, "thinking_tokens": 8,
                      "cache_read_tokens": 40, "cache_write_tokens": 5, "total_tokens": 173}
    # Reasoning/cache subsets stay their own fields: they are not folded into input/output,
    # and the reported total is carried through rather than recomputed from the subsets.
    assert fields["input_tokens"] == 100 and fields["output_tokens"] == 20
    assert fields["total_tokens"] == 173
    assert summary["observed_fields"] == fields


def test_summarize_rejects_mixed_origins_gaps_and_resets_as_unknown():
    mixed = usage.summarize([_event(1, {"input_tokens": 5}, origin="native-measured"),
                             _event(2, {"input_tokens": 5}, origin="synthetic-fixture")])
    assert any("mixed usage origins" in item["reason"] for item in mixed["unknown"])
    assert mixed["measured_fields"] == {} and mixed["synthetic_fields"] == {}
    gap = usage.summarize([_event(1, {"input_tokens": 5}, mode="delta"),
                           _event(3, {"input_tokens": 5}, mode="delta")])
    assert any("delta sequence gap" in item["reason"] for item in gap["unknown"])
    reset = usage.summarize([_event(1, {"input_tokens": 50}), _event(2, {"input_tokens": 10})])
    assert any("unmarked counter reset" in item["reason"] for item in reset["unknown"])
    assert reset["measured_fields"] == {}
    ambiguous = usage.summarize([_event(1, {"input_tokens": 5}),
                                 _event(2, {"input_tokens": 5}, mode="delta")])
    assert any("ambiguous counter mode" in item["reason"] for item in ambiguous["unknown"])


def test_summarize_keeps_estimates_out_of_measured_counters_and_reports_unseen_invocations():
    summary = usage.summarize([_event(1, {"input_tokens": 100}, origin="native-measured"),
                               _event(1, {"input_tokens": 7}, origin="estimated",
                                      scope="corral-estimate")],
                              registered=["inv", "never-reported"])
    assert summary["measured_fields"] == {"input_tokens": 100}
    assert summary["estimated_fields"] == {"input_tokens": 7}
    # An estimate is never summed into a provider-reported counter.
    assert summary["observed_fields"] == {"input_tokens": 100}
    assert {"invocation": "never-reported", "reason": "no native events"} in summary["unknown"]
    # Mixing an estimate into a measured counter group is refused rather than blended.
    blended = usage.summarize([_event(1, {"input_tokens": 100}, origin="native-measured"),
                               _event(2, {"input_tokens": 7}, origin="estimated")])
    assert blended["measured_fields"] == {} and blended["estimated_fields"] == {}
    assert any("mixed usage origins" in item["reason"] for item in blended["unknown"])


def test_summarize_rejects_invalid_counter_metadata_and_unrecognized_origins():
    bad = dict(_event(1, {"input_tokens": 5}), sequence=0)
    assert any("invalid native counter metadata" in item["reason"]
               for item in usage.summarize([bad])["unknown"])
    unknown_origin = _event(1, {"input_tokens": 5}, origin="made-up")
    summary = usage.summarize([unknown_origin])
    assert any("unrecognized usage origin" in item["reason"] for item in summary["unknown"])
    assert summary["observed_fields"] == {}


def test_agy_envelopes_are_cumulative_measured_with_separate_cache_and_reasoning_fields():
    payload = json.dumps({"status": "success", "conversation_id": "conv-1", "num_turns": 3,
                          "response": "implemented the change",
                          "usage": {"input_tokens": 1500, "output_tokens": 350,
                                    "thinking_tokens": 200, "cache_read_tokens": 4000,
                                    "unsupported_metric": 99999}})
    envelope = envelopes.parse("agy-json-v1", stdout_text=payload, stderr_text="", exit_code=0,
                               invocation="inv", log_text="")
    assert envelope.ok and envelope.status == "SUCCESS"
    assert len(envelope.usage_events) == 1
    event = envelope.usage_events[0]
    assert event["mode"] == "cumulative" and event["origin"] == "native-measured"
    assert event["session_id"] == "conv-1"
    # Undeclared counters are dropped rather than folded into a known one.
    assert event["counters"] == {"input_tokens": 1500, "output_tokens": 350,
                                 "thinking_tokens": 200, "cache_read_tokens": 4000}
    # With no selection evidence the observed model stays unknown rather than being inferred.
    assert "model" not in envelope.identity
    assert any("observed model stays unknown" in item for item in envelope.warnings)
    labeled = envelopes.parse("agy-json-v1", stdout_text=payload, stderr_text="", exit_code=0,
                              invocation="inv",
                              log_text='selected model override to backend: label="gemini-x"')
    # A harness label is recorded as a label, and explicitly not as provider attestation.
    assert labeled.identity["harness_model_label"] == "gemini-x"
    assert "model" not in labeled.identity
    assert any("not independent provider attestation" in item for item in labeled.warnings)


def test_agy_failure_shapes_are_rejected():
    failed = envelopes.parse("agy-json-v1", stdout_text=json.dumps({"status": "error",
                                                                   "response": "x"}),
                             stderr_text="", exit_code=0, invocation="inv")
    assert not failed.ok and any("reported status" in item for item in failed.errors)
    empty = envelopes.parse("agy-json-v1", stdout_text=json.dumps({"status": "success",
                                                                 "response": "   "}),
                            stderr_text="", exit_code=0, invocation="inv")
    assert any("empty result payload" in item for item in empty.errors)
    unparsable = envelopes.parse("agy-json-v1", stdout_text="not json", stderr_text="boom",
                                 exit_code=1, invocation="inv")
    assert unparsable.status == "failed" and unparsable.detail["stderr_tail"] == "boom"


# --------------------------------------------------------------------------- identity truth

def test_harness_that_misreports_its_model_is_not_accepted(tmp_path):
    """Observed identity is compared field by field; success never proves it."""
    ops = GOOD_OPS + [f"identity {json.dumps({'model': 'some-other-model'})}"]
    _env, _c, _t, run = _dispatch(tmp_path, ops, "identity-lie")
    result = run["result"]
    assert result["accepted"] is False
    assert result["identity_mismatch"] == ["model"]
    assert result["observed"]["model"] == "some-other-model"
    assert ns.adapter_result(run)["identity_requested"]["model"] == ns.FAKE_MODEL


def test_missing_identity_block_leaves_observed_unknown():
    payload = json.dumps({"schema": "corral-synthetic-v1", "synthetic": True,
                          "status": "completed", "narrative": "done",
                          "result": {"answer": 5}})
    envelope = envelopes.parse("corral-synthetic-v1", stdout_text=payload, stderr_text="",
                               exit_code=0, invocation="inv", synthetic_expected=True)
    assert envelope.identity == {}
    assert any("observed stays unknown" in item for item in envelope.warnings)
    assert envelope.ok is True  # unknown identity is telemetry, not a fabricated provider claim


def test_synthetic_envelope_not_marked_synthetic_is_refused():
    payload = json.dumps({"schema": "corral-synthetic-v1", "status": "completed",
                          "result": {"answer": 5}})
    envelope = envelopes.parse("corral-synthetic-v1", stdout_text=payload, stderr_text="",
                               exit_code=0, invocation="inv", synthetic_expected=True)
    assert not envelope.ok
    assert any("not explicitly marked synthetic" in item for item in envelope.errors)


# ------------------------------------------------- continuation, checkpoints, safe return

def test_same_task_continuation_keeps_ordered_history_and_dispatches_once(tmp_path):
    env = ns.native_env(tmp_path)
    controller = env["controller"]
    spec = ns.native_spec(env, ops=GOOD_OPS)
    task = controller.submit("owner", "continuation", spec)
    controller.steer("owner", task, "amend-1", {"objective": ns.write_ops("narrative first pass")})
    controller.steer("owner", task, "amend-2", {"objective": ns.write_ops(*GOOD_OPS)})
    run = controller.run("owner", task, execution_host=ns.FAKE_HOST)
    prompt = (env["state"] / "scratch" / task / "prompt.md").read_text()
    assert "Prior amendments for this same task identity (continue, do not restart):" in prompt
    first, second = prompt.index("amend-1"), prompt.index("amend-2")
    assert first < second  # checkpoint order is preserved, not re-sorted or dropped
    history = [json.loads(line[2:]) for line in prompt.splitlines() if line.startswith("- {")]
    assert [item["amendment_id"] for item in history] == ["amend-1", "amend-2"]
    assert [item["sequence"] for item in history] == [1, 2]
    # One dispatch for the whole continuation: no second worker, no duplicate claim.
    assert len(ns.attempts(run)) == 1
    assert len(controller.store.records("claim")) == 1
    assert run["result"]["accepted"] is True
    assert controller.store.get("state", task)["status"] == "completed"
    # Re-running the same task identity returns the retained result instead of dispatching.
    again = controller.run("owner", task, execution_host=ns.FAKE_HOST)
    assert again["result"] == run["result"]
    assert len(controller.store.records("claim")) == 1
    assert env["workspace"].is_dir()


def test_steering_cannot_grant_authority_or_replace_execution(tmp_path):
    env = ns.native_env(tmp_path)
    controller = env["controller"]
    task = controller.submit("owner", "steer-guard", ns.native_spec(env, ops=GOOD_OPS))
    with pytest.raises(PermissionError, match="cannot grant authority"):
        controller.steer("owner", task, "amend-bad", {"selection": {"profile": {"id": "x"}}})
    with pytest.raises(PermissionError, match="cannot grant authority"):
        controller.steer("owner", task, "amend-bad2", {"command": ["/bin/sh"]})


def test_transfer_refuses_a_newer_dev_change_and_preserves_it(tmp_path):
    """Safe return: a stale expected manifest never overwrites newer developer work."""
    env, controller, task, run = _dispatch(tmp_path, GOOD_OPS, "newer-dev")
    assert run["result"]["accepted"] is True
    workspace = env["workspace"]
    incoming = manifest(workspace, ["math_ops.py"])
    expected = manifest(workspace, ["math_ops.py"])
    newer = "# newer developer work, not produced by Corral\n"
    (workspace / "math_ops.py").write_text(newer)
    with pytest.raises(PermissionError, match="checkout changed; refuse overwrite"):
        controller.transfer("owner", task, "return-1", incoming, expected)
    assert (workspace / "math_ops.py").read_text() == newer
    # An interrupted return is uncertain and must be reconciled, not silently released.
    owner, _epoch, status = controller.store.ownership(
        "workspace:" + str(workspace.resolve()))
    assert status == "uncertain" and owner.startswith("transfer:")
    with pytest.raises(PermissionError, match="still owned or outcome uncertain"):
        controller.transfer("owner", task, "return-2", incoming, expected)
