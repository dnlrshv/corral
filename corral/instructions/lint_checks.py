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
from . import lint_refs


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


# Appended to lint_checks.py after the first slice is committed.
REASON_MARKERS = ['because', 'since', 'so that', 'otherwise', 'to avoid', 'to prevent', 'to keep', 'which would', 'as this', 'as it', 'reason:', 'why:']


def validate_caps_options(options):
    defaults = dict(tokens=['MUST NOT', 'DO NOT', 'MUST', 'NEVER', 'ALWAYS', 'CRITICAL', 'IMPORTANT'],
                    booster_labels=['CRITICAL', 'IMPORTANT', 'NOTE', 'WARNING', 'REMEMBER'],
                    reason_markers=REASON_MARKERS, paragraph_threshold=2, file_min_count=5,
                    file_density_per_1000_words=5.0)
    key = 'lint.checks.caps-emphasis'
    for name in options.keys() - defaults.keys():
        fail(key + '.' + str(name), 'is an unknown key')
    result = {**defaults, **options}
    for name in ('tokens', 'booster_labels', 'reason_markers'):
        strings(result[name], key + '.' + name)
        if any(not value.strip() for value in result[name]):
            fail(key + '.' + name, 'must contain non-empty strings')
    for name in ('paragraph_threshold', 'file_min_count', 'file_density_per_1000_words'):
        value = result[name]
        numeric = (int, float) if name == 'file_density_per_1000_words' else (int,)
        if isinstance(value, bool) or not isinstance(value, numeric) or not 0 < value < float('inf'):
            fail(key + '.' + name, 'must be positive and finite')
    return result


def validate_thinking_options(options):
    if options.keys() - {'allow_agent_keywords'}:
        fail('lint.checks.thinking-scaffold', 'has unknown options')
    value = options.get('allow_agent_keywords', True)
    if not isinstance(value, bool):
        fail('lint.checks.thinking-scaffold.allow_agent_keywords', 'must be a boolean')
    return {'allow_agent_keywords': value}


def has_reason(sentence, markers):
    return any(re.search(r'\b' + re.escape(marker) + (r'\b' if marker[-1:].isalnum() else ''), sentence, re.I) for marker in markers)


def reasoned_at(paragraph, offset, markers):
    sentences = list(TextModel.sentences(paragraph))
    for index, (a, b, sentence) in enumerate(sentences):
        if a <= offset < b:
            return has_reason(sentence, markers) or (
                index + 1 < len(sentences) and bool(re.match(r'\s*(?:This is because|The reason|That is because|Otherwise)\b', sentences[index + 1][2], re.I))
            )
    return False


def anchor_view(doc, ctx):
    text = list(doc.prose)
    for anchor in ctx.config.registered_anchors.get(ctx.path, ()):
        start = 0
        while anchor and (start := doc.original.find(anchor, start)) != -1:
            for index in range(start, start + len(anchor)):
                if text[index] != '\n':
                    text[index] = ' '
            start += len(anchor)
    return ''.join(text)


def check_caps_emphasis(doc, ctx):
    if re.search(r'interpreted as described in (RFC ?2119|BCP ?14)', doc.prose, re.I):
        return []
    opts = validate_caps_options(ctx.config.checks['caps-emphasis'].options)
    token_pattern = re.compile(r'(?<!\w)(?:\*\*|__)?(?:' + '|'.join(re.escape(t) for t in sorted(opts['tokens'], key=len, reverse=True)) + r')(?:\*\*|__)?(?!\w)') if opts['tokens'] else re.compile(r'(?!)')
    boosters = re.compile(r'^\s*(?:[-*+] |\d+[.)] |#{1,6} )?\s*(?:\*\*|__)?(' + '|'.join(map(re.escape, opts['booster_labels'])) + r')(?:\*\*|__)?(?::| -| —|!)') if opts['booster_labels'] else re.compile(r'(?!)')
    view = anchor_view(doc, ctx)
    paragraphs = list(doc.paragraphs(view))
    out, occurrences = [], []
    for pindex, (start, end, paragraph) in enumerate(paragraphs):
        matches = [m for m in token_pattern.finditer(paragraph) if not reasoned_at(paragraph, m.start(), opts['reason_markers'])]
        occurrence_start = len(occurrences)
        occurrences.extend((start + m.start(), start + m.end(), m[0]) for m in matches)
        booster = boosters.match(paragraph)
        if booster and not reasoned_at(paragraph, booster.start(1), opts['reason_markers']):
            out.append(ctx.finding(doc, 'caps-emphasis', start + booster.start(1), start + booster.end(1), f'"{booster[1]}:" booster label; state the constraint at normal volume, with its reason'))
        heading = re.match(r'^\s*#{1,6}\s+(?:\*\*|__)?(' + '|'.join(map(re.escape, opts['tokens'])) + r')(?:\*\*|__)?\s*$', paragraph) if opts['tokens'] else None
        if heading and ctx.kind != 'generated_rule':
            del occurrences[occurrence_start:]
            matches = []
        if heading and pindex + 1 < len(paragraphs) and not has_reason(paragraphs[pindex + 1][2], opts['reason_markers']):
            out.append(ctx.finding(doc, 'caps-emphasis', start + heading.start(1), start + heading.end(1), f'"{heading[1]}:" booster label; state the constraint at normal volume, with its reason'))
        if ctx.kind == 'generated_rule':
            for m in matches:
                out.append(ctx.finding(doc, 'caps-emphasis', start + m.start(), start + m.end(), 'unreasoned emphasis word; state the constraint at normal volume, with its reason'))
        elif len(matches) >= opts['paragraph_threshold']:
            m = matches[0]
            tokens = ', '.join(m[0].strip('*_') for m in matches)
            out.append(ctx.finding(doc, 'caps-emphasis', start + m.start(), start + m.end(), f'{len(matches)} unreasoned emphasis words in one paragraph ({tokens}); keep the one or two real constraints and give each its reason'))
    words = len(re.findall(r"[A-Za-z][A-Za-z'’-]*", view))
    density = 1000 * len(occurrences) / max(1, words)
    if ctx.kind != 'generated_rule' and len(occurrences) >= opts['file_min_count'] and density >= opts['file_density_per_1000_words']:
        a, b, _ = occurrences[0]
        out.append(ctx.finding(doc, 'caps-emphasis', a, b, f'{len(occurrences)} unreasoned emphasis words ({density:.1f} per 1,000 words); when everything is marked critical the markers stop carrying information', 'info'))
    return out


THINKING = [re.compile(p, re.I) for p in [
    r'\bthink(?:ing)? step[- ]by[- ]step\b', r'\btake a deep breath\b',
    r"\b(?:do not|don'?t|never) (?:over-?think|think)\b", r'\bthink (?:less|more carefully|longer)\b',
    r'\bshow your (?:thinking|reasoning|chain of thought)\b',
]]
THINK_TAG = re.compile(r'<\s*/?\s*(?:scratchpad|thinking|brainstorm|inner_monologue)\s*>', re.I)
AGENT_KEYWORDS = re.compile(r'\b(?:think harder|think hard|ultrathink)\b', re.I)
WORD_CAP = re.compile(r'\b(?:at most|no more than|not more than|under|fewer than|less than|a maximum of|max(?:imum)?\.?|up to|limit(?:ed)? to|keep (?:it|this|them|responses?|replies|answers?|summar(?:y|ies)|output) (?:to|under|below|within))\s+(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+(?:words?|sentences?|paragraphs?|bullets?|bullet points?)\b', re.I)
HISTORY = [
    ('dated-state', re.compile(r'\bas of (?:\d{4}-\d{2}-\d{2}|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.? \d{1,2},? \d{4}|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]* \d{4}|q[1-4] \d{4})\b', re.I)),
    ('issue-ref', re.compile(r'(?<![\w/&#])#\d{2,6}\b|\b(?:PR|pull request|issue)\s*#?\d{2,6}\b', re.I)),
    ('dated-tag', re.compile(r'\((?:[^()\n]{0,40}\b)?\d{4}-\d{2}-\d{2}\)')),
    ('incident-narration', re.compile(r'\b(?:after|since|following) (?:a|an|the|one|that) [^.\n]{0,60}\b(?:incident|outage|regression|ejection|failure|postmortem)\b|\b(?:last|previous) (?:week|month|quarter|time)\b', re.I)),
]
MIGRATION = [(re.compile(p, re.I), severity) for p, severity in [
    (r'\bno longer\b(?!\s+than\b)', 'warning'),
    (r'\b(?:any ?more)\b', 'warning'),
    (r'\bnow (?:works|behaves|means|counts|lives|defaults|requires|uses)\b(?: differently)?', 'warning'),
    (r'\balso counts\b', 'warning'), (r'\b(?:used to|previously)\b', 'info'),
    (r'\b(?:was|were|has been|have been) (?:renamed|replaced|removed|moved|deprecated)\b', 'info'),
]]
GRADER = [re.compile(p, re.I) for p in [
    r"\b(?:you will be|you are being|you're being|this (?:job|task|run|retrospective|review) is(?: not)?) (?:graded|scored|judged|evaluated|rated)\b",
    r'\bhidden tests?\b', r'\b(?:graded|scored|judged) (?:on|by)\b',
]]


def check_thinking_scaffold(doc, ctx):
    matches = [m for pattern in THINKING for m in pattern.finditer(doc.prose)]
    if ctx.kind == 'generated_rule' or not validate_thinking_options(ctx.config.checks['thinking-scaffold'].options)['allow_agent_keywords']:
        matches.extend(AGENT_KEYWORDS.finditer(doc.prose))
    matches.extend(m for m in THINK_TAG.finditer(doc.nonfenced) if not any(a <= m.start() < b for a, b in doc.comments))
    return [ctx.finding(doc, 'thinking-scaffold', *m.span(), f'"{m[0]}" steers thinking through prose; on thinking models depth is set by effort in configuration') for m in matches]


def check_word_cap(doc, ctx):
    return [ctx.finding(doc, 'word-cap', *m.span(), f'numeric output cap "{m[0]}"; state the audience and outcome instead ("answers only what was asked"), keeping any format requirement as a format rule') for m in WORD_CAP.finditer(doc.prose)]


def check_history_narrative(doc, ctx):
    view = anchor_view(doc, ctx)
    out = []
    for subid, pattern in HISTORY:
        for m in pattern.finditer(view):
            if subid == 'dated-state':
                message = f'time-stamped state "{m[0]}" goes stale; state the current rule, or move the snapshot into a dated note outside the instruction file'
            else:
                message = f'history narrative ({subid}) "{m[0]}"; a rule\'s authority is the behaviour it prescribes, not the incident behind it'
            severity = 'error' if ctx.kind == 'generated_rule' else 'warning' if subid == 'dated-state' else 'info'
            out.append(ctx.finding(doc, 'history-narrative', *m.span(), message, severity))
    return out


def check_migration_relative(doc, ctx):
    view = anchor_view(doc, ctx)
    out = []
    for pattern, severity in MIGRATION:
        for m in pattern.finditer(view):
            if m[0].lower() in ('any more', 'anymore'):
                paragraph_start, _, paragraph = next(((a, b, s) for a, b, s in doc.paragraphs(view) if a <= m.start() < b), (0, 0, ''))
                sentence = next((s for a, b, s in TextModel.sentences(paragraph) if a <= m.start() - paragraph_start < b), '')
                if not re.search(r"\b(?:not|no|never|don'?t|doesn'?t|isn'?t|aren'?t|won'?t|can'?t)\b", sentence, re.I):
                    continue
            out.append(ctx.finding(doc, 'migration-relative', *m.span(), f'migration-relative phrasing "{m[0]}"; write the current rule as if it were the only rule that ever existed', 'error' if ctx.kind == 'generated_rule' else severity))
    return out


def check_grader_vocabulary(doc, ctx):
    return [ctx.finding(doc, 'grader-vocabulary', *m.span(), f'grader vocabulary "{m[0]}"; state the requirement the grader would check instead') for pattern in GRADER for m in pattern.finditer(doc.prose)]


def check_missing_reason(doc, ctx):
    if ctx.kind != 'generated_rule':
        return []
    markers = ctx.config.checks.get('caps-emphasis', CheckConfig()).options.get('reason_markers', REASON_MARKERS)
    if has_reason(doc.prose, markers):
        return []
    start = len(doc.original) - len(doc.original.lstrip())
    return [ctx.finding(doc, 'missing-reason', start, len(doc.original), 'rule has no stated reason; add it after "because" in the same sentence')]


CHECKS.update({spec.slug: spec for spec in [
    CheckSpec('caps-emphasis', 'Unreasoned emphasis clusters and booster labels', check_caps_emphasis, validate_caps_options),
    CheckSpec('thinking-scaffold', 'Thinking directions expressed through prose', check_thinking_scaffold, validate_thinking_options),
    CheckSpec('word-cap', 'Numeric output word caps', check_word_cap, no_options, 'warning', 'warning'),
    CheckSpec('history-narrative', 'Dated state and historical narratives', check_history_narrative, no_options, 'info', 'error'),
    CheckSpec('migration-relative', 'Rules phrased relative to old behaviour', check_migration_relative, no_options),
    CheckSpec('grader-vocabulary', 'Grading rather than requirements', check_grader_vocabulary, no_options),
    CheckSpec('missing-reason', 'Generated rule without its reason', check_missing_reason, no_options, 'warning', 'warning'),
]})


CHECKS.update({spec.slug: spec for spec in [
    CheckSpec("dead-path", "Unresolved local paths", lint_refs.check_dead_path, lint_refs.validate_dead_path_options, "warning", "warning", True),
    CheckSpec("dead-command", "Missing make targets and package scripts", lint_refs.check_dead_command, no_options, "error", "warning", True),
]})
