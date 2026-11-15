"""Public deterministic lint API.

Suppression comments are honoured by lint_text, including generated rules.
Callers linting untrusted generated rules should strip comments first so the
producer cannot suppress its own findings.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
import os
import re
from typing import Literal, Sequence

from corral import __version__
from corral.governance.proposals import path_matches as _path_matches
from .config import LintConfig, validate_lint_config
from .text_model import TextModel
from . import lint_refs

Severity = Literal['error', 'warning', 'info']
FileKind = Literal['instruction', 'skill', 'generated_rule']
SEVERITY_ORDER = {'info': 0, 'warning': 1, 'error': 2}


@dataclass(frozen=True)
class LintFinding:
    check: str
    severity: Severity
    path: str
    line: int
    column: int
    message: str
    excerpt: str


@dataclass
class LintReport:
    files_checked: int
    findings: list[LintFinding]
    suppressed: int
    input_errors: list[str] = field(default_factory=list)
    invalid_input: bool = False

    def counts(self) -> dict[str, int]:
        return {s: sum(f.severity == s for f in self.findings) for s in ('error', 'warning', 'info')}

    def exit_code(self, *, strict: bool) -> int:
        if self.invalid_input:
            return 2
        return int(any(SEVERITY_ORDER[f.severity] >= (1 if strict else 2) for f in self.findings))

    def to_json(self, *, min_severity: Severity = 'warning') -> dict:
        return dict(schema_version=1, tool='corral instructions lint', corral_version=__version__,
                    files_checked=self.files_checked, min_severity=min_severity,
                    summary={**self.counts(), 'suppressed': self.suppressed},
                    findings=[asdict(f) for f in self.findings if SEVERITY_ORDER[f.severity] >= SEVERITY_ORDER[min_severity]],
                    input_errors=self.input_errors, exit_code=self.exit_code(strict=False))


def default_lint_config() -> LintConfig:
    return LintConfig()


def kind_for_path(path: str) -> FileKind:
    return 'skill' if Path(path).name == 'SKILL.md' else 'instruction'


def path_matches(path: str, pattern: str) -> bool:
    # Governance's directory-prefix shortcut treats a leading ** literally.
    # Apply its matcher to each suffix for recursive directory exclusions.
    return _path_matches(path, pattern) or (
        pattern.startswith('**/') and any(
            _path_matches('/'.join(path.split('/')[i:]), pattern[3:])
            for i in range(len(path.split('/')))
        )
    )


def configured(path: str, config: LintConfig) -> bool:
    include = config.include + config.extend_include + (config.governance_globs if config.use_governance_globs else [])
    return any(path_matches(path, p) for p in include) and not excluded(path, config)


def excluded(path: str, config: LintConfig) -> bool:
    return any(path_matches(path, p) for p in config.exclude)


def text_path(path: Path) -> bool:
    return path.suffix.lower() in ('', '.md', '.mdc', '.markdown', '.txt')


def _discover(config: LintConfig, root: Path, errors: list[str]) -> list[Path]:
    candidates = []
    if lint_refs.is_git(root, errors):
        result = lint_refs.git(root, ['ls-files', '-z', '--cached', '--others', '--exclude-standard'], errors=errors)
        if result is not None and result.returncode == 0:
            candidates = [root / os.fsdecode(p) for p in result.stdout.split(b'\0') if p]
    else:
        for directory, dirs, names in os.walk(root, followlinks=False):
            dirs[:] = [d for d in dirs if not (Path(directory) / d).is_symlink()]
            candidates.extend(Path(directory) / name for name in names)
    return sorted({p for p in candidates if text_path(p) and configured(p.relative_to(root).as_posix(), config)
                   and not lint_refs.has_symlink(p, root) and p.is_file()})


def discover_files(config: LintConfig, root: Path) -> list[Path]:
    validate_lint_config(config)
    errors = []
    return [p for p in _discover(config, root.absolute(), errors) if _read(p, p.relative_to(root.absolute()).as_posix(), config, errors) is not None]


def _read(path, display, config, errors):
    try:
        if path.stat().st_size > config.max_file_bytes:
            errors.append(f'{display}: skipped, larger than {config.max_file_bytes} bytes')
            return None
        text = path.read_bytes().decode('utf-8', errors='replace')
    except OSError:
        errors.append(f'{display}: skipped, unreadable file')
        return None
    if text and text.count('\ufffd') / len(text) > .01:
        errors.append(f'{display}: skipped, not valid UTF-8')
        return None
    return text


SUPPRESSION = re.compile(r'corral-lint:\s*(disable-next-line|disable-line|disable-file)\s*=\s*([a-z][a-z0-9-]*(?:\s*,\s*[a-z][a-z0-9-]*)*)')


def _lint(text, *, path, kind, config, root=None, errors=None):
    from .lint_checks import CHECKS, CheckContext
    doc = TextModel(text, frontmatter=kind != 'generated_rule')
    ctx = CheckContext(path, kind, config, root, errors)
    findings = []
    for slug, spec in CHECKS.items():
        cfg = config.checks.get(slug)
        if cfg is None or not cfg.enabled or any(path_matches(path, p) for p in cfg.exclude) or (spec.needs_root and root is None):
            continue
        findings.extend(spec.run(doc, ctx))
    # Deduplicate overlaps, retaining the strongest severity.
    dedup = {}
    for f in findings:
        key = (f.check, f.line, f.column)
        if key not in dedup or SEVERITY_ORDER[f.severity] > SEVERITY_ORDER[dedup[key].severity]:
            dedup[key] = f
    findings = list(dedup.values())
    directives = []
    regions = [(a, doc.original[a:b]) for a, b in doc.comments]
    if Path(path).suffix.lower() not in ('.md', '.mdc', '.markdown'):
        regions.extend((m.start(), m[0]) for m in re.finditer(r'^\s*# corral-lint:[^\n]*', doc.original, re.M))
    diagnostics = []
    for offset, region in regions:
        for match in SUPPRESSION.finditer(region):
            start = offset + match.start()
            line, _ = doc.position(start)
            slugs = [s.strip() for s in match[2].split(',')]
            for slug in slugs:
                if slug != 'all' and slug not in CHECKS:
                    diagnostics.append(ctx.finding(doc, 'unknown-suppression', start, offset + match.end(), f'unknown suppression check: {slug}'))
            target = line
            if match[1] == 'disable-next-line':
                target = next((i for i, value in enumerate(doc.original.splitlines(), 1) if i > line and value.strip()), -1)
            directives.append([match[1], target, slugs, start, offset + match.end(), False])
    kept = []
    suppressed_count = 0
    for f in findings:
        suppressed = False
        for d in directives:
            if (d[0] == 'disable-file' or d[1] == f.line) and ('all' in d[2] or f.check in d[2]):
                d[5] = True
                suppressed = True
        if not suppressed:
            kept.append(f)
        else:
            suppressed_count += 1
    for mode, target, slugs, a, b, used in directives:
        if mode != 'disable-file' and not used and kind != 'generated_rule':
            diagnostics.append(ctx.finding(doc, 'unused-suppression', a, b, 'suppression removed no findings'))
    for f in diagnostics:
        cfg = config.checks.get(f.check)
        if cfg and cfg.enabled and not any(path_matches(path, p) for p in cfg.exclude):
            kept.append(f)
    return sorted(kept, key=lambda f: (f.path, f.line, f.column, f.check)), suppressed_count


def lint_text(text: str, *, path: str, kind: FileKind, config: LintConfig, root: Path | None = None) -> list[LintFinding]:
    validate_lint_config(config)
    return _lint(text, path=path, kind=kind, config=config, root=root)[0]


def lint_paths(paths: Sequence[Path], *, config: LintConfig, root: Path) -> LintReport:
    validate_lint_config(config)
    root = root.absolute()
    report = LintReport(0, [], 0)
    candidates = list(paths) if paths else _discover(config, root, report.input_errors)
    expanded = []
    for candidate in candidates:
        candidate = candidate if candidate.is_absolute() else root / candidate
        if not lint_refs.has_symlink(candidate, root) and candidate.is_dir():
            for directory, dirs, names in os.walk(candidate, followlinks=False):
                dirs[:] = [d for d in dirs if not (Path(directory) / d).is_symlink()]
                expanded.extend(Path(directory) / name for name in names)
        else:
            expanded.append(candidate)
    for path in sorted(set(expanded)):
        path = Path(os.path.abspath(path if path.is_absolute() else root / path))
        try:
            display = path.relative_to(root).as_posix()
        except ValueError:
            report.input_errors.append(f'{path.name}: outside root, skipped')
            report.invalid_input = True
            continue
        if excluded(display, config) or not text_path(path):
            continue
        if lint_refs.has_symlink(path, root):
            report.input_errors.append(f'{display}: skipped, symbolic link')
            continue
        if not path.is_file():
            report.input_errors.append(f'{display}: path does not exist or is not a file')
            report.invalid_input = True
            continue
        text = _read(path, display, config, report.input_errors)
        if text is None:
            continue
        findings, suppressed = _lint(text, path=display, kind=kind_for_path(display), config=config, root=root, errors=report.input_errors)
        report.files_checked += 1
        report.findings.extend(findings)
        report.suppressed += suppressed
    report.findings.sort(key=lambda f: (f.path, f.line, f.column, f.check))
    return report
