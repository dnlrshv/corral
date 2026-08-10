"""CLI implementation for ``corral preflight``."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from corral.config import load_config
from corral.preflight import auth, quota
from corral.preflight.brief import (
    _generate_brief_with_fallback,
    _resolve_mode_payload,
    build_task_issue,
    compute_fingerprint,
    fetch_issue,
    format_brief_output,
    generate_general_brief,
    get_git_head_sha,
    load_surfaces_yaml,
    output_has_fingerprint,
)
from corral.preflight.brief_validation import recognized_modules_from_code_map


def add_preflight_arguments(parser: argparse.ArgumentParser) -> None:
    """Register the preflight flags on *parser* (shared with ``corral.cli``)."""
    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument("--issue", type=int, help="GitHub issue number")
    mode_group.add_argument("--task", type=str, help="Freeform interactive task summary")
    mode_group.add_argument(
        "--auto",
        action="store_true",
        help="Resolve issue from current branch, otherwise emit a deterministic general brief",
    )
    parser.add_argument(
        "--code-map",
        type=Path,
        default=None,
        help="Directory holding the code-map artifacts "
        "(default: <root>/<codemap.output_dir> from corral.yaml)",
    )
    parser.add_argument(
        "--surfaces",
        type=Path,
        default=None,
        help="Path to the surfaces registry (default: <root>/<hooks.surfaces>)",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=None,
        help="Maximum output tokens for the LLM call (default: preflight.max_tokens)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Write brief to this file (default: preflight.output from corral.yaml, else stdout)",
    )
    parser.add_argument(
        "--fallback-on-error",
        action="store_true",
        help="Deprecated no-op: deterministic fallback is the default when the LLM is unavailable",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Fail instead of emitting deterministic fallback when the LLM is unavailable",
    )


def run(args: argparse.Namespace) -> int:
    """Generate a preflight brief. See ``corral preflight --help``.

    Auth uses ``ANTHROPIC_API_KEY``, ``ANTHROPIC_AUTH_TOKEN``, or
    ``CLAUDE_CODE_OAUTH_TOKEN`` (in that precedence order). Pick exactly one
    mode: ``--issue N``, ``--task TEXT``, or ``--auto`` (branch-based).
    Without the ``corral[preflight]`` extra — or with no auth configured —
    the command still succeeds with a deterministic code-map fallback unless
    ``--strict`` is given.
    """
    cfg = load_config(getattr(args, "config", None))
    root = Path(args.root) if getattr(args, "root", None) is not None else cfg.root
    auth._load_dotenv_once()

    if args.fallback_on_error:
        auth.warn_deprecated_fallback_on_error()

    mode, payload, issue_number = _resolve_mode_payload(args, root)
    fingerprint = compute_fingerprint(mode, payload, get_git_head_sha(root))

    output_path = (
        Path(args.output)
        if args.output is not None
        else (root / cfg.preflight.output if cfg.preflight.output else None)
    )
    quota_file = (
        root / cfg.preflight.quota_status_file if cfg.preflight.quota_status_file else None
    )
    if output_path is not None and output_has_fingerprint(output_path, fingerprint):
        quota.refresh_cached_quota_status(
            output_path, fingerprint, format_brief_output, quota_file
        )
        return 0

    surfaces_path = (
        Path(args.surfaces) if args.surfaces is not None else root / cfg.hooks.surfaces
    )
    try:
        surfaces_yaml = load_surfaces_yaml(surfaces_path)
    except OSError as exc:
        print(f"error: cannot read surfaces registry: {exc}", file=sys.stderr)
        return 1

    code_map_dir = (
        Path(args.code_map)
        if args.code_map is not None
        else root / cfg.codemap.output_dir
    )
    gotchas_path = root / cfg.preflight.gotchas
    max_tokens = args.max_tokens if args.max_tokens is not None else cfg.preflight.max_tokens
    recognized_modules = (
        frozenset(cfg.preflight.recognized_modules)
        if cfg.preflight.recognized_modules is not None
        else recognized_modules_from_code_map(code_map_dir)
    )
    workflow_kinds = cfg.preflight.workflow_kinds
    common = dict(gotchas_path=gotchas_path, workflow_kinds=workflow_kinds)

    if issue_number is not None:
        try:
            issue = fetch_issue(issue_number)
        except Exception as exc:
            print(
                f"error: failed to fetch issue #{issue_number}: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            return 1
        brief = _generate_brief_with_fallback(
            issue,
            surfaces_yaml,
            max_tokens=max_tokens,
            strict=args.strict,
            repo_root=root,
            model=cfg.preflight.model,
            recognized_modules=recognized_modules,
            **common,
        )
    elif args.task is not None:
        issue = build_task_issue(args.task)
        brief = _generate_brief_with_fallback(
            issue,
            surfaces_yaml,
            max_tokens=max_tokens,
            strict=args.strict,
            repo_root=root,
            model=cfg.preflight.model,
            recognized_modules=recognized_modules,
            **common,
        )
    else:
        brief = generate_general_brief(surfaces_yaml, **common)
        brief["preflight_status"] = "general"

    quota.refresh_quota_status(brief, quota_file)
    output_text = format_brief_output(brief, fingerprint)

    if output_path is not None:
        output_path.write_text(output_text)
    else:
        sys.stdout.write(output_text)

    return 0
