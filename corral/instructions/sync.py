"""Evaluate all instruction targets before applying safe planned changes."""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable


@dataclass
class TargetResult:
    kind: str
    executor: str | None
    path: str
    resolved_path: str | None = None
    state: str = 'ok'
    severity: str = 'ok'
    action: str = 'none'
    detail: str = ''
    diff: str | None = None


@dataclass
class PlannedWrite:
    target: TargetResult
    apply: Callable[[], None]
    action: str


@dataclass
class SyncOptions:
    check: bool = False
    dry_run: bool = False
    scope: str = 'repo'
    only: set[str] | None = None
    force: bool = False
    prune: bool = False
    strict: bool = False


@dataclass
class SyncReport:
    mode: str
    scope: str
    canonical: str
    targets: list[TargetResult] = field(default_factory=list)
    strict: bool = False

    def exit_code(self) -> int:
        if any(t.severity == 'error' and t.state != 'foreign-content' for t in self.targets):
            return 2
        return int(any(t.severity in ('drift', 'error') or (self.strict and t.severity == 'warning') for t in self.targets))

    def to_json(self) -> dict:
        counts = Counter(t.severity for t in self.targets)
        return dict(schema_version=1, command='instructions sync', mode=self.mode, scope=self.scope,
                    canonical=self.canonical, exit_code=self.exit_code(), targets=[asdict(t) for t in self.targets],
                    summary={s: counts[s] for s in ('ok', 'drift', 'warning', 'error')})

    def render_text(self) -> str:
        lines = []
        if self.mode == 'dry-run':
            lines.extend(t.diff.rstrip('\n') for t in self.targets if t.diff)
        for target in self.targets:
            if target.severity == 'ok' and target.action == 'none' and not target.detail.startswith('info:'):
                continue
            if self.mode == 'write':
                label = {'create': 'created', 'write': 'wrote', 'remove': 'removed'}.get(target.action, 'error' if target.severity == 'error' else 'skipped')
            else:
                label = target.severity
            if target.detail.startswith('info:'):
                label = 'info'
            lines.append(f'{label:<8} {target.path:<32} {target.state}: {target.detail}')
        if not lines:
            return 'Instructions sync: clean.'
        counts = self.to_json()['summary']
        lines.append('Instructions sync: ' + ', '.join(f'{counts[s]} {s}' for s in ('drift', 'warning', 'error', 'ok')) + '.')
        return '\n'.join(lines)


def run_sync(root: Path, config, options: SyncOptions) -> SyncReport:
    from .pointers import evaluate_pointer, validate_canonical
    root = root.resolve()
    mode = 'check' if options.check else 'dry-run' if options.dry_run else 'write'
    report = SyncReport(mode, options.scope, config.canonical, strict=options.strict)
    if options.scope != 'repo' or (options.only and options.only - {'pointers'}):
        raise ValueError('requested instructions sync scope or target is not available yet')
    canonical_error = validate_canonical(root, config)
    if canonical_error:
        report.targets.append(canonical_error)
        return report
    planned = []
    for spec in sorted(config.pointers if options.only is None or 'pointers' in options.only else [], key=lambda spec: spec.path):
        targets, plan = evaluate_pointer(root, spec, config, options)
        report.targets.extend(targets)
        if plan:
            planned.append(plan)
    if mode == 'write':
        for plan in planned:
            try:
                plan.apply()
            except (OSError, ValueError) as exc:
                plan.target.severity = 'error'
                plan.target.action = 'skip'
                plan.target.detail = str(exc).replace(str(root) + '/', '')
            else:
                plan.target.action = plan.action
                plan.target.severity = 'ok'
    return report
