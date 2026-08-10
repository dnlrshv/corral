"""Deterministic applicability model for the instruction-staleness report.

Design (binding):

  Applicability is **DETERMINISTIC**. For every rule in the instruction
  registry we compute what fraction of recent agent sessions the rule *would
  have applied to*, derived from each session's ``workflow_kind`` + touched
  paths/surfaces (telemetry rollups + merged-PR diffs) intersected with the
  rule's selectors. We NEVER use self-reported citations -- an agent saying
  "I read the conventions doc" is not evidence a rule was relevant.

Thresholds are configuration (``governance.staleness.*``; defaults preserve
the source governance consult values):

  * retain-in-core   -- >= ``retain_rate`` of sessions across
                        >= ``retain_workflow_count`` workflow kinds over
                        ``retain_days``.
  * flag-for-demotion -- < ``demote_rate`` over ``demote_days`` (the longer
                        window) AND not retained AND at least ``min_sessions``
                        evaluable sessions behind the number.

Hysteresis is the two-threshold band (a Schmitt trigger): a rule between the
demotion floor and the retain ceiling sits in the neutral ``MONITOR`` band and
is never actioned, and a rule that clears the *recent* retain bar can never be
demoted on the *long-window* rate even if the two windows disagree
(recent-activity-protects-against-demotion).

Data honesty: a session lacking touched-path data counts in a rule's
denominator ONLY for workflow-scoped rules; a path/surface-only rule cannot be
evaluated against a path-less session, so that session is excluded from its
denominator. The global coverage fraction (sessions with path data / all
sessions) is reported explicitly so a thin-data window cannot masquerade as
"everything is stale". A first run is expected to be mostly
``INSUFFICIENT_DATA`` -- a valid, reportable outcome (zero demotions is
success; the job is never scored by demotions produced).

SURFACES RESOLVER (do not regress):
  ``selectors.surfaces`` values are surface IDs, not file paths. corral
  resolves surface IDs to path sets ONCE through the surfaces registry (the
  ``corral.hooks.surface_check`` loader) and uses the resolved mapping
  consistently in BOTH directions -- exemption classification and session
  matching -- so a surface selector matches a session by resolved path
  membership, never by comparing an ID string against a raw path:

  * exemption: a rule whose surface selectors reference a ``needs_human``
    surface ID is exempt (ID intersection against the registry's needs_human
    set);
  * session match: a session's touched paths are matched against the UNION of
    the resolved path prefixes of the rule's surface selectors.

  Unknown surface IDs are a validation error (fail closed) rather than a
  silent non-match.

PORTABILITY DEVIATION (intentional):
  The source also exempted a hard-coded set of project-specific money paths
  and safety/authorization concern-key names. Those two exemption classes are
  private taxonomy, so corral does not silently transplant or generalize them.
  Their portable replacement is explicit ``needs_human`` surface membership:
  adopters must put equivalent high-risk paths in that registry. The source's
  ``needs_human`` exemption class is retained and narrowed to that public,
  repository-owned taxonomy; no other source exemption class was dropped.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from ..config import StalenessConfig

# ---------------------------------------------------------------------------
# Verdict vocabulary
# ---------------------------------------------------------------------------

RETAIN = "RETAIN"
DEMOTE = "DEMOTE"
MONITOR = "MONITOR"
INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
EXEMPT = "EXEMPT"

#: gotcha-registry control types that are *executable* enforcement (an
#: executable control owns the rule, so the prose can be replaced by a
#: pointer). ``prompt_only`` is intentionally excluded -- prose is the only
#: enforcement there.
EXECUTABLE_CONTROL_TYPES: frozenset[str] = frozenset(
    {"regression_test", "lint", "schema_validator", "hook"}
)

# Canonical normalized workflow-kind vocabulary (matches the telemetry rollup
# column). Raw strings that are not canonical fall through a keyword
# heuristic; an unrecognizable value becomes ``unknown`` -- never a guess.
CANONICAL_WORKFLOW_KINDS: frozenset[str] = frozenset(
    {"fix-issue", "pr-review", "pr-merge", "pr-triage", "pm"}
)
UNKNOWN_WORKFLOW_KIND = "unknown"


def normalize_workflow_kind(raw: Any) -> str:
    """Map a raw rollup ``workflow_kind`` string to the canonical vocabulary.

    Deterministic. Already-canonical values pass through; everything else
    falls back to a keyword heuristic (merge/review/triage/fix), and an
    unrecognizable value becomes ``unknown``.
    """
    if raw is None:
        return UNKNOWN_WORKFLOW_KIND
    text = str(raw).strip()
    if not text:
        return UNKNOWN_WORKFLOW_KIND
    lowered = text.lower()
    if lowered in CANONICAL_WORKFLOW_KINDS:
        return lowered
    compact = lowered.replace(":", " ").replace("_", "-").replace(" ", "-")
    if "project-management" in compact:
        return "pm"
    if "merge" in compact:
        return "pr-merge"
    if "review" in compact:
        return "pr-review"
    if "triage" in compact:
        return "pr-triage"
    if "fix" in compact:
        return "fix-issue"
    return UNKNOWN_WORKFLOW_KIND


# ---------------------------------------------------------------------------
# Session + rule models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Session:
    """One agent session, as consumed from the telemetry rollups.

    ``touched_paths`` is ``None`` when no path data is available for the
    session (unmerged PR, or ``pr_number`` not resolvable to a merged-PR
    diff). It is a (possibly empty) frozenset when path data was resolved.
    The distinction is load-bearing for the honest-denominator rule.
    """

    session_id: str
    workflow_kind: str  # normalized
    when: date
    touched_paths: frozenset[str] | None = None

    @property
    def has_path_data(self) -> bool:
        return self.touched_paths is not None


@dataclass(frozen=True)
class Selectors:
    paths: tuple[str, ...] = ()
    workflows: tuple[str, ...] = ()
    surfaces: tuple[str, ...] = ()  # surface IDs; resolve via the surfaces registry

    @property
    def is_universal(self) -> bool:
        return not (self.paths or self.workflows or self.surfaces)

    @property
    def has_path_axis(self) -> bool:
        return bool(self.paths or self.surfaces)

    @property
    def has_workflow_axis(self) -> bool:
        return bool(self.workflows)


@dataclass(frozen=True)
class Rule:
    rule_id: str
    file: str
    anchor: str
    concern_key: str
    modality: str
    selectors: Selectors
    review_by: str


def selectors_from_raw(raw: Mapping[str, Any] | None) -> Selectors:
    raw = raw or {}
    return Selectors(
        paths=tuple(raw.get("paths", []) or []),
        workflows=tuple(normalize_workflow_kind(w) for w in (raw.get("workflows", []) or [])),
        surfaces=tuple(raw.get("surfaces", []) or []),
    )


def rules_from_registry(registry: Mapping[str, Mapping[str, Any]]) -> dict[str, Rule]:
    """Convert a parsed registry (from ``corral.governance.registry.parse_registry``)."""
    out: dict[str, Rule] = {}
    for rid, r in registry.items():
        out[rid] = Rule(
            rule_id=rid,
            file=r["file"],
            anchor=r["anchor"],
            concern_key=r["concern_key"],
            modality=r["modality"],
            selectors=selectors_from_raw(r.get("selectors")),
            review_by=r.get("review_by", ""),
        )
    return out


# ---------------------------------------------------------------------------
# Path matching
# ---------------------------------------------------------------------------


def _touched_matches_selector_paths(
    touched: Iterable[str], selector_paths: Iterable[str]
) -> bool:
    """A concrete touched file matches a selector path (a prefix by design)."""
    sel = tuple(selector_paths)
    if not sel:
        return False
    for f in touched:
        for pre in sel:
            if f == pre or f.startswith(pre):
                return True
    return False


# ---------------------------------------------------------------------------
# Exemption (needs_human surfaces, from the adopter's surfaces registry)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExemptionContext:
    """Needs-human exemption facts derived from the surfaces registry."""

    needs_human_surface_ids: frozenset[str]
    needs_human_surface_paths: tuple[str, ...]


def classify_exemption(rule: Rule, ctx: ExemptionContext) -> str | None:
    """Return a human-readable exemption reason, or None if the rule is not exempt.

    Exemption is surfaces-registry-driven: a rule is exempt when its selectors
    touch a ``needs_human`` surface (by surface ID, resolved consistently with
    session matching) or a path under one. The report prints every exempt rule
    with its reason; the check errs toward exemption (a wrongly-exempt rule is
    merely never flagged, while a wrongly-demotable human-review rule would be
    a real regression).
    """
    if set(rule.selectors.surfaces) & ctx.needs_human_surface_ids:
        return "surface selector references a needs_human surface"
    if _touched_matches_selector_paths(ctx.needs_human_surface_paths, rule.selectors.paths):
        return "path selector intersects a needs_human surface path"
    return None


# ---------------------------------------------------------------------------
# Applicability
# ---------------------------------------------------------------------------


def evaluate_session(
    rule: Rule,
    session: Session,
    surface_paths: Mapping[str, frozenset[str]] | None = None,
) -> bool | None:
    """Deterministically decide whether ``rule`` applies to ``session``.

    Returns ``None`` when the session is NOT evaluable for the rule (excluded
    from its denominator), ``True``/``False`` when it is evaluable (in the
    denominator; ``True`` == the rule would have applied).

    ``surface_paths`` is the resolved surface-ID -> path-set mapping (see the
    module docstring). Surface selectors are matched through it in BOTH the
    exemption and session directions.
    """
    sel = rule.selectors
    if sel.is_universal:
        # Applies to every session by construction.
        return True

    workflow_match = (
        session.workflow_kind in sel.workflows if sel.has_workflow_axis else False
    )

    if session.has_path_data:
        path_match = _touched_matches_selector_paths(
            session.touched_paths or frozenset(), sel.paths
        )
        surface_match = False
        if sel.surfaces:
            if surface_paths is None:
                raise ValueError(
                    "surface selectors require a resolved surface-path mapping"
                )
            resolved: set[str] = set()
            for surface_id in sel.surfaces:
                resolved.update(surface_paths.get(surface_id, frozenset()))
            surface_match = _touched_matches_selector_paths(
                session.touched_paths or frozenset(), resolved
            )
        return workflow_match or path_match or surface_match

    # No path data. Honest-denominator rule: evaluable only via the workflow
    # axis. Path/surface-only rules are NOT evaluable here (excluded).
    if sel.has_workflow_axis:
        return workflow_match
    return None


@dataclass
class WindowStats:
    window_days: int
    denominator: int = 0
    numerator: int = 0
    workflow_kinds: set[str] = field(default_factory=set)

    @property
    def applicability(self) -> float:
        return (self.numerator / self.denominator) if self.denominator else 0.0


def compute_window_stats(
    rule: Rule,
    sessions: Iterable[Session],
    *,
    as_of: date,
    window_days: int,
    surface_paths: Mapping[str, frozenset[str]] | None = None,
) -> WindowStats:
    stats = WindowStats(window_days=window_days)
    cutoff_ordinal = as_of.toordinal() - window_days
    for s in sessions:
        if s.when.toordinal() < cutoff_ordinal or s.when > as_of:
            continue
        verdict = evaluate_session(rule, s, surface_paths)
        if verdict is None:
            continue
        stats.denominator += 1
        if verdict:
            stats.numerator += 1
            stats.workflow_kinds.add(s.workflow_kind)
    return stats


@dataclass
class RuleVerdict:
    rule: Rule
    verdict: str
    exemption_reason: str | None
    retain_window: WindowStats
    demote_window: WindowStats
    executable_control: dict[str, Any] | None = None  # from the gotcha registry, if any

    @property
    def rule_id(self) -> str:
        return self.rule.rule_id


def classify_rule(
    rule: Rule,
    sessions: list[Session],
    *,
    as_of: date,
    cfg: StalenessConfig,
    exemption_ctx: ExemptionContext,
    surface_paths: Mapping[str, frozenset[str]] | None = None,
    executable_control: dict[str, Any] | None = None,
) -> RuleVerdict:
    reason = classify_exemption(rule, exemption_ctx)
    retain = compute_window_stats(
        rule, sessions, as_of=as_of, window_days=cfg.retain_days, surface_paths=surface_paths
    )
    demote = compute_window_stats(
        rule, sessions, as_of=as_of, window_days=cfg.demote_days, surface_paths=surface_paths
    )

    def _finish(verdict: str) -> RuleVerdict:
        return RuleVerdict(
            rule=rule,
            verdict=verdict,
            exemption_reason=reason,
            retain_window=retain,
            demote_window=demote,
            executable_control=executable_control,
        )

    is_retained = (
        retain.applicability >= cfg.retain_rate
        and len(retain.workflow_kinds) >= cfg.retain_workflow_count
    )
    if reason is not None:
        # Exempt rules are never demoted; still surface a retain signal for
        # the report, but the actionable verdict is EXEMPT.
        return _finish(EXEMPT)
    if is_retained:
        return _finish(RETAIN)
    # Not retained. Eligible for demotion only with enough evaluable data on
    # the long window AND a sub-floor applicability. Hysteresis: recent retain
    # would already have returned above, so a demote here means both windows
    # agree.
    if demote.denominator < cfg.min_sessions:
        return _finish(INSUFFICIENT_DATA)
    if demote.applicability < cfg.demote_rate:
        return _finish(DEMOTE)
    return _finish(MONITOR)


# ---------------------------------------------------------------------------
# Whole-analysis result
# ---------------------------------------------------------------------------


@dataclass
class AnalysisResult:
    as_of: date
    quarter: str
    total_sessions_long: int
    total_sessions_recent: int
    sessions_with_path_data_long: int
    workflow_kind_counts: dict[str, int]
    verdicts: list[RuleVerdict]

    @property
    def coverage_fraction_long(self) -> float:
        if not self.total_sessions_long:
            return 0.0
        return self.sessions_with_path_data_long / self.total_sessions_long

    def by_verdict(self, verdict: str) -> list[RuleVerdict]:
        return [v for v in self.verdicts if v.verdict == verdict]

    @property
    def demotion_candidates(self) -> list[RuleVerdict]:
        return self.by_verdict(DEMOTE)

    @property
    def executable_pointer_candidates(self) -> list[RuleVerdict]:
        return [v for v in self.verdicts if v.executable_control is not None]


def quarter_label(d: date) -> str:
    return f"{d.year}-Q{(d.month - 1) // 3 + 1}"


def validate_rule_surface_ids(
    rules: Mapping[str, Rule], known_surface_ids: frozenset[str]
) -> list[str]:
    """Return the sorted unknown surface IDs referenced by any rule selector."""
    unknown: set[str] = set()
    for rule in rules.values():
        unknown.update(set(rule.selectors.surfaces) - known_surface_ids)
    return sorted(unknown)


def analyze(
    rules: Mapping[str, Rule],
    sessions: list[Session],
    *,
    as_of: date,
    cfg: StalenessConfig,
    exemption_ctx: ExemptionContext,
    surface_paths: Mapping[str, frozenset[str]],
    gotcha_controls: Mapping[str, dict[str, Any]] | None = None,
) -> AnalysisResult:
    """Run the deterministic applicability analysis for every rule.

    ``surface_paths`` must be the resolved surface-ID -> path-set mapping; the
    analysis fails closed (ValueError) when any rule references an unknown
    surface ID.
    """
    unknown = validate_rule_surface_ids(rules, frozenset(surface_paths))
    if unknown:
        raise ValueError(
            "registry selectors reference unknown surface ids "
            f"{unknown}; declare them in the surfaces registry"
        )
    gotcha_controls = gotcha_controls or {}
    ordinal_recent = as_of.toordinal() - cfg.retain_days
    ordinal_long = as_of.toordinal() - cfg.demote_days
    in_long = [
        s for s in sessions if ordinal_long <= s.when.toordinal() and s.when <= as_of
    ]
    in_recent = [s for s in in_long if s.when.toordinal() >= ordinal_recent]

    workflow_counts: dict[str, int] = {}
    for s in in_long:
        workflow_counts[s.workflow_kind] = workflow_counts.get(s.workflow_kind, 0) + 1

    verdicts: list[RuleVerdict] = []
    for rid in sorted(rules):
        rule = rules[rid]
        control = gotcha_controls.get(rid)
        exec_control = (
            control
            if (control and control.get("control_type") in EXECUTABLE_CONTROL_TYPES)
            else None
        )
        verdicts.append(
            classify_rule(
                rule,
                sessions,
                as_of=as_of,
                cfg=cfg,
                exemption_ctx=exemption_ctx,
                surface_paths=surface_paths,
                executable_control=exec_control,
            )
        )

    return AnalysisResult(
        as_of=as_of,
        quarter=quarter_label(as_of),
        total_sessions_long=len(in_long),
        total_sessions_recent=len(in_recent),
        sessions_with_path_data_long=sum(1 for s in in_long if s.has_path_data),
        workflow_kind_counts=dict(sorted(workflow_counts.items())),
        verdicts=verdicts,
    )


def parse_session_when(row: Mapping[str, Any], *, fallback: date) -> date:
    """Best-effort session date from a rollup row (ended_at, then started_at)."""
    for key in ("ended_at", "started_at"):
        val = row.get(key)
        if val is None:
            continue
        if isinstance(val, datetime):
            return val.date()
        if isinstance(val, date):
            return val
        try:
            return datetime.fromisoformat(str(val).replace("Z", "+00:00")).date()
        except (ValueError, TypeError):
            continue
    return fallback


__all__ = [
    "CANONICAL_WORKFLOW_KINDS",
    "DEMOTE",
    "EXECUTABLE_CONTROL_TYPES",
    "EXEMPT",
    "INSUFFICIENT_DATA",
    "MONITOR",
    "RETAIN",
    "UNKNOWN_WORKFLOW_KIND",
    "AnalysisResult",
    "ExemptionContext",
    "Rule",
    "RuleVerdict",
    "Selectors",
    "Session",
    "WindowStats",
    "analyze",
    "classify_exemption",
    "classify_rule",
    "compute_window_stats",
    "evaluate_session",
    "normalize_workflow_kind",
    "parse_session_when",
    "quarter_label",
    "rules_from_registry",
    "selectors_from_raw",
    "validate_rule_surface_ids",
]
