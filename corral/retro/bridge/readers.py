"""Sanitized file-backed evidence readers for the weekly retrospective.

Two readers, both fail-closed on anything suspicious:

- :func:`load_memory_corpus` ingests markdown memory files (``feedback``
  records always; ``project`` records only when gotcha-flavored) from
  configured corpus roots, including ``*/memory`` project leaves under a
  projects-style root.
- :func:`load_run_artifacts` ingests structured top-level artifacts from run
  audit directories.

Every record is credential-scrubbed (:mod:`corral.retro.bridge.security`)
before it can reach a prompt or the weekly summary, and the outbound gate
re-checks the fully assembled record. Symlinked roots/artifacts and records
escaping their containment root are skipped fail-closed.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import timezone, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from corral.retro.bridge.security import (
    UnsafeBridgeRecordError,
    assert_safe_record,
    sanitize_text,
)
from corral.retro.types import BridgeEvidence, EvidenceGroup

STRUCTURED_RUN_FILES = ("final_report.md", "decision.md", "blockers.md", "commands_run.md")
MAX_BRIDGE_TEXT_CHARS = 2400  # prompt-size cap for one bridge evidence row.
MAX_RUN_DIRS = 50  # bounded weekly scan of recent local audit trails.
MAX_SUMMARY_CHARS = 400  # bounded bridge heading in external prompts.
MAX_REPO_PATHS = 25  # bounded inferred-path fanout per bridge row.
MAX_REPO_PATH_CHARS = 240  # bounded individual inferred path.

MEMORY_AGENT = "memory"
RUN_AUDIT_AGENT = "run-audit"

#: Neutral top-level path prefixes recognized when inferring repo paths from
#: bridge text. Extend per repository as needed.
REPO_PATH_PREFIXES = (
    "config",
    "scripts",
    "src",
    "tests",
    "docs",
    "agent_memory",
    "agent_telemetry",
    ".github",
)

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(?P<header>.*?)\n---\s*\n(?P<body>.*)\Z", re.DOTALL)
_INCIDENT_LINE_RE = re.compile(
    r"(?im)^\s*(?:incident_ref|incident)\s*:\s*([A-Za-z0-9_.:/-]{3,120})\s*$"
)
_REPO_PATH_RE = re.compile(
    r"(?<![\w/.-])((?:" + "|".join(re.escape(prefix) for prefix in REPO_PATH_PREFIXES) +
    r")/[A-Za-z0-9_./-]+)"
)
_GOTCHA_TERMS = (
    "gotcha",
    "incident",
    "regression",
    "failure",
    "fixup",
    "lesson",
    "mistake",
)


def load_bridge_evidence(
    *,
    memory_roots: Sequence[str | Path] = (),
    run_artifact_roots: Sequence[str | Path] = (),
) -> list[BridgeEvidence]:
    """Load sanitized bridge evidence from every configured root."""
    records: list[BridgeEvidence] = []
    records.extend(load_memory_corpus(memory_roots))
    records.extend(load_run_artifacts(run_artifact_roots))
    return records


def load_memory_corpus(roots: Sequence[str | Path]) -> list[BridgeEvidence]:
    """Load sanitized evidence from memory files under the configured roots."""
    records: list[BridgeEvidence] = []
    for configured in roots:
        memory_root = Path(str(configured)).expanduser()
        if not memory_root.is_dir():
            continue
        if memory_root.is_symlink():
            continue
        try:
            memory_dirs = _memory_dirs(memory_root)
        except OSError:
            continue
        if not memory_dirs:
            continue
        try:
            files = sorted(
                (path, root)
                for root in memory_dirs
                for path in root.rglob("*.md")
                if path.is_file()
            )
        except OSError:
            continue
        for path, containment_root in files:
            try:
                parsed = _read_memory_file(path, containment_root)
            except (UnsafeBridgeRecordError, OSError):
                continue
            if parsed is not None:
                records.append(parsed)
    return records


def _memory_dirs(root: Path) -> list[Path]:
    if root.name == "memory":
        return [root]
    resolved_root = root.resolve(strict=True)
    selected = []
    for path in root.glob("*/memory"):
        if path.is_symlink():
            continue
        resolved = path.resolve(strict=True)
        if path.is_dir() and resolved.is_relative_to(resolved_root):
            selected.append(path)
    return sorted(selected)


def load_run_artifacts(roots: Sequence[str | Path]) -> list[BridgeEvidence]:
    """Load sanitized evidence from structured top-level run artifacts only."""
    records: list[BridgeEvidence] = []
    for configured in roots:
        runs_root = Path(str(configured)).expanduser()
        if not runs_root.is_dir() or runs_root.is_symlink():
            continue
        try:
            candidates = list(runs_root.iterdir())
        except OSError:
            continue
        dated_run_dirs: list[tuple[float, Path]] = []
        for path in candidates:
            try:
                if path.is_dir() and not path.is_symlink():
                    dated_run_dirs.append((path.stat().st_mtime, path))
            except OSError:
                continue
        run_dirs = [path for _, path in sorted(dated_run_dirs, reverse=True)[:MAX_RUN_DIRS]]
        for run_dir in run_dirs:
            try:
                record = _read_run_dir(run_dir, runs_root)
            except (UnsafeBridgeRecordError, OSError):
                continue
            if record is not None:
                records.append(record)
    return records


def _read_memory_file(path: Path, containment_root: Path) -> BridgeEvidence | None:
    resolved_root = containment_root.resolve(strict=True)
    resolved_path = path.resolve(strict=True)
    if not resolved_path.is_relative_to(resolved_root):
        raise UnsafeBridgeRecordError("memory record escapes selected corpus")
    try:
        raw = resolved_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raw = resolved_path.read_text(encoding="utf-8", errors="replace")
    frontmatter, body = _split_frontmatter(raw)
    memory_type = frontmatter.get("type", "").lower()
    combined = "\n".join(
        part for part in (frontmatter.get("name"), frontmatter.get("description"), body) if part
    )
    if memory_type != "feedback" and not (
        memory_type == "project" and _is_gotcha_flavored(combined)
    ):
        return None
    modified_dt = datetime.fromtimestamp(resolved_path.stat().st_mtime, tz=timezone.utc)
    sanitized = sanitize_text(combined)
    repo_paths = _extract_repo_paths(sanitized)
    normalized = _normalize_relative_dates(sanitized, modified_dt, protected_terms=repo_paths)
    rel = _source_component(path.relative_to(containment_root).as_posix())
    project = _source_component(containment_root.parent.name)
    record = BridgeEvidence(
        source_ref=sanitize_text(f"memory:{project}/{rel}"),
        incident_ref=_explicit_incident_ref(
            frontmatter.get("incident_ref") or frontmatter.get("incident") or ""
        ),
        agent=MEMORY_AGENT,
        area=_area(repo_paths, default="memory"),
        summary=_truncate_summary(sanitize_text(_summary(frontmatter, normalized))),
        text=_truncate(normalized),
        repo_paths=repo_paths,
        modified=modified_dt.date().isoformat(),
    )
    assert_safe_record(record)
    return record


def _read_run_dir(run_dir: Path, runs_root: Path) -> BridgeEvidence | None:
    resolved_root = runs_root.resolve(strict=True)
    resolved_run = run_dir.resolve(strict=True)
    if not resolved_run.is_relative_to(resolved_root):
        raise UnsafeBridgeRecordError("run audit escapes selected audit root")
    chunks = []
    for name in STRUCTURED_RUN_FILES:
        path = resolved_run / name
        if not path.is_file():
            continue
        if path.is_symlink():
            raise UnsafeBridgeRecordError("structured run artifact is symlinked")
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        chunks.append(f"## {name}\n{text}")
    if not chunks:
        return None
    combined = "\n\n".join(chunks)
    if not _is_gotcha_flavored(combined):
        return None
    sanitized = sanitize_text(combined)
    rel = _source_component(run_dir.relative_to(runs_root).as_posix())
    repo_paths = _extract_repo_paths(sanitized)
    modified_dt = datetime.fromtimestamp(resolved_run.stat().st_mtime, tz=timezone.utc)
    record = BridgeEvidence(
        source_ref=sanitize_text(f"{RUN_AUDIT_AGENT}:{rel}"),
        incident_ref=_explicit_incident_ref(_extract_incident_ref(sanitized)),
        agent=RUN_AUDIT_AGENT,
        area=_area(repo_paths, default="run-audits"),
        summary=_truncate_summary(sanitize_text(_first_meaningful_line(sanitized) or rel)),
        text=_truncate(sanitized),
        repo_paths=repo_paths,
        modified=modified_dt.date().isoformat(),
    )
    assert_safe_record(record)
    return record


def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    header = {}
    for line in match.group("header").splitlines():
        key, sep, value = line.partition(":")
        if sep:
            header[key.strip().lower()] = value.strip().strip("\"'")
    return header, match.group("body")


def _normalize_relative_dates(
    text: str,
    modified: datetime,
    *,
    protected_terms: Sequence[str] = (),
) -> str:
    base = modified.date()
    replacements = {
        "today": base,
        "yesterday": base - timedelta(days=1),
        "tomorrow": base + timedelta(days=1),
        "last week": base - timedelta(days=7),
        "a week ago": base - timedelta(days=7),
        "next week": base + timedelta(days=7),
    }
    normalized = text
    placeholders: dict[str, str] = {}
    for index, term in enumerate(sorted(set(protected_terms), key=len, reverse=True)):
        placeholder = f"__RETRO_PROTECTED_{index}__"
        placeholders[placeholder] = term
        normalized = normalized.replace(term, placeholder)
    for phrase, value in replacements.items():
        normalized = re.sub(
            rf"\b{re.escape(phrase)}\b",
            value.isoformat(),
            normalized,
            flags=re.IGNORECASE,
        )
    normalized = re.sub(
        r"\b(\d+)\s+days?\s+ago\b",
        lambda match: (base - timedelta(days=int(match.group(1)))).isoformat(),
        normalized,
        flags=re.IGNORECASE,
    )
    normalized = re.sub(
        r"\b(\d+)\s+weeks?\s+ago\b",
        lambda match: (base - timedelta(days=7 * int(match.group(1)))).isoformat(),
        normalized,
        flags=re.IGNORECASE,
    )
    normalized = re.sub(
        r"\bin\s+(\d+)\s+days?\b",
        lambda match: (base + timedelta(days=int(match.group(1)))).isoformat(),
        normalized,
        flags=re.IGNORECASE,
    )
    normalized = re.sub(
        r"\bin\s+(\d+)\s+weeks?\b",
        lambda match: (base + timedelta(days=7 * int(match.group(1)))).isoformat(),
        normalized,
        flags=re.IGNORECASE,
    )
    for placeholder, term in placeholders.items():
        normalized = normalized.replace(placeholder, term)
    return normalized


def _extract_repo_paths(text: str) -> tuple[str, ...]:
    paths = []
    for match in _REPO_PATH_RE.findall(text):
        cleaned = match.rstrip(".,);:]`'\"")[:MAX_REPO_PATH_CHARS]
        if cleaned not in paths:
            paths.append(cleaned)
        if len(paths) >= MAX_REPO_PATHS:
            break
    return tuple(paths)


def _is_gotcha_flavored(text: str) -> bool:
    lowered = text.lower()
    return any(term in lowered for term in _GOTCHA_TERMS)


def _summary(frontmatter: dict[str, str], text: str) -> str:
    return frontmatter.get("description") or frontmatter.get("name") or _first_meaningful_line(text)


def _first_meaningful_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip("#- ")
        if stripped:
            return stripped
    return ""


def _truncate(text: str) -> str:
    return text[:MAX_BRIDGE_TEXT_CHARS]


def _truncate_summary(text: str) -> str:
    return text[:MAX_SUMMARY_CHARS]


def _area(repo_paths: Sequence[str], *, default: str) -> str:
    if not repo_paths:
        return default
    return repo_paths[0].split("/", 1)[0]


def _extract_incident_ref(text: str) -> str:
    match = _INCIDENT_LINE_RE.search(text)
    return match.group(1) if match else ""


def _explicit_incident_ref(value: str) -> str:
    """Return a canonical root only for an explicitly structured incident id."""
    value = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9_.:/-]{3,120}", value):
        return ""
    return sanitize_text(f"bridge-incident:{value}")


def _source_component(value: str) -> str:
    """Return a stable, delimiter-safe source-ref component."""
    return quote(sanitize_text(value), safe="/._-")


def merge_bridge_groups(
    groups: list[EvidenceGroup],
    records: Sequence[BridgeEvidence],
) -> list[EvidenceGroup]:
    """Attach bridge records to matching evidence groups (or spawn bridge-only groups)."""
    if not records:
        return groups
    grouped: dict[str, list[BridgeEvidence]] = {}
    for record in records:
        grouped.setdefault(_bridge_group_key(record), []).append(record)
    by_key = {group.key: group for group in groups}
    for key, grouped_records in grouped.items():
        existing = by_key.get(key)
        if existing is not None:
            replacement = EvidenceGroup(
                key=existing.key,
                agent=existing.agent,
                area=existing.area,
                pairs=existing.pairs,
                extra_notes=existing.extra_notes,
                note_source_prs=existing.note_source_prs,
                bridge_evidence=tuple(grouped_records),
            )
            groups[groups.index(existing)] = replacement
            by_key[key] = replacement
            continue
        groups.append(
            EvidenceGroup(
                key=key,
                agent=grouped_records[0].agent,
                area=grouped_records[0].area,
                pairs=(),
                bridge_evidence=tuple(grouped_records),
            )
        )
    return groups


def render_bridge_evidence(records: Sequence[BridgeEvidence]) -> str:
    if not records:
        return "(none)"
    blocks = []
    for record in records:
        paths = ", ".join(record.repo_paths) if record.repo_paths else "(none inferred)"
        modified = f" | modified: {record.modified}" if record.modified else ""
        incident = record.incident_ref or "(corroboration only)"
        blocks.append(
            f"- Source: `{record.source_ref}` | incident: `{incident}`{modified}\n"
            f"  Repo paths: {paths}\n"
            f"  Summary: {record.summary}\n"
            f"  Text:\n{record.text}"
        )
    return "\n".join(blocks)


def group_repo_paths(group: EvidenceGroup) -> set[str]:
    """Return every repo path represented by a mixed evidence group."""
    paths = {path for pair in group.pairs for path in pair.shared_files}
    paths.update(path for record in group.bridge_evidence for path in record.repo_paths)
    return paths


def render_group_evidence(group: EvidenceGroup, excerpts: Mapping[int, str]) -> str:
    """Render the common evidence bundle used by drafting and verification."""
    pairs = [
        f"- PR #{pair.original_pr} -> #{pair.fixup_pr}; shared files "
        f"{', '.join(pair.shared_files) or '(none)'}\n  "
        f"{excerpts.get(pair.original_pr, '') or '(no excerpt)'}"
        for pair in group.pairs
    ]
    notes = "\n".join(f"- {note}" for note in group.extra_notes) or "(none)"
    return (
        "## fix-up pairs\n"
        + ("\n".join(pairs) or "(none)")
        + f"\n## notes\n{notes}\n## sanitized file-backed bridge evidence\n"
        + render_bridge_evidence(group.bridge_evidence)
    )


def _bridge_group_key(record: BridgeEvidence) -> str:
    primary = min(record.repo_paths) if record.repo_paths else record.area
    return f"{record.agent}::{primary}"


__all__ = [
    "MAX_BRIDGE_TEXT_CHARS",
    "MAX_RUN_DIRS",
    "MEMORY_AGENT",
    "RUN_AUDIT_AGENT",
    "STRUCTURED_RUN_FILES",
    "group_repo_paths",
    "load_bridge_evidence",
    "load_memory_corpus",
    "load_run_artifacts",
    "merge_bridge_groups",
    "render_bridge_evidence",
    "render_group_evidence",
]
