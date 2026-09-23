import json
from pathlib import Path
from tempfile import TemporaryDirectory
from corral.execution.usage import Spool
from corral.execution.store import Store
from corral.execution.envelopes import parse
from corral.execution.completion import publish_usage

def test_codex_duplicate_turn_completed():
    fixture_path = Path(__file__).parent / "fixtures" / "qwen_codex_turn.jsonl"
    stdout_text = fixture_path.read_text()
    # Add a duplicate turn.completed to simulate the duplicate terminal events
    stdout_text += '{"type":"turn.completed","usage":{"input_tokens":1250000,"cached_input_tokens":1100000,"output_tokens":34000,"reasoning_output_tokens":12000}}\n'

    envelope = parse("codex-jsonl-v1", stdout_text=stdout_text, stderr_text="", exit_code=0, invocation="inv-1")
    assert envelope.status == "completed"
    assert len(envelope.usage_events) == 1

    event = envelope.usage_events[0]
    assert event["mode"] == "cumulative"
    assert event["counters"]["input_tokens"] == 1250000
    assert event["counters"]["cache_read_tokens"] == 1100000
    assert event["counters"]["output_tokens"] == 34000
    assert event["counters"]["thinking_tokens"] == 12000
    assert envelope.detail["duplicate_terminal_events"] == 2
    assert "deduplicated to single snapshot" in "\n".join(envelope.warnings)

def test_codex_duplicate_turn_completed_conflict():
    fixture_path = Path(__file__).parent / "fixtures" / "qwen_codex_turn.jsonl"
    stdout_text = fixture_path.read_text()
    stdout_text += '{"type":"turn.completed","usage":{"input_tokens":15,"output_tokens":20}}\n'

    envelope = parse("codex-jsonl-v1", stdout_text=stdout_text, stderr_text="", exit_code=0, invocation="inv-1")
    assert envelope.status == "completed"
    assert len(envelope.usage_events) == 0
    assert "multi-turn aggregation is ambiguous" in "\n".join(envelope.warnings)

def test_failed_native_attempt_emits_usage():
    fixture_path = Path(__file__).parent / "fixtures" / "qwen_codex_turn.jsonl"
    stdout_text = fixture_path.read_text()

    envelope = parse("codex-jsonl-v1", stdout_text=stdout_text, stderr_text="some error", exit_code=1, invocation="inv-failed")
    assert envelope.status == "failed"
    assert len(envelope.usage_events) == 1

    with TemporaryDirectory() as d:
        path = Path(d)
        spool = Spool(path / "spool")
        for ev in envelope.usage_events:
            ev["epoch"] = 1
            spool.append(ev)

        store = Store(path / "store")
        publish_usage(spool, store, output=path, spec={"soft_thresholds": {}}, attempt="inv-failed", telemetry_errors=[])

        usage = json.loads((path / "usage.json").read_text())
        assert usage["observed_fields"]["input_tokens"] == 1250000

def test_resume_accounting_rejected():
    fixture_path = Path(__file__).parent / "fixtures" / "qwen_codex_turn.jsonl"
    stdout_text = fixture_path.read_text()

    from corral.execution.envelope_streams import parse_codex
    envelope = parse_codex(stdout_text=stdout_text, stderr_text="", exit_code=0, invocation="inv-resume", result_path=None, resumed=True)
    assert len(envelope.usage_events) == 0
    assert "codex session was resumed" in "\n".join(envelope.warnings)

def test_spool_duplicate_replay():
    with TemporaryDirectory() as d:
        path = Path(d)
        spool = Spool(path / "spool")

        ev1 = {
            "invocation": "logical-id",
            "scope": "session",
            "id": "logical-id-turn-1",
            "mode": "cumulative",
            "epoch": 1,
            "sequence": 1,
            "origin": "native-measured",
            "counters": {"input_tokens": 10, "output_tokens": 20},
            "schema": "synthetic-fixture"
        }
        spool.append(ev1)
        spool.append(ev1) # duplicate replay

        store = Store(path / "store")
        publish_usage(spool, store, output=path, spec={}, attempt="logical-id", telemetry_errors=[])

        usage = json.loads((path / "usage.json").read_text())
        assert usage["observed_fields"]["input_tokens"] == 10

        unknown_reasons = {u.get("invocation"): u.get("reason") for u in usage["unknown"]}
        assert unknown_reasons.get("desktop-root") == "no native events"

        assert usage["account_total"] is None
        assert usage["combined_tokens"] is None
        assert usage["desktop_root"] == "uncovered"

def test_spool_repair_sums_cumulatives_across_epochs():
    with TemporaryDirectory() as d:
        path = Path(d)
        spool = Spool(path / "spool")

        # Failed native attempt
        ev1 = {
            "invocation": "logical-id",
            "scope": "session",
            "id": "logical-id-turn-1",
            "mode": "cumulative",
            "epoch": 1,
            "sequence": 1,
            "origin": "native-measured",
            "counters": {"input_tokens": 10, "output_tokens": 20},
            "schema": "synthetic-fixture"
        }
        spool.append(ev1)

        # Repair attempt (fresh checkpoint generation, same logical ID, different epoch)
        ev2 = {
            "invocation": "logical-id",
            "scope": "session",
            "id": "logical-id-repair-turn-1",
            "mode": "cumulative",
            "epoch": 2,
            "sequence": 1,
            "origin": "native-measured",
            "counters": {"input_tokens": 15, "output_tokens": 30},
            "schema": "synthetic-fixture"
        }
        spool.append(ev2)

        store = Store(path / "store")
        publish_usage(spool, store, output=path, spec={}, attempt="logical-id", telemetry_errors=[])

        usage = json.loads((path / "usage.json").read_text())
        # Both cumulative records from different epochs should be summed: 10 + 15 = 25
        assert usage["observed_fields"]["input_tokens"] == 25
        assert usage["observed_fields"]["output_tokens"] == 50
