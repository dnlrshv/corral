"""Neutral evidence dataclasses for the weekly retrospective."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from corral.retro.verification import CandidateVerification


@dataclass(frozen=True)
class BridgeEvidence:
    """One sanitized file-backed retrospective evidence row."""

    source_ref: str
    incident_ref: str
    agent: str
    area: str
    summary: str
    text: str
    repo_paths: tuple[str, ...] = ()
    modified: str | None = None


@dataclass(frozen=True)
class FixupPairContext:
    """One (original agent PR, later fix-up PR touching shared files) pair."""

    original_pr: int
    original_author: str
    fixup_pr: int
    fixup_author: str
    days_between: float
    shared_files: tuple[str, ...]
    agent: str
    area: str
    original_title: str = ""
    fixup_title: str = ""


@dataclass(frozen=True)
class EvidenceGroup:
    """A cluster of evidence about distinct root incidents.

    Bridge rows are independent roots only in bridge-only groups. Once a group
    has canonical PR roots, bridge rows are corroboration: their prose/path
    metadata cannot prove that they describe a different real incident.
    """

    key: str
    agent: str
    area: str
    pairs: tuple[FixupPairContext, ...]
    extra_notes: tuple[str, ...] = ()
    note_source_prs: tuple[int, ...] = ()
    bridge_evidence: tuple[BridgeEvidence, ...] = ()

    @property
    def pr_numbers(self) -> list[int]:
        return sorted(
            {number for pair in self.pairs for number in (pair.original_pr, pair.fixup_pr)}
        )

    @property
    def root_incident_ids(self) -> set[int]:
        ids = {pair.original_pr for pair in self.pairs}
        ids.update(self._resolve_root_incident(pr) for pr in self.note_source_prs)
        return ids

    @property
    def root_incident_refs(self) -> set[str]:
        if self.root_incident_ids:
            return {f"pr:{pr}" for pr in self.root_incident_ids}
        return {record.incident_ref for record in self.bridge_evidence if record.incident_ref}

    @property
    def root_incident_labels(self) -> list[str]:
        return [
            f"#{ref.removeprefix('pr:')}" if ref.startswith("pr:") else ref
            for ref in sorted(self.root_incident_refs)
        ]

    def _resolve_root_incident(self, pr_number: int) -> int:
        for pair in self.pairs:
            if pr_number in (pair.original_pr, pair.fixup_pr):
                return pair.original_pr
        return pr_number

    @property
    def evidence_count(self) -> int:
        return len(self.root_incident_refs)

    @property
    def pair_number_tuples(self) -> set[tuple[int, int]]:
        return {(pair.original_pr, pair.fixup_pr) for pair in self.pairs}


@dataclass(frozen=True)
class VerifiedCandidate:
    """A drafted candidate plus its independent verification outcome.

    ``candidate`` carries the SHARPENED rule text when the verifier confirmed
    with a sharpened wording; ``original_rule`` preserves what the drafter
    actually drafted so the weekly summary can show both.
    """

    candidate: object
    original_rule: str
    verification: "CandidateVerification"


__all__ = ["BridgeEvidence", "EvidenceGroup", "FixupPairContext", "VerifiedCandidate"]
