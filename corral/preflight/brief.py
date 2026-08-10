"""Generate a preflight brief for an interactive agent session."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from corral.preflight import gotcha_budget, quota
from corral.preflight.auth import (
    DEFAULT_PREFLIGHT_MODEL,
    PreflightLLMResponse,
    call_llm_with_meta,
    has_llm_auth,
)
from corral.preflight.brief_validation import (
    BriefQualityError,
    apply_semantic_validation,
)
from corral.preflight.parser import parse_brief, sanitize_preflight_error
from corral.preflight.retry import (
    LIST_FIELD_CAPS,
    BriefResponseError,
    generate_brief_with_retry,
    issue_text,
    validate_brief,
)

AGENT_GOTCHAS_FIELD = "agent_gotchas"
AUTO_BRANCH_RE = re.compile(r"(?:agent|feat|fix|claude|codex)/(\d+)-")
FINGERPRINT_HEADER_PREFIX = "# preflight_fingerprint: "
GENERAL_SURFACE_CAP = 8
DEFAULT_MAX_TOKENS = 1500
PATH_MENTION_RE = re.compile(
    r"(?<![\w./-])(?:[\w.-]+/)+[\w.-]+\.(?:py|ya?ml|json|toml|md|sh)|"
    r"(?<![\w./-])(?:AGENTS|CLAUDE|README)\.md(?![\w./-])"
)


def fetch_issue(issue_number: int) -> dict[str, Any]:
    result = subprocess.run(
        [
            "gh",
            "issue",
            "view",
            str(issue_number),
            "--json",
            "number,title,body,labels,url",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return json.loads(result.stdout)


def get_git_head_sha(root: Path) -> str:
    """Return HEAD's sha, or "" when git or the repository is unavailable.

    Fail-soft on purpose: the fingerprint only needs to be stable for a given
    tree state, and preflight must keep working outside a git checkout.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
            cwd=root,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip()


def get_current_branch(root: Path) -> str:
    """Return the current branch name, or "" when git is unavailable."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
            cwd=root,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip()


def issue_number_from_branch(branch: str) -> int | None:
    match = AUTO_BRANCH_RE.search(branch)
    if match is None:
        return None
    return int(match.group(1))


def _short_task_hash(task_text: str) -> str:
    return hashlib.sha256(task_text.encode()).hexdigest()[:8]


def compute_fingerprint(mode: str, payload: str, git_head_sha: str) -> str:
    return hashlib.sha256(f"{mode}{payload}{git_head_sha}".encode()).hexdigest()[:12]


def fingerprint_header(fingerprint: str) -> str:
    return f"{FINGERPRINT_HEADER_PREFIX}{fingerprint}"


def output_has_fingerprint(output_path: Path, fingerprint: str) -> bool:
    if not output_path.exists():
        return False
    with output_path.open() as handle:
        first_line = handle.readline().rstrip("\n")
    return first_line == fingerprint_header(fingerprint)


def load_surfaces_yaml(surfaces_path: Path) -> str:
    return surfaces_path.read_text()


def extract_needs_human_paths(code_map_yaml: str) -> list[str]:
    """Return all paths from surfaces entries with `needs_human: true`.

    `do_not_touch` is sourced from this deterministic set, not LLM judgment,
    so high-risk surfaces cannot be silently dropped from the brief."""
    parsed = yaml.safe_load(code_map_yaml) or {}
    surfaces = parsed.get("surfaces", {})
    if not isinstance(surfaces, dict):
        return []
    paths: list[str] = []
    seen: set[str] = set()
    for entry in surfaces.values():
        if not isinstance(entry, dict) or not entry.get("needs_human"):
            continue
        for path in entry.get("paths", []) or []:
            if isinstance(path, str) and path not in seen:
                seen.add(path)
                paths.append(path)
    return paths


def load_agent_gotchas(gotchas_path: Path) -> list[dict[str, Any]]:
    """Load agent gotcha records from the shared memory registry."""
    if not gotchas_path.exists():
        return []
    payload = json.loads(gotchas_path.read_text())
    if not isinstance(payload, dict):
        return []
    gotchas = payload.get("gotchas", [])
    if not isinstance(gotchas, list):
        return []
    return [entry for entry in gotchas if isinstance(entry, dict)]


def _is_expired(entry: dict[str, Any], today: date) -> bool:
    expires = entry.get("expires")
    if not isinstance(expires, str):
        return False
    try:
        return date.fromisoformat(expires) < today
    except ValueError:
        return False


def _current_workflow_kind(workflow_kinds: Mapping[str, str]) -> str | None:
    """Best-effort workflow-kind inference from the GitHub Actions
    ``GITHUB_WORKFLOW`` env var (always set to the workflow's ``name:`` on GH
    Actions runners), using the ``preflight.workflow_kinds`` mapping from
    ``corral.yaml``. Returns None outside CI or for an unrecognized workflow
    name; callers must treat that as "don't filter on workflow_kinds", not as
    an error. The mapping ships empty: workflow names are per-adopter CI
    configuration, so an unrecognized name never maps to a guessed value.
    """
    name = os.environ.get("GITHUB_WORKFLOW")
    if name is None:
        return None
    return workflow_kinds.get(name)


def _gotcha_matches_context(
    entry: dict[str, Any],
    touched: set[str],
    scoped_surfaces: set[str],
    workflow_kind: str | None,
) -> bool:
    """Return True if any of the entry's three schema-v2 dimensions match.

    Schema v2 replaced the overloaded ``applies_to_surfaces`` field — which
    previously held either workflow-kind strings or file paths
    interchangeably and was always compared against file paths, so
    workflow-kind-only entries could never match — with three explicit
    dimensions matched independently.
    """
    repo_paths = [p for p in entry.get("repo_paths", []) if isinstance(p, str)]
    if (
        touched
        and repo_paths
        and any(fnmatch.fnmatch(path, pattern) for path in touched for pattern in repo_paths)
    ):
        return True
    entry_surfaces = {s for s in entry.get("surface_ids", []) if isinstance(s, str)}
    if scoped_surfaces and (scoped_surfaces & entry_surfaces):
        return True
    entry_kinds = {k for k in entry.get("workflow_kinds", []) if isinstance(k, str)}
    return workflow_kind is not None and workflow_kind in entry_kinds


def filter_briefer_gotchas(
    gotchas: list[dict[str, Any]],
    files_to_touch: list[Any],
    today: date | None = None,
    *,
    surface_ids: list[Any] | None = None,
    workflow_kind: str | None = None,
    workflow_kinds: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Return matching non-expired gotchas, capped for briefer token budget."""
    touched = {path for path in files_to_touch if isinstance(path, str)}
    scoped_surfaces = {s for s in (surface_ids or []) if isinstance(s, str)}
    if workflow_kind is None:
        workflow_kind = _current_workflow_kind(workflow_kinds or {})

    today = today or date.today()
    filtered: list[dict[str, Any]] = []
    for entry in gotchas:
        if entry.get("inject_into_briefer") is not True:
            continue
        if _is_expired(entry, today):
            continue
        if _gotcha_matches_context(entry, touched, scoped_surfaces, workflow_kind):
            filtered.append(entry)
    return gotcha_budget.cap_briefer_gotchas(filtered)


def filter_general_briefer_gotchas(
    gotchas: list[dict[str, Any]],
    today: date | None = None,
) -> list[dict[str, Any]]:
    """Return non-expired gotchas explicitly marked for briefer injection."""
    today = today or date.today()
    filtered = [
        entry
        for entry in gotchas
        if entry.get("inject_into_briefer") is True and not _is_expired(entry, today)
    ]
    return gotcha_budget.cap_briefer_gotchas(filtered)


def select_general_surface_ids(code_map_yaml: str, cap: int = GENERAL_SURFACE_CAP) -> list[str]:
    """Return high-risk surface IDs for an issue-free deterministic brief.

    Two-pass selection: collect every ``needs_human`` surface first, then
    fill remaining slots with ``needs_equivalence_check`` /
    ``needs_shadow_run`` / ``needs_backtest`` surfaces. Cap last so
    high-risk human-gated surfaces are never silently dropped.
    """
    parsed = yaml.safe_load(code_map_yaml) or {}
    surfaces = parsed.get("surfaces", {})
    if not isinstance(surfaces, dict):
        return []

    human: list[str] = []
    other: list[str] = []
    for surface_id, entry in surfaces.items():
        if not isinstance(surface_id, str) or not isinstance(entry, dict):
            continue
        if entry.get("needs_human") is True:
            human.append(surface_id)
        elif (
            entry.get("needs_equivalence_check") is True
            or entry.get("needs_shadow_run") is True
            or entry.get("needs_backtest") is True
        ):
            other.append(surface_id)
    return (human + other)[:cap]


def build_task_issue(task_text: str) -> dict[str, Any]:
    return {"title": "Interactive task", "body": task_text}


def generate_brief(
    issue: dict[str, Any],
    surfaces_yaml: str,
    *,
    gotchas_path: Path,
    repo_root: Path,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    model: str = DEFAULT_PREFLIGHT_MODEL,
    recognized_modules: frozenset[str] = frozenset(),
    workflow_kinds: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    def call_model(
        prompt: str, call_max_tokens: int, history: list[dict[str, str]] | None = None
    ) -> PreflightLLMResponse:
        return call_llm_with_meta(prompt, call_max_tokens, history=history, model=model)

    brief, stop_reason = generate_brief_with_retry(
        issue=issue,
        code_map_yaml=surfaces_yaml,
        max_tokens=max_tokens,
        call_llm_with_meta=call_model,
        parse_brief=parse_brief,
        extract_needs_human_paths=extract_needs_human_paths,
    )
    apply_semantic_validation(brief, surfaces_yaml, repo_root, recognized_modules)
    brief[AGENT_GOTCHAS_FIELD] = filter_briefer_gotchas(
        load_agent_gotchas(gotchas_path),
        brief["files_to_touch"],
        surface_ids=brief.get("surfaces_in_scope"),
        workflow_kinds=workflow_kinds,
    )
    brief["stop_reason"] = stop_reason
    return brief


def generate_general_brief(
    surfaces_yaml: str,
    *,
    gotchas_path: Path,
    workflow_kinds: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    brief: dict[str, Any] = {
        "files_to_touch": [],
        "files_to_read_only": [],
        "surfaces_in_scope": select_general_surface_ids(surfaces_yaml),
        "cross_cutting_concerns": [
            "No issue or task supplied; inspect high-risk surfaces before editing."
        ],
        "recent_related_prs": [],
        "invariants_to_preserve": [
            "Preserve high-risk surface controls and production safety rules."
        ],
        "test_files": [],
        "estimated_blast_radius": "medium",
        "do_not_touch": extract_needs_human_paths(surfaces_yaml),
    }
    validate_brief(brief)
    brief[AGENT_GOTCHAS_FIELD] = filter_general_briefer_gotchas(
        load_agent_gotchas(gotchas_path)
    )
    return brief


def extract_path_mentions(text: str) -> list[str]:
    seen: set[str] = set()
    paths: list[str] = []
    for match in PATH_MENTION_RE.finditer(text):
        path = match.group(0).strip("`'\"),.;:")
        if path not in seen:
            seen.add(path)
            paths.append(path)
    return paths


def select_surface_ids_for_paths(
    code_map_yaml: str,
    paths: list[str],
    cap: int = GENERAL_SURFACE_CAP,
) -> list[str]:
    if not paths:
        return []
    parsed = yaml.safe_load(code_map_yaml) or {}
    surfaces = parsed.get("surfaces", {})
    if not isinstance(surfaces, dict):
        return []

    matched: list[str] = []
    seen: set[str] = set()
    for surface_id, entry in surfaces.items():
        if not isinstance(surface_id, str) or not isinstance(entry, dict):
            continue
        surface_paths = [path for path in entry.get("paths", []) or [] if isinstance(path, str)]
        if any(path in surface_paths for path in paths) and surface_id not in seen:
            seen.add(surface_id)
            matched.append(surface_id)
    return matched[:cap]


def generate_fallback_brief(
    issue: dict[str, Any],
    surfaces_yaml: str,
    *,
    gotchas_path: Path,
    workflow_kinds: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Generate a deterministic brief when the LLM auth/API is unavailable."""
    mentioned_paths = extract_path_mentions(issue_text(issue))
    surfaces_in_scope = select_surface_ids_for_paths(surfaces_yaml, mentioned_paths)
    if not surfaces_in_scope:
        surfaces_in_scope = select_general_surface_ids(surfaces_yaml)

    brief: dict[str, Any] = {
        "files_to_touch": [],
        "files_to_read_only": mentioned_paths[: LIST_FIELD_CAPS["files_to_read_only"]],
        "surfaces_in_scope": surfaces_in_scope[: LIST_FIELD_CAPS["surfaces_in_scope"]],
        "cross_cutting_concerns": [
            "Preflight LLM was unavailable; using deterministic code-map fallback."
        ],
        "recent_related_prs": [],
        "invariants_to_preserve": [
            "Preserve high-risk surface controls and production safety rules."
        ],
        "test_files": [],
        "estimated_blast_radius": "medium" if surfaces_in_scope else "low",
        "do_not_touch": extract_needs_human_paths(surfaces_yaml),
        "preflight_status": "fallback",
    }
    validate_brief(brief)
    gotchas = load_agent_gotchas(gotchas_path)
    brief[AGENT_GOTCHAS_FIELD] = (
        filter_briefer_gotchas(
            gotchas,
            mentioned_paths,
            surface_ids=brief.get("surfaces_in_scope"),
            workflow_kinds=workflow_kinds,
        )
        if mentioned_paths
        else filter_general_briefer_gotchas(gotchas)
    )
    return brief


def format_brief_output(brief: dict[str, Any], fingerprint: str) -> str:
    output_text = yaml.dump(brief, default_flow_style=False, allow_unicode=True)
    return f"{fingerprint_header(fingerprint)}\n{output_text}"


def _generate_brief_with_fallback(
    issue: dict[str, Any],
    surfaces_yaml: str,
    *,
    max_tokens: int,
    strict: bool,
    gotchas_path: Path,
    repo_root: Path,
    model: str,
    recognized_modules: frozenset[str],
    workflow_kinds: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    try:
        brief = generate_brief(
            issue,
            surfaces_yaml,
            gotchas_path=gotchas_path,
            repo_root=repo_root,
            max_tokens=max_tokens,
            model=model,
            recognized_modules=recognized_modules,
            workflow_kinds=workflow_kinds,
        )
        brief["preflight_status"] = "generated"
        return brief
    except Exception as exc:
        if strict:
            raise
        error = sanitize_preflight_error(exc)
        brief = generate_fallback_brief(
            issue, surfaces_yaml, gotchas_path=gotchas_path, workflow_kinds=workflow_kinds
        )
        brief["preflight_error"] = error
        if isinstance(exc, BriefQualityError):
            brief["fallback_reason"] = "semantic_quality"
        elif isinstance(exc, BriefResponseError):
            brief["fallback_reason"] = "llm_response_invalid"
        else:
            brief["fallback_reason"] = "preflight_llm_unavailable"
        if not has_llm_auth():
            print(
                "::notice::Preflight LLM authentication is not configured; "
                "using deterministic fallback.",
                file=sys.stderr,
            )
        else:
            print(f"::warning::Preflight LLM failed; using fallback: {error}", file=sys.stderr)
        return brief


def _resolve_mode_payload(args: argparse.Namespace, root: Path) -> tuple[str, str, int | None]:
    if args.issue is not None:
        return "issue", f"issue:{args.issue}", args.issue
    if args.task is not None:
        return "task", f"task:{_short_task_hash(args.task)}", None

    issue_number = issue_number_from_branch(get_current_branch(root))
    if issue_number is None:
        return "auto", "auto:general", None
    return "auto", f"issue:{issue_number}", issue_number
