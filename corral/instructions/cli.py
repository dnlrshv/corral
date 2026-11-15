"""Instruction CLI registration and standalone hook entry points."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
import sys

import yaml

from .lint import SEVERITY_ORDER, configured, lint_paths
from .lint_checks import CHECKS


def add_lint_parser(sub):
    parser = sub.add_parser('lint', help='Lint agent instruction files')
    _lint_flags(parser)
    parser.set_defaults(handler=run_lint)


def _lint_flags(parser):
    parser.add_argument('paths', nargs='*', type=Path)
    parser.add_argument('--root', type=Path)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--strict', action='store_true')
    parser.add_argument('--min-severity', choices=('info', 'warning', 'error'))
    parser.add_argument('--only-configured', action='store_true')
    parser.add_argument('--select', action='append', default=[])
    parser.add_argument('--ignore', action='append', default=[])
    parser.add_argument('--list-checks', action='store_true')


def add_instructions_parser(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser('instructions', help='Sync and lint agent instruction files')
    commands = parser.add_subparsers(dest='instructions_command')
    add_lint_parser(commands)
    add_sync_parser(commands)


def _redact_error(value: str, root: Path | None = None) -> str:
    for prefix in (str(root) if root else '', str(Path.cwd()), str(Path.home())):
        if prefix:
            value = value.replace(prefix + '/', '').replace(prefix, '.')
    # Errors may refer to an explicitly supplied config outside the workspace.
    return re.sub(r"(?<![\w])/[^\s\"'<>]+", lambda m: Path(m[0]).name, value)


def run_lint(args) -> int:
    from corral.config import load_config
    if args.list_checks:
        for slug, spec in sorted(CHECKS.items()):
            print(f'{slug}: instruction/skill={spec.severity}, generated_rule={spec.generated_severity}; {spec.description}')
        return 0
    root = None
    try:
        cfg = load_config(args.config)
        root = (args.root or cfg.root).absolute()
        if root.is_symlink() or not root.is_dir():
            raise ValueError('--root is not a directory')
        lint = cfg.instructions.lint
        lint.governance_globs = cfg.governance.instruction_globs
        lint.registry = cfg.governance.registry
        lint.registry_config = cfg.governance
        select = {s.strip() for value in args.select for s in value.split(',')}
        ignore = {s.strip() for value in args.ignore for s in value.split(',')}
        for slug in sorted(select | ignore):
            if slug not in CHECKS:
                raise ValueError(f'unknown check: {slug}')
        for slug, check in lint.checks.items():
            if (select and slug not in select) or slug in ignore:
                check.enabled = False
        paths = args.paths
        if args.only_configured and paths:
            paths = [p for p in paths if configured(p.relative_to(root).as_posix() if p.is_absolute() and p.is_relative_to(root) else p.as_posix(), lint)]
            if not paths:
                from .lint import LintReport
                report = LintReport(0, [], 0)
            else:
                report = lint_paths(paths, config=lint, root=root)
        else:
            report = lint_paths(paths, config=lint, root=root)
        minimum = args.min_severity or lint.min_severity
        code = report.exit_code(strict=args.strict)
        if args.json:
            output = report.to_json(min_severity=minimum)
            output['exit_code'] = code
            print(json.dumps(output, sort_keys=True))
        else:
            shown = [f for f in report.findings if SEVERITY_ORDER[f.severity] >= SEVERITY_ORDER[minimum]]
            for f in shown:
                print(f'{f.path}:{f.line}:{f.column}: {f.severity} {f.check} {f.message}')
            counts = {s: sum(f.severity == s for f in shown) for s in ('error', 'warning', 'info')}
            totals = report.counts()
            hidden = [f"{totals[s] - counts[s]} {s} hidden" for s in ('warning', 'info') if SEVERITY_ORDER[s] < SEVERITY_ORDER[minimum]]
            details = ', '.join([*hidden, f'{report.suppressed} suppressed'])
            errors = f"{counts['error']} error" + ('s' if counts['error'] != 1 else '')
            warnings = f"{counts['warning']} warning" + ('s' if counts['warning'] != 1 else '')
            files = f'{report.files_checked} file' + ('s' if report.files_checked != 1 else '')
            print(f"instructions lint: {errors}, {warnings}, {counts['info']} info shown ({details}) in {files}")
        for error in report.input_errors:
            print('instructions lint: ' + _redact_error(error, root), file=sys.stderr)
        return code
    except (ValueError, OSError, yaml.YAMLError) as exc:
        print('instructions lint: ' + _redact_error(str(exc), root), file=sys.stderr)
        return 2


def lint_main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog='corral-instructions-lint')
    _lint_flags(parser)
    return run_lint(parser.parse_args(argv))


def _sync_flags(parser):
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--check', action='store_true')
    modes.add_argument('--dry-run', action='store_true')
    parser.add_argument('--scope', choices=('repo', 'user', 'all'), default='repo')
    parser.add_argument('--only', action='append')
    for flag in ('force', 'prune', 'strict', 'json', 'allow-empty-overlay'):
        parser.add_argument('--' + flag, action='store_true')
    parser.add_argument('--overlay', type=Path)
    parser.add_argument('--user-target', action='append', default=[])
    parser.add_argument('--allow-target-repo', type=Path, action='append', default=[])
    parser.add_argument('--root', type=Path)
    parser.add_argument('--config', type=Path)


def add_sync_parser(sub):
    parser = sub.add_parser('sync', help='Synchronize managed agent instruction targets')
    _sync_flags(parser)
    parser.set_defaults(handler=run_sync)


def run_sync(args) -> int:
    from corral.config import load_config
    from .sync import SyncOptions, run_sync as synchronize
    root = None
    try:
        config = load_config(args.config)
        root = (args.root or config.root).absolute()
        if not root.is_dir():
            raise ValueError('--root is not a directory')
        only = {s.strip() for value in args.only for s in value.split(',')} if args.only is not None else None
        if only is not None and only - {'pointers', 'skills', 'user'}:
            raise ValueError('unknown instructions sync target: ' + ', '.join(sorted(only - {'pointers', 'skills', 'user'})))
        options = SyncOptions(check=args.check, dry_run=args.dry_run, scope=args.scope, only=only,
                              force=args.force, prune=args.prune, strict=args.strict)
        report = synchronize(root, config.instructions.sync, options)
        print(json.dumps(report.to_json(), sort_keys=True) if args.json else report.render_text())
        return report.exit_code()
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print('instructions sync: ' + _redact_error(str(exc), root), file=sys.stderr)
        return 2


def sync_main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog='corral-instructions-sync')
    _sync_flags(parser)
    return run_sync(parser.parse_args(argv))
