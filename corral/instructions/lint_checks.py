"""Check registry and deterministic content checks."""
from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
import re
from typing import Callable

import yaml

from .config import CheckConfig, LintConfig, SEVERITIES, fail, strings
from .text_model import TextModel


@dataclass(frozen=True)
class CheckSpec:
    slug: str
    description: str
    run: Callable
    validate_options: Callable
    severity: str = 'warning'
    generated_severity: str = 'error'
    needs_root: bool = False


@dataclass
class CheckContext:
    path: str
    kind: str
    config: LintConfig
    root: Path | None = None
    input_errors: list[str] | None = None

    def finding(self, doc, slug, start, end, message, severity=None):
        from .lint import LintFinding
        spec = CHECKS[slug]
        cfg = self.config.checks.get(slug, CheckConfig())
        default = spec.generated_severity if self.kind == 'generated_rule' else spec.severity
        severity = cfg.severity_by_kind.get(self.kind, cfg.severity or severity or default)
        line, column = doc.position(start)
        excerpt = ' '.join(doc.original[start:end].split())
        if len(excerpt) > 120:
            excerpt = excerpt[:119] + '…'
        return LintFinding(slug, severity, self.path, line, column, message, excerpt)


def no_options(options):
    if options:
        raise ValueError('unsupported options: ' + ', '.join(map(str, options)))
    return {}


def validate_double_bang_options(options):
    return no_options(options)


def validate_retired_model_options(options):
    key = 'lint.checks.retired-model-id'
    defaults = dict(severity_retired='error', severity_deprecated='warning', extra=[], allow=[])
    for name in options.keys() - defaults.keys():
        fail(key + '.' + str(name), 'is an unknown key')
    result = {**defaults, **options}
    for name in ('severity_retired', 'severity_deprecated'):
        if result[name] not in SEVERITIES:
            fail(key + '.' + name, 'must be info, warning or error')
    strings(result['allow'], key + '.allow')
    if not isinstance(result['extra'], list):
        fail(key + '.extra', 'must be a list')
    patterns = list(result['allow'])
    for entry in result['extra']:
        if not isinstance(entry, dict) or set(entry) != {'id', 'status', 'replacement', 'pattern'} or not all(isinstance(v, str) for v in entry.values()):
            fail(key + '.extra', 'entries must contain id, status, replacement and pattern strings')
        if entry['status'] not in ('retired', 'deprecated'):
            fail(key + '.extra.status', 'must be retired or deprecated')
        patterns.append(entry['pattern'])
    for pattern in patterns:
        try:
            re.compile(pattern)
        except re.error as exc:
            fail(key, f'contains invalid regex: {exc}')
    return result


MODEL_TABLE = yaml.safe_load(files('corral.instructions').joinpath('data/model_ids.yaml').read_text(encoding='utf-8'))
MODEL_PATTERNS = [(entry, re.compile(entry['pattern'])) for entry in MODEL_TABLE['entries']]
DOUBLE_BANG = re.compile(r'(?<!!)!!(?![!A-Za-z])')


def check_double_bang(doc: TextModel, ctx: CheckContext):
    out = []
    admonitions = {i for i, line in enumerate(doc.prose.splitlines(), 1) if line.strip().startswith('!!!')}
    for match in DOUBLE_BANG.finditer(doc.prose):
        if doc.position(match.start())[0] in admonitions:
            continue
        out.append(ctx.finding(doc, 'double-bang', *match.span(), '"!!" emphasis; say it once at normal volume'))
    return out


def check_retired_model_id(doc: TextModel, ctx: CheckContext):
    opts = ctx.config.checks['retired-model-id'].options
    opts = validate_retired_model_options(opts)
    text = doc.original
    if doc.frontmatter_end:
        chars = list(text)
        for i in range(doc.frontmatter_end):
            if chars[i] != '\n':
                chars[i] = ' '
        # Keep description/model scalars and their indented continuations only.
        offset, keep = 0, False
        for line in text[:doc.frontmatter_end].splitlines(keepends=True):
            if re.match(r'^[A-Za-z_][\w-]*:', line):
                keep = bool(re.match(r'^(description|model):', line))
            if keep:
                chars[offset:offset + len(line)] = line
            offset += len(line)
        text = ''.join(chars)
    allows = [m.span() for p in opts['allow'] for m in re.finditer(p, text)]
    patterns = MODEL_PATTERNS + [(entry, re.compile(entry['pattern'])) for entry in opts['extra']]
    out = []
    for entry, pattern in patterns:
        for match in pattern.finditer(text):
            if any(a <= match.start() and match.end() <= b for a, b in allows):
                continue
            status = entry['status']
            message = f"{match[0]} is {status} (table as of {MODEL_TABLE['as_of']}); use {entry['replacement']}, or remove the pin if the text does not need a specific model"
            out.append(ctx.finding(doc, 'retired-model-id', *match.span(), message, opts['severity_' + status]))
    return out


CHECKS = {spec.slug: spec for spec in [
    CheckSpec('double-bang', 'Repeated exclamation emphasis in prose', check_double_bang, validate_double_bang_options),
    CheckSpec('retired-model-id', 'Retired or deprecated model pins', check_retired_model_id, validate_retired_model_options, 'error', 'error'),
    CheckSpec('unknown-suppression', 'Unknown check in a suppression', lambda doc, ctx: [], no_options, 'warning', 'warning'),
    CheckSpec('unused-suppression', 'Suppression that removes no findings', lambda doc, ctx: [], no_options, 'info', 'info'),
]}
