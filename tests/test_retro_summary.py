from __future__ import annotations

from corral.retro.mining import GotchaCandidate
from corral.retro.summary import render_summary
from corral.retro.types import VerifiedCandidate
from corral.retro.verification import CandidateVerification

# Representative tokens a rendered summary must never echo: raw provider
# vocabulary, owner/repo identifiers, foreign issue references, private
# framework paths. Values are synthetic — the guard is the mechanism.
FORBIDDEN_TERMS = ("acme-corp", "example-owner/example-repo", "#9001", "internal-framework")


def candidate(rule="Always X") -> GotchaCandidate:
    return GotchaCandidate(
        rule=rule,
        workflow_kinds=["fix-issue"],
        repo_paths=["src/a.py"],
        surface_ids=[],
        source_prs=[1, 2],
        control_type="prompt_only",
        control_path=None,
        inject_into_briefer=True,
        confidence=0.9,
        rationale="r",
        severity="info",
        created="2026-08-03",
        evidence_key="k",
    )


def verified(verdict="CONFIRM", reasoning="", sharpened=None, provider="vendor-b", model="verify-model", unverified_reason=None) -> VerifiedCandidate:
    return VerifiedCandidate(
        candidate=candidate(),
        original_rule="Always X",
        verification=CandidateVerification(
            verdict=verdict,
            reasoning=reasoning,
            sharpened_rule=sharpened,
            unverified_reason=unverified_reason,
            verifier_provider=provider,
            verifier_model=model,
        ),
    )


def entry(gotcha_id="G-2026-001") -> dict:
    return {
        "id": gotcha_id,
        "rule": "Always X",
        "workflow_kinds": ["fix-issue"],
        "repo_paths": ["src/a.py"],
        "surface_ids": [],
        "source_prs": [1, 2],
        "source_refs": [],
        "control_type": "prompt_only",
        "expires": "2026-11-01",
    }


def test_summary_renders_entries_with_seat_provenance() -> None:
    text = render_summary(
        since="2026-08-03",
        until="2026-08-09",
        total_groups=4,
        qualified_groups=2,
        dedup_skipped=1,
        llm_skipped=[],
        entries_with_verification=[(entry(), verified())],
        refuted=[],
        severity_issues=[],
        dry_run=True,
        verification_status="available (vendor-b/verify-model)",
    )
    assert "# Agent Retrospective — 2026-08-03 to 2026-08-09" in text
    assert "confirmed by vendor-b/verify-model" in text
    assert "DRY RUN (nothing written)" in text
    assert "G-2026-001" in text
    lowered = text.lower()
    for term in FORBIDDEN_TERMS:
        assert term not in lowered, f"private vocabulary leaked: {term}"


def test_summary_renders_refuted_and_unverified() -> None:
    text = render_summary(
        since="2026-08-03",
        until="2026-08-09",
        total_groups=1,
        qualified_groups=1,
        dedup_skipped=0,
        llm_skipped=[{"key": "k", "reason": "transient seat errors after retries", "kind": "capacity"}],
        entries_with_verification=[(entry(), verified(verdict="UNVERIFIED", provider="", model="", unverified_reason="error: verifier seat failed"))],
        refuted=[verified(verdict="REFUTE", reasoning="coincidental")],
        severity_issues=[],
        dry_run=False,
        verification_status="unavailable — candidates proceed drafter-only",
    )
    assert "unverified (error: verifier seat failed)" in text
    assert "## Refuted candidates" in text
    assert "coincidental" in text
    assert "re-run recommended" in text  # capacity banner


def test_summary_zero_week_message_and_proposals_stub() -> None:
    text = render_summary(
        since="2026-08-03",
        until="2026-08-09",
        total_groups=0,
        qualified_groups=0,
        dedup_skipped=0,
        llm_skipped=[],
        entries_with_verification=[],
        refuted=[],
        severity_issues=[],
        dry_run=False,
        verification_status="available (vendor-b/verify-model)",
    )
    assert "Zero proposals this week is a successful outcome" in text
    assert "retro.proposals.enabled: false" in text


def test_summary_does_not_render_private_agent_prefixes_in_skipped_keys() -> None:
    text = render_summary(
        since="2026-08-03",
        until="2026-08-09",
        total_groups=1,
        qualified_groups=1,
        dedup_skipped=0,
        llm_skipped=[
            {
                "key": "claude::src/orders.py",
                "reason": "dropped: candidate cap (3) reached",
            }
        ],
        entries_with_verification=[],
        refuted=[],
        severity_issues=[],
        dry_run=False,
        verification_status="available (vendor-b/verify-model)",
    )
    assert "`src/orders.py`" in text
    assert "claude" not in text.lower()
