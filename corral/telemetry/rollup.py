"""Roll up weekly agent session telemetry artifacts into a parquet.

Downloads all ``agent-telemetry-*`` artifacts uploaded in the last *days* days,
parses the JSON session files, enriches each row with GitHub PR metadata
(merged status, cycle time, changed LOC) and reconstructed CI outcomes, and
writes a parquet to ``<output_dir>/rollup_<YYYY-Www>.parquet``.

Usage::

    corral telemetry rollup

    # or with overrides:
    corral telemetry rollup --days 7 --week 2026-18

Graceful degradation: the source repository's enrichment and artifact-quality
modules are not part of corral, so the ``area`` and ``issue_type`` columns are
defaulted to ``"unknown"`` and ``changed_loc_quartile`` stays ``"unknown"``;
no quality report is written. ``changed_loc`` is still derived from the PR
files endpoint (additions + deletions) when ``gh`` is available.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import subprocess
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from corral.telemetry.ci_outcome import REQUIRED_CI_CONTEXTS, fetch_ci_outcome_for_pr
from corral.telemetry.rollup_schema import (
    ROW_DEFAULTS,
    SCHEMA,
    coerce_int_or_none,
    decision_fields,
)

UTC = timezone.utc

DEFAULT_OUTPUT_DIR = "agent_telemetry"
DEFAULT_LOOKBACK_DAYS = 7

_WEEK_RE = re.compile(r"^(?P<year>\d{4})-?W?(?P<week>[0-4]\d|5[0-3])$")


def parse_week(week: str) -> tuple[int, int]:
    """Parse ``YYYY-WW`` (also accepting ``YYYY-Www`` / ``YYYYWW``) into (year, week)."""
    match = _WEEK_RE.match(week.strip())
    if match is None:
        raise ValueError(f"week must look like YYYY-WW, got {week!r}")
    year = int(match.group("year"))
    week_number = int(match.group("week"))
    if not 1 <= week_number <= 53:
        raise ValueError(f"week must be between 01 and 53, got {week!r}")
    return year, week_number


def default_output_path(
    output_dir: Path | str = DEFAULT_OUTPUT_DIR,
    now: datetime | None = None,
    week: str | None = None,
) -> Path:
    """Weekly rollup parquet path, named from the ISO week.

    *week* (``YYYY-WW``) overrides the ISO week of *now* (default: current).
    """
    if week is not None:
        year, week_number = parse_week(week)
    else:
        current = now or datetime.now(UTC)
        year, week_number, _ = current.isocalendar()
    return Path(output_dir) / f"rollup_{year}-W{week_number:02d}.parquet"


def _gh_json(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
        check=True,
    )


def _gh_binary(args: list[str]) -> bytes:
    result = subprocess.run(
        ["gh", *args],
        capture_output=True,
        check=True,
    )
    return result.stdout


def list_recent_artifacts(repo: str, days: int) -> list[dict[str, Any]]:
    """Return agent-telemetry artifacts created within the last *days* days."""
    cutoff = datetime.now(UTC) - timedelta(days=days)
    result = _gh_json(
        ["api", f"/repos/{repo}/actions/artifacts", "--paginate", "--jq", ".artifacts[]"]
    )
    artifacts: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            artifact: dict[str, Any] = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not str(artifact.get("name", "")).startswith("agent-telemetry-"):
            continue
        created_str = artifact.get("created_at", "")
        if not created_str:
            continue
        created_at = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
        if created_at >= cutoff:
            artifacts.append(artifact)
    return artifacts


def download_artifact_zip(artifact_id: int, repo: str) -> bytes:
    """Download artifact zip and return raw bytes."""
    return _gh_binary(["api", f"/repos/{repo}/actions/artifacts/{artifact_id}/zip"])


def extract_json_sessions(zip_bytes: bytes) -> list[dict[str, Any]]:
    """Extract and parse all JSON session files from a zip archive.

    Malformed members and non-object payloads are skipped silently: a corrupt
    session file must never abort the weekly rollup.
    """
    sessions: list[dict[str, Any]] = []
    try:
        archive = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        return sessions
    with archive:
        for info in archive.infolist():
            if info.is_dir() or not info.filename.endswith(".json"):
                continue
            try:
                parsed = json.loads(archive.read(info.filename).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(parsed, dict):
                sessions.append(parsed)
    return sessions


def fetch_pr_info(
    pr_number: int, repo: str, required_contexts: tuple[str, ...] = REQUIRED_CI_CONTEXTS
) -> dict[str, Any]:
    """Fetch PR metadata: merged status, timestamps, changed LOC, CI outcome."""
    try:
        pr = _gh_get_json(
            f"/repos/{repo}/pulls/{pr_number}",
            "{merged: .merged, merged_at: .merged_at, created_at: .created_at}",
        )
    except (subprocess.CalledProcessError, json.JSONDecodeError, OSError):
        return {}
    if not isinstance(pr, dict):
        return {}

    try:
        # Paginate: PRs touching > 30 files would otherwise have a truncated
        # changed-LOC derivation.
        files_result = _gh_json(
            [
                "api",
                "--paginate",
                f"/repos/{repo}/pulls/{pr_number}/files",
                "--jq",
                ".[] | {filename: .filename, additions: .additions, deletions: .deletions}",
            ]
        )
    except (subprocess.CalledProcessError, OSError):
        pr["changed_loc"] = None
    else:
        additions = 0
        deletions = 0
        saw_file = False
        for line in files_result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            file_additions = coerce_int_or_none(entry.get("additions"))
            file_deletions = coerce_int_or_none(entry.get("deletions"))
            if file_additions is None and file_deletions is None:
                continue
            saw_file = True
            additions += file_additions or 0
            deletions += file_deletions or 0
        pr["additions"] = additions if saw_file else None
        pr["deletions"] = deletions if saw_file else None
        pr["changed_loc"] = additions + deletions if saw_file else None

    # Reconstructed from the PR's full commit history, not just its final state
    # -- works identically for a PR merged years ago or moments ago; see
    # corral/telemetry/ci_outcome.py for the reconstruction and its known gaps.
    pr.update(fetch_ci_outcome_for_pr(pr_number, repo, required_contexts))
    return pr


def _gh_get_json(endpoint: str, jq_filter: str) -> Any:
    result = _gh_json(["api", endpoint, "--jq", jq_filter])
    return json.loads(result.stdout.strip())


def normalize_row(
    session: dict[str, Any], pr_info: dict[str, Any], artifact: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Build a rollup row from a session dict and PR metadata.

    ``area`` and ``issue_type`` default to ``"unknown"``: the source
    repository's enrichment module (path/label heuristics) is not part of
    corral.
    """
    artifact = artifact or {}
    merged = bool(pr_info.get("merged"))
    cycle_time: float | None = None
    if merged and pr_info.get("merged_at") and pr_info.get("created_at"):
        merged_at = datetime.fromisoformat(pr_info["merged_at"].replace("Z", "+00:00"))
        created_at = datetime.fromisoformat(pr_info["created_at"].replace("Z", "+00:00"))
        cycle_time = (merged_at - created_at).total_seconds() / 60.0

    raw_pr = session.get("pr_number")
    pr_number: int | None
    if isinstance(raw_pr, int):
        pr_number = raw_pr
    elif isinstance(raw_pr, str) and raw_pr.isdigit():
        pr_number = int(raw_pr)
    else:
        pr_number = None

    return {
        "session_id": str(session.get("session_id") or ""),
        "agent": str(session.get("agent") or "unknown"),
        "model": str(session.get("model") or ""),
        "reported_model": (
            str(session["reported_model"]) if session.get("reported_model") else None
        ),
        "area": ROW_DEFAULTS["area"],
        "issue_type": ROW_DEFAULTS["issue_type"],
        "arm": str(session.get("arm") or "unknown"),
        "pr_number": pr_number,
        "merged": merged,
        "cycle_time_minutes": cycle_time,
        **decision_fields(session, pr_info, artifact),
    }


def build_table(rows: list[dict[str, Any]]) -> pa.Table:
    if not rows:
        return pa.table(
            {col: pa.array([], type=SCHEMA.field(col).type) for col in SCHEMA.names},
            schema=SCHEMA,
        )
    return pa.table(
        {
            field.name: pa.array(
                [
                    row[field.name] if field.name in row else ROW_DEFAULTS.get(field.name)
                    for row in rows
                ],
                type=field.type,
            )
            for field in SCHEMA
        },
        schema=SCHEMA,
    )


def rollup(
    repo: str, days: int = DEFAULT_LOOKBACK_DAYS, required_contexts: tuple[str, ...] = REQUIRED_CI_CONTEXTS
) -> list[dict[str, Any]]:
    """Download artifacts and return normalized session rows."""
    print(f"Listing artifacts for {repo} (last {days} days)...")
    artifacts = list_recent_artifacts(repo, days)
    print(f"Found {len(artifacts)} matching artifacts.")

    rows: list[dict[str, Any]] = []
    seen_session_ids: set[str] = set()
    pr_cache: dict[int, dict[str, Any]] = {}
    for artifact in artifacts:
        artifact_id: int = artifact["id"]
        print(f"  Downloading artifact {artifact_id} ({artifact['name']})...")
        try:
            zip_bytes = download_artifact_zip(artifact_id, repo)
        except subprocess.CalledProcessError as exc:
            print(f"  WARNING: failed to download artifact {artifact_id}: {exc}")
            continue

        for session in extract_json_sessions(zip_bytes):
            session_id = str(session.get("session_id") or "")
            if session_id and session_id in seen_session_ids:
                continue
            if session_id:
                seen_session_ids.add(session_id)

            raw_pr = session.get("pr_number")
            pr_info: dict[str, Any] = {}
            if raw_pr:
                try:
                    pr_num = int(raw_pr)
                except (ValueError, TypeError):
                    pr_num = None
                if pr_num is not None:
                    if pr_num in pr_cache:
                        pr_info = pr_cache[pr_num]
                    else:
                        pr_info = fetch_pr_info(pr_num, repo, required_contexts)
                        pr_cache[pr_num] = pr_info
            rows.append(normalize_row(session, pr_info, artifact))

    return rows


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Roll up weekly agent telemetry artifacts.")
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help=f"Artifact lookback window in days (default: {DEFAULT_LOOKBACK_DAYS})",
    )
    parser.add_argument(
        "--week",
        default=None,
        help="ISO week label YYYY-WW for the output file (default: current week)",
    )
    parser.add_argument(
        "--output", type=Path, help="Output parquet path (default: auto-named from ISO week)"
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help=f"Directory receiving the rollup parquet (default: {DEFAULT_OUTPUT_DIR}/)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to corral.yaml (default: nearest corral.yaml above cwd)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    from corral.config import load_config

    cfg = load_config(args.config)
    days = args.days if args.days is not None else cfg.telemetry.lookback_days
    output_dir = (
        args.output_dir
        if args.output_dir is not None
        else cfg.root / cfg.telemetry.rollup_output_dir
    )
    required_contexts = tuple(cfg.telemetry.required_ci_contexts) or REQUIRED_CI_CONTEXTS

    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not repo:
        print("GITHUB_REPOSITORY environment variable is required", flush=True)
        return 1

    if args.week is not None:
        try:
            parse_week(args.week)
        except ValueError as exc:
            print(str(exc), flush=True)
            return 1

    try:
        rows = rollup(repo, days, required_contexts)
    except FileNotFoundError:
        print("gh CLI not found on PATH; cannot list telemetry artifacts", flush=True)
        return 1
    print(f"Normalized {len(rows)} session rows.")

    output_path = args.output or default_output_path(output_dir, week=args.week)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    table = build_table(rows)
    pq.write_table(table, output_path)
    print(f"Wrote rollup → {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
