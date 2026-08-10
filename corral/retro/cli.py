"""CLI for the weekly retrospective: seat diagnostics, ``run``, and
``revert-refinement``.

``corral retro run`` is the single writer of the gotcha registry. It mines
fix-up PR evidence, drafts candidates on the configured drafter seat, has
each one independently verified, and -- unless ``--dry-run`` -- writes the
schema-validated registry plus a Markdown weekly summary.
"""

from __future__ import annotations

import argparse
import dataclasses
import subprocess
import sys
from collections.abc import Callable, Sequence
from datetime import timezone, date, datetime, timedelta
from pathlib import Path
from typing import Any

from corral.config import load_config
from corral.retro import drafting, evidence, mining, registry, retry, summary
from corral.retro.github import GhCliGitHub, GitHubClient, GitHubError
from corral.retro.providers import runner_for_seat
from corral.retro.providers.base import SeatRunner, SeatStatus
from corral.retro.seats import SeatRegistry, SeatRegistryError
from corral.retro.types import EvidenceGroup, VerifiedCandidate
from corral.retro.verification import verify_candidate

RunnerFactory = Callable[..., SeatRunner]
RefResolver = Callable[[str], str]


def _print_table(rows: list[list[str]]) -> None:
    headers = ["SEAT", "REQUIRED", "PROVIDER", "MODEL", "ADAPTER", "STATUS", "DETAIL"]
    widths = [len(header) for header in headers]
    for row in rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))
    print("  ".join(header.ljust(widths[index]) for index, header in enumerate(headers)))
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(value.ljust(widths[index]) for index, value in enumerate(row)))


def run_seats_check(args: argparse.Namespace) -> int:
    """Probe all registry seats; require the drafter and first verifier."""
    try:
        config = load_config(args.config)
        registry = SeatRegistry.from_config(config)
    except (FileNotFoundError, OSError, SeatRegistryError, ValueError) as exc:
        print(f"corral retro seats check: {exc}", file=sys.stderr)
        return 1

    required = {config.retro.drafter_seat, config.retro.verifier_seats[0]}
    rows: list[list[str]] = []
    failed_required = False
    for name, seat in registry.items():
        try:
            probe = runner_for_seat(seat).probe(seat)
            status = SeatStatus(probe.status)
            detail = probe.detail
        except Exception as exc:
            status = SeatStatus.UNAVAILABLE
            detail = f"probe failed: {exc}"
        is_required = name in required
        if is_required and status is not SeatStatus.OK:
            failed_required = True
        rows.append(
            [
                name,
                "yes" if is_required else "no",
                seat.provider,
                seat.model,
                seat.adapter,
                status.value,
                detail,
            ]
        )

    missing = required.difference(registry)
    for name in sorted(missing):
        failed_required = True
        rows.append([name, "yes", "-", "-", "-", "unavailable", "seat not defined"])
    _print_table(rows)
    return 1 if failed_required else 0


# ---------------------------------------------------------------------------
# corral retro run
# ---------------------------------------------------------------------------


class BaseMovedError(RuntimeError):
    """The single-writer base ref moved between checkout and write."""


def iso_week_label(now: datetime | None = None) -> str:
    current = now or datetime.now(timezone.utc)
    year, week, _ = current.isocalendar()
    return f"{year}-W{week:02d}"


def week_window(week: str | None, *, now: datetime | None = None) -> tuple[str, str]:
    """Return (since, until) ISO dates spanning the ISO week (Mon..Sun)."""
    from corral.telemetry.rollup import parse_week

    if week is None:
        current = now or datetime.now(timezone.utc)
        year, week_number, _ = current.isocalendar()
    else:
        year, week_number = parse_week(week)
    monday = date.fromisocalendar(year, week_number, 1)
    return monday.isoformat(), (monday + timedelta(days=6)).isoformat()


def default_fixup_glob(config: object) -> str:
    telemetry = getattr(config, "telemetry")
    return f"{telemetry.rollup_output_dir}/fixup_*.parquet"


def find_fixup_parquet(root: Path, glob_pattern: str, week: str) -> Path | None:
    """Prefer the week-specific committed parquet, else the newest glob match."""
    matches = sorted(root.glob(glob_pattern))
    if not matches:
        return None
    week_specific = [path for path in matches if path.stem == f"fixup_{week}"]
    if week_specific:
        return week_specific[-1]
    return matches[-1]


def load_fixup_contexts(
    *,
    root: Path,
    github: GitHubClient,
    fixup_glob: str,
    week: str,
    since: str,
    until: str,
) -> list[Any]:
    """Prefer a committed fix-up parquet (a prior week's already-merged
    rollup); fall back to a live GitHub PR search for the requested window
    ("consume, if present" -- never a hard dependency on the rollup having
    merged yet)."""
    fixup_parquet = find_fixup_parquet(root, fixup_glob, week)
    if fixup_parquet is not None:
        import pyarrow.parquet as pq

        rows = pq.read_table(fixup_parquet).to_pylist()
        return mining.build_contexts(rows)
    prs = github.merged_prs(since, until)
    rows = mining_fixup_pairs(prs)
    by_number = {int(pr["number"]): pr for pr in prs}
    enriched = [
        {
            **row,
            "original_title": by_number.get(int(row["original_pr"]), {}).get("title", ""),
            "fixup_title": by_number.get(int(row["fixup_pr"]), {}).get("title", ""),
        }
        for row in rows
    ]
    return mining.build_contexts(enriched)


def mining_fixup_pairs(prs: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    from corral.retro.fixups import find_fixup_pairs

    return find_fixup_pairs(prs)


def dedup_groups(
    groups: list[EvidenceGroup],
    existing_gotchas: list[dict[str, Any]],
    open_issues: list[dict[str, Any]],
    *,
    min_root_incidents: int = 2,
) -> tuple[list[EvidenceGroup], list[str]]:
    existing_prs = registry.existing_source_prs(existing_gotchas)
    existing_refs = registry.existing_source_refs(existing_gotchas)
    open_pairs = registry.open_issue_pr_pairs(open_issues)
    open_refs = registry.open_issue_source_refs(open_issues)
    known_refs = existing_refs | open_refs
    survivors: list[EvidenceGroup] = []
    skipped_keys: list[str] = []
    for group in groups:
        group = registry.without_known_bridge_refs(group, known_refs)
        if registry.is_duplicate_group(
            group,
            existing_prs=existing_prs,
            open_pairs=open_pairs,
        ) or not mining.qualifying_groups([group], min_root_incidents):
            skipped_keys.append(group.key)
            continue
        survivors.append(group)
    return survivors, skipped_keys


def _drafter_complete(
    seat_registry: SeatRegistry,
    config: object,
    *,
    runner_factory: RunnerFactory | None,
) -> Callable[[str], str]:
    """Build the drafter callable: SeatRunner.complete wrapped in bounded retry."""
    retro = getattr(config, "retro")
    seat = seat_registry.require(retro.drafter_seat)
    runner = (runner_factory or runner_for_seat)(seat)

    def complete(prompt: str) -> str:
        return retry.call_with_retry(
            lambda p: runner.complete(
                seat, p, timeout=retro.drafting_timeout_s, max_tokens=retro.max_tokens
            ),
            prompt,
            context="gotcha candidate drafting",
        )

    return complete


def draft_and_verify_candidates(
    groups: list[EvidenceGroup],
    *,
    github: GitHubClient,
    seat_registry: SeatRegistry,
    config: object,
    runner_factory: RunnerFactory | None = None,
    created_on: date | None = None,
) -> tuple[list[VerifiedCandidate], list[dict[str, str]]]:
    """Draft candidates on the configured drafter seat and independently verify
    each one before it is allowed into the gotcha registry.

    Returns (verified, skipped). ``verified`` holds every candidate that
    survived drafting and the confidence threshold, each tagged with its
    verdict (CONFIRM / REFUTE / UNVERIFIED) -- the caller decides what to do
    with REFUTEd ones (excluded from the registry, still shown in the weekly
    summary). ``skipped`` records groups dropped by the candidate cap,
    extraction failures, or sub-threshold confidence; those never reach
    verification.

    ``groups`` is expected pre-sorted most-evidenced-first, so
    ``groups[:max_candidates]`` is the highest-evidence slice -- the cap
    applies pre-verification: a group refuted by the verifier does not free a
    slot for the next-most-evidenced group.
    """
    from corral.retro.bridge.readers import render_bridge_evidence

    retro = getattr(config, "retro")
    max_candidates = retro.evidence.max_candidates
    skipped: list[dict[str, str]] = []

    capped_groups = groups[:max_candidates]
    for group in groups[max_candidates:]:
        skipped.append(
            {"key": group.key, "reason": f"dropped: candidate cap ({max_candidates}) reached"}
        )

    complete = _drafter_complete(seat_registry, config, runner_factory=runner_factory)

    verified: list[VerifiedCandidate] = []
    for group_index, group in enumerate(capped_groups):
        excerpts = evidence.fetch_pr_excerpts(github, group.pr_numbers)
        try:
            candidate = drafting.extract_candidate_with_retry(
                group,
                excerpts,
                complete,
                created_on=created_on,
                allowed_severities=retro.allowed_severities,
                severe_severities=retro.severe_severities,
            )
        except retry.RetriableLLMError as exc:
            skipped.append(
                {
                    "key": group.key,
                    "reason": f"transient seat errors after retries: {exc}",
                    "kind": "capacity",
                }
            )
            skipped.extend(
                {
                    "key": deferred.key,
                    "reason": (
                        "not attempted after an earlier group exhausted retries "
                        "(pass-level capacity circuit open)"
                    ),
                    "kind": "capacity_deferred",
                }
                for deferred in capped_groups[group_index + 1 :]
            )
            break
        except ValueError as exc:  # returned-output validation is group-local
            skipped.append({"key": group.key, "reason": f"extraction failed: {exc}"})
            continue
        if candidate.confidence < retro.confidence_threshold:
            skipped.append(
                {
                    "key": group.key,
                    "reason": (
                        f"confidence {candidate.confidence:.2f} below threshold "
                        f"{retro.confidence_threshold:.2f}"
                    ),
                }
            )
            continue

        verdict = verify_candidate(
            candidate,
            excerpts,
            bridge_evidence=(
                render_bridge_evidence(group.bridge_evidence) if group.bridge_evidence else ""
            ),
            registry=seat_registry,
            config=config,
            runner_factory=runner_factory,
        )
        effective_candidate = candidate
        if verdict.verdict == "CONFIRM" and verdict.sharpened_rule:
            effective_candidate = dataclasses.replace(candidate, rule=verdict.sharpened_rule)
        verified.append(
            VerifiedCandidate(
                candidate=effective_candidate,
                original_rule=candidate.rule,
                verification=verdict,
            )
        )
    return verified, skipped


def probe_verifier_status(
    seat_registry: SeatRegistry,
    verifier_seats: Sequence[str],
    *,
    runner_factory: RunnerFactory | None = None,
) -> str:
    """Run-start verifier availability line for the weekly summary."""
    factory = runner_factory or runner_for_seat
    for name in verifier_seats:
        seat = seat_registry.get(name)
        if seat is None:
            continue
        try:
            probe = factory(seat).probe(seat)
        except Exception as exc:
            return (
                f"unavailable — verifier probe failed ({exc}); candidates proceed "
                'drafter-only, each marked "unverified"'
            )
        if probe.available:
            return f"available ({probe.provider}/{probe.model})"
        return (
            f"unavailable — verifier probe: {probe.detail or 'not available'}; "
            'candidates proceed drafter-only, each marked "unverified"'
        )
    return (
        "unavailable — no verifier seats configured; candidates proceed "
        'drafter-only, each marked "unverified"'
    )


def default_resolve_ref(ref: str) -> str:
    """Resolve *ref* to a commit id via git (single-writer freshness check)."""
    result = subprocess.run(
        ["git", "rev-parse", ref], capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def check_base_fresh(
    resolve_ref: RefResolver, base_ref: str, expected: str
) -> None:
    """Fail closed when the base ref moved since the expected id was captured."""
    actual = resolve_ref(base_ref)
    if actual != expected:
        raise BaseMovedError(
            f"base ref {base_ref!r} moved: expected {expected}, found {actual}; "
            "refusing to write the gotcha registry (re-run from a fresh checkout)"
        )


def _root_relative(root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else root / path


def run_retro(
    args: argparse.Namespace,
    *,
    github: GitHubClient | None = None,
    runner_factory: RunnerFactory | None = None,
    resolve_ref: RefResolver | None = None,
) -> int:
    """Execute one weekly retrospective pass. Returns a process exit code."""
    from corral.retro.bridge import discovery, readers

    config = load_config(args.config)
    root = config.root
    retro = config.retro

    if not retro.repository:
        print(
            "corral retro run: retro.repository must be configured (owner/name)",
            file=sys.stderr,
        )
        return 1

    base_ref = getattr(args, "base_ref", None)
    expected_base = getattr(args, "expected_base", None)
    if bool(base_ref) != bool(expected_base):
        print(
            "corral retro run: --base-ref and --expected-base must be provided together",
            file=sys.stderr,
        )
        return 1

    try:
        seat_registry = SeatRegistry.from_config(config)
    except (FileNotFoundError, OSError, SeatRegistryError) as exc:
        print(f"corral retro run: {exc}", file=sys.stderr)
        return 1

    factory = runner_factory or runner_for_seat
    try:
        drafter_seat = seat_registry.require(retro.drafter_seat)
    except SeatRegistryError as exc:
        print(f"corral retro run: {exc}", file=sys.stderr)
        return 1
    try:
        drafter_probe = factory(drafter_seat).probe(drafter_seat)
    except Exception as exc:
        print(f"corral retro run: drafter probe failed: {exc}", file=sys.stderr)
        return 1
    if not drafter_probe.available:
        print(
            f"corral retro run: drafter seat {retro.drafter_seat!r} unavailable "
            f"({drafter_probe.detail or 'probe failed'}); cannot draft candidates",
            file=sys.stderr,
        )
        return 1

    now = datetime.now(timezone.utc)
    week = args.week or iso_week_label(now)
    default_since, default_until = week_window(week, now=now)
    since = args.since or default_since
    until = args.until or default_until

    telemetry_dir = _root_relative(root, config.telemetry.rollup_output_dir)
    gotchas_path = _root_relative(root, retro.gotchas_path)
    if getattr(args, "output_summary", None) is not None:
        output_summary = Path(args.output_summary)
        if not output_summary.is_absolute():
            output_summary = root / output_summary
    else:
        output_summary = telemetry_dir / f"retrospective_{week}.md"
    if retro.proposals.enabled and not args.dry_run:
        unsafe = _proposal_artifact_destination(
            root,
            [gotchas_path, output_summary],
            target_globs=retro.proposals.target_globs,
            instruction_globs=config.governance.instruction_globs,
            protected_paths=config.governance.protected_paths,
            registry_path=config.governance.registry,
        )
        if unsafe:
            print(
                "corral retro run: refusing to write a normal run artifact to "
                f"proposal/instruction target {unsafe!r}",
                file=sys.stderr,
            )
            return 1

    client = github or GhCliGitHub(
        retro.repository, timeout_s=retro.github.timeout_s
    )

    contexts = load_fixup_contexts(
        root=root,
        github=client,
        fixup_glob=retro.fixup_glob or default_fixup_glob(config),
        week=week,
        since=since,
        until=until,
    )
    notes_by_pr = evidence.load_session_learning_notes_by_pr(telemetry_dir)
    bridge_records = readers.load_bridge_evidence(
        memory_roots=discovery.resolve_roots(
            [_root_relative(root, entry) for entry in retro.bridge.memory_roots]
        ),
        run_artifact_roots=discovery.resolve_roots(
            [_root_relative(root, entry) for entry in retro.bridge.run_artifact_roots]
        ),
    )
    groups = mining.group_evidence(
        contexts,
        notes_by_pr,
        bridge_evidence=bridge_records,
        ignored_title_patterns=retro.evidence.ignored_title_patterns,
        ignored_path_globs=retro.evidence.ignored_path_globs,
    )
    qualified = mining.qualifying_groups(groups, retro.evidence.min_root_incidents)

    gotchas_payload = registry.load_gotchas_file(gotchas_path)
    existing_gotchas = gotchas_payload["gotchas"]
    open_issues = evidence.fetch_open_gotcha_issues(
        client, label=retro.github.gotcha_label
    )
    survivors, dedup_skipped = dedup_groups(
        qualified,
        existing_gotchas,
        open_issues,
        min_root_incidents=retro.evidence.min_root_incidents,
    )

    verified, llm_skipped = draft_and_verify_candidates(
        survivors,
        github=client,
        seat_registry=seat_registry,
        config=config,
        runner_factory=runner_factory,
        created_on=now.date(),
    )

    # Only an explicit REFUTE excludes a candidate from the registry.
    # UNVERIFIED (verifier unavailable/erroring/unparseable) proceeds
    # drafter-only -- this is the graceful-degradation path.
    gotcha_ready = [v for v in verified if v.verification.verdict != "REFUTE"]
    refuted = [v for v in verified if v.verification.verdict == "REFUTE"]

    year = str(now.year)
    new_entries = registry.assign_sequential_entries(
        [v.candidate for v in gotcha_ready], existing_gotchas, year
    )
    entries_with_verification = list(zip(new_entries, gotcha_ready, strict=True))

    if new_entries:
        if base_ref and expected_base:
            try:
                check_base_fresh(resolve_ref or default_resolve_ref, base_ref, expected_base)
            except (BaseMovedError, subprocess.CalledProcessError, FileNotFoundError) as exc:
                print(f"corral retro run: {exc}", file=sys.stderr)
                return 1

    severity_issues = _file_severity_issues(
        entries_with_verification,
        github=client,
        config=config,
        dry_run=args.dry_run,
    )

    proposal_run = None
    if retro.proposals.enabled:
        from corral.retro.proposals import draft_and_verify_proposals

        try:
            proposal_run = draft_and_verify_proposals(
                survivors,
                github=client,
                seat_registry=seat_registry,
                config=config,
                runner_factory=runner_factory,
            )
        except Exception as exc:  # a pass failure must not lose the gotcha output
            from corral.retro.proposals.models import DocProposalRun

            proposal_run = DocProposalRun(pass_failure=f"{type(exc).__name__}: {exc}")

    verification_status = probe_verifier_status(
        seat_registry, retro.verifier_seats, runner_factory=runner_factory
    )
    summary_text = summary.render_summary(
        since=since,
        until=until,
        total_groups=len(groups),
        qualified_groups=len(qualified),
        dedup_skipped=len(dedup_skipped),
        llm_skipped=llm_skipped,
        entries_with_verification=entries_with_verification,
        refuted=refuted,
        severity_issues=severity_issues,
        dry_run=args.dry_run,
        verification_status=verification_status,
        proposals_enabled=retro.proposals.enabled,
        proposal_run=proposal_run,
    )

    print(summary_text)

    if args.dry_run:
        print(
            f"--dry-run: not writing {gotchas_path} or filing issues "
            f"({len(new_entries)} gotcha candidate(s) would be applied).",
            file=sys.stderr,
        )
        return 0

    output_summary.parent.mkdir(parents=True, exist_ok=True)
    output_summary.write_text(summary_text, encoding="utf-8")

    if new_entries:
        gotchas_payload["gotchas"] = existing_gotchas + new_entries
        registry.write_gotchas_file(gotchas_path, gotchas_payload)
        print(f"Wrote {len(new_entries)} new gotcha(s) -> {gotchas_path}")
    else:
        print("No new gotchas to write this week.")

    if retro.proposals.enabled and proposal_run is not None:
        accepted = len(proposal_run.accepted)
        print(
            f"Doc/skill proposal pass: {accepted} verified proposal(s) rendered "
            "human-review-only in the summary (never auto-applied)."
        )
    return 0


def _proposal_artifact_destination(
    root: Path,
    paths: list[Path],
    *,
    target_globs: list[str],
    instruction_globs: list[str],
    protected_paths: list[str],
    registry_path: str,
) -> str | None:
    """Return the first normal artifact path that aliases an instruction target."""
    from corral.governance.proposals import path_matches

    root_resolved = root.resolve()
    root_lexical = root.absolute()
    patterns = [*target_globs, *instruction_globs, *protected_paths]
    for path in paths:
        candidate = path if path.is_absolute() else root / path
        relatives: set[str] = set()
        for normalized, normalized_root in (
            (candidate.absolute(), root_lexical),
            (candidate.resolve(), root_resolved),
        ):
            try:
                relatives.add(normalized.relative_to(normalized_root).as_posix())
            except ValueError:
                pass
        for relative in relatives:
            if relative == registry_path or any(
                path_matches(relative, pattern) for pattern in patterns
            ):
                return relative
    return None


def _file_severity_issues(
    entries_with_verification: list[tuple[dict[str, Any], VerifiedCandidate]],
    *,
    github: GitHubClient,
    config: object,
    dry_run: bool,
) -> list[tuple[Any, str, str | None]]:
    """Escalate configured-severe candidates per retro.issue_sink.

    External issue creation happens only after every batch-fatal model call,
    so a later failure cannot leave an issue claiming a gotcha was written
    when the batch aborted before writing.
    """
    retro = getattr(config, "retro")
    severe = {str(item).upper() for item in retro.severe_severities}
    if not severe or retro.issue_sink == "off":
        return []
    issues: list[tuple[Any, str, str | None]] = []
    for entry, verified in entries_with_verification:
        candidate = verified.candidate
        if str(candidate.severity).upper() not in severe:
            continue
        title = registry.build_severity_issue_title(
            candidate, entry["id"], label=retro.github.gotcha_label
        )
        body = registry.build_severity_issue_body(candidate, entry["id"])
        issue_url: str | None = None
        if dry_run:
            pass
        elif retro.issue_sink == "github":
            try:
                issue_url = github.create_issue(
                    title,
                    body,
                    labels=[retro.github.gotcha_label],
                    assignee=retro.github.assignee,
                )
            except GitHubError as exc:
                print(f"warning: failed to file severity issue: {exc}", file=sys.stderr)
        else:  # stdout sink: render, never file
            print(f"[retro.issue_sink: stdout] would file severity issue:\n{title}\n")
        issues.append((candidate, entry["id"], issue_url))
    return issues


# ---------------------------------------------------------------------------
# corral retro revert-refinement
# ---------------------------------------------------------------------------


def run_revert_refinement(args: argparse.Namespace) -> int:
    """Render (never apply) the reverse patch for one ledger record."""
    from corral.retro.revert import render_revert_patch

    config = load_config(args.config)
    ledger_path = (
        Path(args.ledger)
        if getattr(args, "ledger", None) is not None
        else config.root / config.retro.refinements_path
    )
    try:
        patch = render_revert_patch(ledger_path, args.refinement_id)
    except (ValueError, FileNotFoundError) as exc:
        print(f"corral retro revert-refinement: {exc}", file=sys.stderr)
        return 1
    if getattr(args, "output", None) is not None:
        Path(args.output).write_text(patch, encoding="utf-8")
    else:
        print(patch, end="")
    return 0


__all__ = [
    "BaseMovedError",
    "check_base_fresh",
    "dedup_groups",
    "default_fixup_glob",
    "draft_and_verify_candidates",
    "find_fixup_parquet",
    "iso_week_label",
    "load_fixup_contexts",
    "probe_verifier_status",
    "run_retro",
    "run_revert_refinement",
    "run_seats_check",
    "week_window",
]
