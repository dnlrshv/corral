"""Semantic validation for LLM-generated preflight briefs."""

from __future__ import annotations

import posixpath
import re
from pathlib import Path
from typing import Any

import yaml

PR_REF_RE = re.compile(r"^#\d+$")

# Intent per LLM-proposed path field. "read"/"modify" require the path to
# already exist as a regular file. "create_or_modify" (test_files) accepts
# either an existing regular file (update) or a not-yet-existing path whose
# parent directory exists under one of the recognized modules.
PATH_FIELD_INTENTS: dict[str, str] = {
    "files_to_touch": "modify",
    "files_to_read_only": "read",
    "test_files": "create_or_modify",
}


class BriefQualityError(ValueError):
    """Raised when an LLM-authored brief has too many hallucinated paths to trust.

    Distinct from the generic ``ValueError`` raised by structural brief
    validation so callers can attribute the resulting fallback to
    ``fallback_reason: semantic_quality`` instead of a generic parse/auth
    failure.
    """


def recognized_modules_from_code_map(code_map_dir: Path) -> frozenset[str]:
    """Derive recognized top-level modules from code-map artifacts.

    Design choice: the source implementation hardcoded the adopting project's
    top-level package directories. corral instead derives the set from the
    code-map artifacts produced by ``corral codemap build``: the top-level
    directory of every file recorded in ``symbols.parquet`` (function/class
    owners) unioned with ``imports.parquet`` (import sources). Root-level
    files contribute nothing — recognized modules are the directories under
    which the LLM may propose NEW files during ``test_files`` validation.
    Projects can override the derived set via the
    ``preflight.recognized_modules`` list in ``corral.yaml``.
    """
    modules: set[str] = set()
    for filename, column in (("symbols.parquet", "file"), ("imports.parquet", "source_file")):
        artifact = code_map_dir / filename
        if not artifact.is_file():
            continue
        try:
            import pyarrow.parquet as pq

            files = pq.read_table(artifact, columns=[column]).column(column).to_pylist()
        except Exception:
            continue
        modules.update(
            path.split("/", 1)[0]
            for path in files
            if isinstance(path, str) and "/" in path
        )
    return frozenset(modules)


def _is_well_formed_relative_path(path: str) -> bool:
    """Reject absolute paths, home-relative paths, traversal, and non-normalized forms."""
    if not path or path.startswith(("/", "~")):
        return False
    if posixpath.normpath(path) != path:
        return False
    return not any(part == ".." for part in path.split("/"))


def _has_symlink_component(repo_root: Path, rel_path: str) -> bool:
    """True if any existing ancestor of rel_path, inclusive, is a symlink."""
    current = repo_root
    for part in rel_path.split("/"):
        current = current / part
        if current.is_symlink():
            return True
    return False


def _is_creatable_path(rel_path: str, full: Path, recognized_modules: frozenset[str]) -> bool:
    if not full.parent.is_dir():
        return False
    top_level = rel_path.split("/", 1)[0]
    return top_level in recognized_modules


def validate_path_intent(
    repo_root: Path, rel_path: Any, intent: str, recognized_modules: frozenset[str]
) -> bool:
    """Return True when rel_path passes intent-aware structural + existence checks."""
    if not isinstance(rel_path, str) or not _is_well_formed_relative_path(rel_path):
        return False
    if _has_symlink_component(repo_root, rel_path):
        return False
    full = repo_root / rel_path
    if intent in ("read", "modify"):
        return full.is_file()
    if intent == "create_or_modify":
        if full.exists():
            return full.is_file()
        return _is_creatable_path(rel_path, full, recognized_modules)
    raise ValueError(f"unknown path intent: {intent!r}")


def _filter_valid_paths(
    paths: Any, intent: str, repo_root: Path, recognized_modules: frozenset[str]
) -> tuple[list[str], int]:
    """Return (valid_paths, invalid_count) for a proposed path list."""
    if not isinstance(paths, list):
        return [], 0
    valid: list[str] = []
    invalid = 0
    for path in paths:
        if validate_path_intent(repo_root, path, intent, recognized_modules):
            valid.append(path)
        else:
            invalid += 1
    return valid, invalid


def filter_valid_surfaces(surfaces: Any, code_map_yaml: str) -> list[str]:
    """Return only entries that are real surface-registry keys."""
    if not isinstance(surfaces, list):
        return []
    parsed = yaml.safe_load(code_map_yaml) or {}
    known = parsed.get("surfaces", {})
    known_ids = set(known) if isinstance(known, dict) else set()
    return [s for s in surfaces if isinstance(s, str) and s in known_ids]


def filter_valid_pr_refs(prs: Any) -> list[str]:
    """Return only well-formed "#123"-style PR/issue references."""
    if not isinstance(prs, list):
        return []
    return [p for p in prs if isinstance(p, str) and PR_REF_RE.match(p)]


def apply_semantic_validation(
    brief: dict[str, Any],
    code_map_yaml: str,
    repo_root: Path,
    recognized_modules: frozenset[str],
) -> None:
    """Drop hallucinated entries from an LLM-authored brief in place."""
    valid_paths = 0
    invalid_paths = 0
    for field, intent in PATH_FIELD_INTENTS.items():
        valid, invalid = _filter_valid_paths(brief.get(field), intent, repo_root, recognized_modules)
        valid_paths += len(valid)
        invalid_paths += invalid
        brief[field] = valid

    brief["surfaces_in_scope"] = filter_valid_surfaces(
        brief.get("surfaces_in_scope"), code_map_yaml
    )
    brief["recent_related_prs"] = filter_valid_pr_refs(brief.get("recent_related_prs"))

    if invalid_paths > valid_paths:
        raise BriefQualityError(
            f"{invalid_paths}/{invalid_paths + valid_paths} LLM-proposed paths "
            "failed intent-aware validation (existence, traversal, or symlink checks)"
        )
