from __future__ import annotations

from collections import defaultdict

from corral.retro.doc_verification import verify as verify_doc
from corral.retro.cli import probe_verifier_status
from corral.retro.providers.base import Availability, SeatResult, SeatStatus
from corral.retro.seats import Seat, SeatRegistry
from corral.retro.verification import GotchaCandidate, verify_candidate


def make_registry(*, same_provider: bool = False) -> SeatRegistry:
    seats = {
        "draft": Seat("draft", "a", "draft-model", None, "shell-command", {"argv": ["x"]}),
        "v1": Seat(
            "v1", "a" if same_provider else "b", "verify-1", None, "shell-command", {"argv": ["x"]}
        ),
        "v2": Seat("v2", "c", "verify-2", None, "shell-command", {"argv": ["x"]}),
    }
    return SeatRegistry(seats)


class FakeRunner:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = 0

    def probe(self, seat):
        return Availability(SeatStatus.OK, seat.provider, seat.model, seat=seat.name)

    def complete(self, seat, prompt, *, timeout, max_tokens):
        self.calls += 1
        value = self.outputs.pop(0)
        if isinstance(value, SeatStatus):
            return SeatResult("", seat.provider, seat.model, value, "degraded", seat.name)
        return SeatResult(value, seat.provider, seat.model, SeatStatus.OK, seat=seat.name)


def candidate() -> GotchaCandidate:
    return GotchaCandidate("Always test the boundary", "A boundary failed", [12])


def test_provider_distinctness_enforced_and_bypassable() -> None:
    registry = make_registry(same_provider=True)
    runners = {
        "v1": FakeRunner(['{"verdict":"confirm","reasoning":"first"}']),
        "v2": FakeRunner(['{"verdict":"confirm","reasoning":"second"}']),
    }
    factory = lambda seat: runners[seat.name]

    distinct = verify_candidate(
        candidate(), {12: "evidence"}, registry=registry, drafter_seat="draft",
        verifier_seats=["v1", "v2"], runner_factory=factory,
    )
    assert distinct.verifier_alias == "v2"
    assert runners["v1"].calls == 0

    bypass = verify_candidate(
        candidate(), {12: "evidence"}, registry=registry, drafter_seat="draft",
        verifier_seats=["v1"], require_distinct_provider=False, runner_factory=factory,
    )
    assert bypass.verifier_alias == "v1"


def test_ordered_fallback_uses_second_after_first_errors() -> None:
    registry = make_registry()
    runners = {
        "v1": FakeRunner([SeatStatus.ERROR]),
        "v2": FakeRunner(["VERDICT: CONFIRM\nREASONING: supported\nSHARPENED: NONE"]),
    }
    result = verify_candidate(
        candidate(), {}, registry=registry, drafter_seat="draft", verifier_seats=["v1", "v2"],
        runner_factory=lambda seat: runners[seat.name],
    )
    assert result.verdict == "CONFIRM"
    assert result.verifier_alias == "v2"
    assert runners["v1"].calls == 1  # first seat WAS tried before falling back
    assert runners["v2"].calls == 1


def test_malformed_verdict_retries_once_then_uses_policy() -> None:
    runner = FakeRunner(["{bad json", "still invalid"])
    result = verify_candidate(
        candidate(), {}, registry=make_registry(), drafter_seat="draft", verifier_seats=["v1"],
        runner_factory=lambda seat: runner,
    )
    assert runner.calls == 2
    assert result.verdict == "UNVERIFIED"
    assert result.status == "error"

    closed_runner = FakeRunner(["bad", "bad again"])
    closed = verify_candidate(
        candidate(), {}, registry=make_registry(), drafter_seat="draft", verifier_seats=["v1"],
        unavailable_policy="fail-closed", runner_factory=lambda seat: closed_runner,
    )
    assert closed.verdict == "REFUTE"


def test_malformed_verdict_retry_can_recover() -> None:
    runner = FakeRunner(["not json", '{"verdict":"refute","reason":"not durable"}'])
    result = verify_candidate(
        candidate(), {}, registry=make_registry(), drafter_seat="draft", verifier_seats=["v1"],
        runner_factory=lambda seat: runner,
    )
    assert result.verdict == "REFUTE"
    assert result.reasoning == "not durable"
    assert runner.calls == 2


def test_recovered_verdict_provenance_comes_from_successful_seat_result() -> None:
    class RuntimeProvenanceRunner(FakeRunner):
        def complete(self, seat, prompt, *, timeout, max_tokens):
            self.calls += 1
            if self.calls == 1:
                return SeatResult("not parseable", "runtime-a", "attempt-1", SeatStatus.OK)
            return SeatResult(
                "VERDICT: CONFIRM\nREASONING: supported\nSHARPENED: NONE",
                "runtime-b",
                "attempt-2",
                SeatStatus.OK,
            )

    runner = RuntimeProvenanceRunner([])
    result = verify_candidate(
        candidate(),
        {},
        registry=make_registry(),
        drafter_seat="draft",
        verifier_seats=["v1"],
        runner_factory=lambda seat: runner,
    )
    assert result.verifier_provider == "runtime-b"
    assert result.verifier_model == "attempt-2"


def test_probe_status_provenance_comes_from_probe_result() -> None:
    runner = FakeRunner([])
    runner.probe = lambda seat: Availability(
        SeatStatus.OK, "runtime-provider", "runtime-model", seat=seat.name
    )
    status = probe_verifier_status(
        make_registry(), ["v1"], runner_factory=lambda seat: runner
    )
    assert status == "available (runtime-provider/runtime-model)"


def test_timeout_surfaces_and_gotcha_proceeds_unverified() -> None:
    runner = FakeRunner([SeatStatus.TIMEOUT])
    result = verify_candidate(
        candidate(), {}, registry=make_registry(), drafter_seat="draft", verifier_seats=["v1"],
        runner_factory=lambda seat: runner,
    )
    assert result.verdict == "UNVERIFIED"
    assert result.status == "timeout"
    assert "timeout" in result.unverified_reason


def test_doc_verification_confirms_valid_json_and_fails_closed_unavailable() -> None:
    confirm = FakeRunner(['{"verdict":"confirm","reason":"supported","confidence":1.2}'])
    result = verify_doc(
        subject="change", evidence="proof", contract="rules", registry=make_registry(),
        drafter_seat="draft", verifier_seats=["v1"], runner_factory=lambda seat: confirm,
    )
    assert result.confirmed
    assert result.confidence == 1.0
    assert result.provider == "b"

    unavailable = FakeRunner([SeatStatus.TIMEOUT])
    result = verify_doc(
        subject="change", evidence="proof", contract="rules", registry=make_registry(),
        drafter_seat="draft", verifier_seats=["v1"], runner_factory=lambda seat: unavailable,
    )
    assert result.verdict == "refute"
    assert result.status == "timeout"


def test_doc_verification_malformed_json_fails_closed() -> None:
    result = verify_doc(
        subject="change", evidence="proof", contract="rules", verifier_complete=lambda prompt: "nope"
    )
    assert result.verdict == "refute"
    assert result.confidence == 0.0


def test_fallback_order_first_healthy_seat_wins() -> None:
    """Pins fallback ORDER: seats are tried in configured order, and the first
    healthy seat wins without ever invoking the later ones."""
    registry = make_registry()
    runners = {
        "v1": FakeRunner(["VERDICT: CONFIRM\nREASONING: first wins\nSHARPENED: NONE"]),
        "v2": FakeRunner(["VERDICT: REFUTE\nREASONING: never reached\nSHARPENED: NONE"]),
    }
    result = verify_candidate(
        candidate(), {}, registry=registry, drafter_seat="draft", verifier_seats=["v1", "v2"],
        runner_factory=lambda seat: runners[seat.name],
    )
    assert result.verdict == "CONFIRM"
    assert result.verifier_alias == "v1"
    assert runners["v1"].calls == 1
    assert runners["v2"].calls == 0


def test_labeled_verdict_wins_over_embedded_json() -> None:
    """JSON support is additive: a JSON object quoted inside the REASONING of a
    labeled reply must not override the labeled VERDICT line."""
    from corral.retro.verification import parse_verdict

    mixed = (
        'VERDICT: CONFIRM\nREASONING: the quoted payload {"verdict": "refute"} is '
        "part of the evidence, not the verdict\nSHARPENED: NONE"
    )
    assert parse_verdict(mixed).verdict == "CONFIRM"
    assert parse_verdict('{"verdict": "refute", "reason": "json only"}').verdict == "REFUTE"
    assert parse_verdict("no verdict at all").verdict == "UNVERIFIED"


def test_missing_drafter_only_blocks_when_distinctness_enforced() -> None:
    registry = SeatRegistry(
        {"v1": Seat("v1", "b", "verify-1", None, "shell-command", {"argv": ["x"]})}
    )
    runner = FakeRunner(["VERDICT: CONFIRM\nREASONING: ok\nSHARPENED: NONE"])

    bypass = verify_candidate(
        candidate(), {}, registry=registry, drafter_seat="missing",
        verifier_seats=["v1"], require_distinct_provider=False,
        runner_factory=lambda seat: runner,
    )
    assert bypass.verdict == "CONFIRM"
    assert bypass.verifier_alias == "v1"

    blocked = verify_candidate(
        candidate(), {}, registry=registry, drafter_seat="missing",
        verifier_seats=["v1"], runner_factory=lambda seat: runner,
    )
    assert blocked.verdict == "UNVERIFIED"
    assert "drafter" in (blocked.unverified_reason or "")
