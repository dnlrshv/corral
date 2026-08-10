"""Data sources for the instruction-staleness report.

* Telemetry sessions come from the weekly rollup parquets
  (``<telemetry.rollup_output_dir>/rollup_*.parquet``).
* Merged-PR path data comes through the C2 ``GitHubClient`` protocol
  (``corral.retro.github``) -- never a second GitHub boundary.
* Surface facts (exemption + surface-selector resolution) come from the
  corral surfaces registry via the ``corral.hooks.surface_check`` loader.

Sparse-telemetry honesty: when session counts fall below
``governance.staleness.min_sessions`` the report SAYS SO instead of emitting
verdicts (see ``corral.governance.staleness.report``).
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import timezone, date, datetime
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from corral.hooks.surface_check import Surface, load_surfaces
from corral.retro.github import GitHubClient

from . import model
from .model import AnalysisResult


class UnknownSurfaceError(ValueError):
    """A registry selector references a surface id absent from the registry."""


# ---------------------------------------------------------------------------
# Telemetry loading
# ---------------------------------------------------------------------------


def _iso_week_monday(path: Path) -> date:
    """Fallback date for rows without timestamps: Monday of the file's ISO week."""
    stem = path.stem  # rollup_2026-W29
    try:
        _, tag = stem.rsplit("_", 1)
        year_s, week_s = tag.split("-W")
        return date.fromisocalendar(int(year_s), int(week_s), 1)
    except (ValueError, IndexError):
        return datetime.now(timezone.utc).date()


def load_rollup_rows(telemetry_dir: Path) -> list[dict[str, Any]]:
    """Read every ``rollup_*.parquet`` row in *telemetry_dir* (sorted)."""
    rows: list[dict[str, Any]] = []
    for parquet in sorted(telemetry_dir.glob("rollup_*.parquet")):
        fallback = _iso_week_monday(parquet)
        for row in pq.read_table(parquet).to_pylist():
            row["_fallback_when"] = fallback
            rows.append(row)
    return rows


def build_sessions(
    rows: list[dict[str, Any]], paths_by_pr: dict[int, frozenset[str]]
) -> list[model.Session]:
    """One Session per unique session_id. ``touched_paths`` is None unless the
    session's merged PR resolved to a file set (honest coverage)."""
    sessions: list[model.Session] = []
    seen: set[str] = set()
    for row in rows:
        sid = str(row.get("session_id") or "")
        if not sid or sid in seen:
            continue
        seen.add(sid)
        when = model.parse_session_when(
            row, fallback=row.get("_fallback_when") or datetime.now(timezone.utc).date()
        )
        pr = row.get("pr_number")
        touched: frozenset[str] | None = None
        if pr is not None and int(pr) in paths_by_pr:
            touched = paths_by_pr[int(pr)]
        sessions.append(
            model.Session(
                session_id=sid,
                workflow_kind=model.normalize_workflow_kind(row.get("workflow_kind")),
                when=when,
                touched_paths=touched,
            )
        )
    return sessions


# ---------------------------------------------------------------------------
# Merged-PR path resolution behind the C2 GitHubClient protocol
# ---------------------------------------------------------------------------


def merged_pr_paths(
    github: GitHubClient, pr_numbers: Iterable[int], *, since: str, until: str
) -> dict[int, frozenset[str]]:
    """Resolve touched files for merged PRs via the ``GitHubClient`` protocol.

    One batched ``merged_prs`` query over the window returns each merged PR's
    file set, keyed by number. Sessions whose PR is unmerged or outside the
    window get no path data (honest coverage).
    """
    wanted = {int(n) for n in pr_numbers if n is not None}
    if not wanted:
        return {}
    out: dict[int, frozenset[str]] = {}
    for pr in github.merged_prs(since, until):
        try:
            num = int(pr.get("number"))
        except (TypeError, ValueError):
            continue
        if num not in wanted:
            continue
        files = frozenset(
            str(f["path"]) for f in (pr.get("files") or []) if isinstance(f, dict) and f.get("path")
        )
        out[num] = files
    return out


# ---------------------------------------------------------------------------
# Surfaces registry: resolver + exemption context (single source of truth)
# ---------------------------------------------------------------------------


def build_surface_resolver(
    surfaces: Iterable[Surface],
) -> tuple[dict[str, frozenset[str]], frozenset[str]]:
    """Return ``(surface_paths, known_ids)`` from the surfaces registry.

    ``surface_paths`` maps surface ID -> frozenset of its declared path
    prefixes; it is the ONE resolution used by both the exemption code and
    session evaluation (see the model module docstring for the source bug this
    fixes). ``known_ids`` supports unknown-ID validation (fail closed).
    """
    surface_paths: dict[str, frozenset[str]] = {}
    for surface in surfaces:
        surface_paths[surface.name] = frozenset(surface.paths)
    return surface_paths, frozenset(surface_paths)


def resolve_surface_paths(
    surfaces: Iterable[Surface], surface_ids: Iterable[str]
) -> dict[str, frozenset[str]]:
    """Resolve *surface_ids* to path sets; unknown ids raise (fail closed)."""
    surface_paths, known_ids = build_surface_resolver(surfaces)
    unknown = sorted(set(surface_ids) - known_ids)
    if unknown:
        raise UnknownSurfaceError(
            f"unknown surface id(s) {unknown}; declare them in the surfaces registry"
        )
    return {sid: surface_paths[sid] for sid in surface_ids}


def load_exemption_context(
    surfaces: Iterable[Surface],
    *,
    surface_paths: dict[str, frozenset[str]] | None = None,
) -> model.ExemptionContext:
    """Needs-human facts derived from the SAME resolved path sets used for sessions."""
    surface_list = list(surfaces)
    resolved = surface_paths
    if resolved is None:
        resolved, _ = build_surface_resolver(surface_list)
    ids = {surface.name for surface in surface_list if surface.needs_human}
    paths = {path for surface_id in ids for path in resolved[surface_id]}
    return model.ExemptionContext(
        needs_human_surface_ids=frozenset(ids),
        needs_human_surface_paths=tuple(sorted(paths)),
    )


def load_surfaces_registry(surfaces_path: Path) -> list[Surface]:
    """Load the surfaces registry; a missing registry is an empty registry."""
    if not surfaces_path.is_file():
        return []
    return load_surfaces(surfaces_path)


# ---------------------------------------------------------------------------
# Gotcha-registry executable controls
# ---------------------------------------------------------------------------


def load_gotcha_controls(gotchas_path: Path) -> dict[str, dict[str, Any]]:
    if not gotchas_path.is_file():
        return {}
    data = json.loads(gotchas_path.read_text(encoding="utf-8"))
    return {g["id"]: g for g in data.get("gotchas", []) if g.get("id")}


# ---------------------------------------------------------------------------
# Orchestration core
# ---------------------------------------------------------------------------


@dataclass
class StalenessRun:
    """One staleness analysis: verdicts + rendered report (nothing written)."""

    result: AnalysisResult
    report_markdown: str
    sessions: list[model.Session]


def run_staleness(
    *,
    as_of: date,
    config: object,
    github: GitHubClient,
    dry_run: bool = False,
) -> StalenessRun:
    """Read inputs, analyze, and render the report. Writes nothing, files nothing.

    Raises ``UnknownSurfaceError`` when a registry rule references a surface
    id missing from the surfaces registry (fail closed).
    """
    from corral.governance.config import GovernanceConfig
    from corral.governance.registry import parse_registry

    root: Path = getattr(config, "root")
    governance: GovernanceConfig = getattr(config, "governance")
    telemetry_cfg = getattr(config, "telemetry")
    retro_cfg = getattr(config, "retro")

    registry_text = (root / governance.registry).read_text(encoding="utf-8")
    rules = model.rules_from_registry(parse_registry(registry_text, governance))

    surfaces = load_surfaces_registry(root / _surfaces_relative(config))
    surface_paths, known_ids = build_surface_resolver(surfaces)
    unknown = model.validate_rule_surface_ids(rules, known_ids)
    if unknown:
        raise UnknownSurfaceError(
            f"registry selectors reference unknown surface id(s) {unknown}; "
            "declare them in the surfaces registry"
        )
    exemption_ctx = load_exemption_context(surfaces, surface_paths=surface_paths)
    gotcha_controls = load_gotcha_controls(root / retro_cfg.gotchas_path)

    telemetry_dir = root / telemetry_cfg.rollup_output_dir
    rows = load_rollup_rows(telemetry_dir)
    pr_numbers = {
        int(r["pr_number"]) for r in rows if r.get("pr_number") is not None
    }
    since_iso = date.fromordinal(as_of.toordinal() - governance.staleness.demote_days).isoformat()
    paths_by_pr = merged_pr_paths(
        github, pr_numbers, since=since_iso, until=as_of.isoformat()
    )
    sessions = build_sessions(rows, paths_by_pr)

    result = model.analyze(
        rules,
        sessions,
        as_of=as_of,
        cfg=governance.staleness,
        exemption_ctx=exemption_ctx,
        surface_paths=surface_paths,
        gotcha_controls=gotcha_controls,
    )

    from .report import render_report_markdown

    repo = retro_cfg.repository or "(repository not configured)"
    report_md = render_report_markdown(
        result, cfg=governance, repo=repo, dry_run=dry_run
    )
    return StalenessRun(result=result, report_markdown=report_md, sessions=sessions)


def _surfaces_relative(config: object) -> str:
    hooks = getattr(config, "hooks")
    return hooks.surfaces


__all__ = [
    "StalenessRun",
    "UnknownSurfaceError",
    "build_sessions",
    "build_surface_resolver",
    "load_exemption_context",
    "load_gotcha_controls",
    "load_rollup_rows",
    "load_surfaces_registry",
    "merged_pr_paths",
    "resolve_surface_paths",
    "run_staleness",
]
