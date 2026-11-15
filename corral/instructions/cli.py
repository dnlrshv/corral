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
            hidden = len(report.findings) - len(shown)
            print(f"instructions lint: {counts['error']} errors, {counts['warning']} warnings, {counts['info']} info shown ({hidden} hidden, {report.suppressed} suppressed) in {report.files_checked} files")
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
