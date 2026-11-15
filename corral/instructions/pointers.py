"""Repository pointer planning with preservation of all unmanaged bytes."""
from __future__ import annotations

import difflib
from pathlib import Path

from . import fsio
from .markers import MarkerError, body_digest, normalize, parse_block, render_block
from .sync import PlannedWrite, TargetResult
from .templates import render_pointer


def safe_path(root: Path, relative: str) -> Path:
    try:
        path = (root / relative).resolve()
    except RuntimeError as exc:
        raise ValueError('path contains a symlink loop') from exc
    if not path.is_relative_to(root):
        raise ValueError('path resolves outside the repository')
    return path


def validate_canonical(root, cfg):
    try:
        canonical = safe_path(root, cfg.canonical)
        if not canonical.is_file():
            return TargetResult('pointer', None, cfg.canonical, state='canonical-missing', severity='error', detail=f'canonical file {cfg.canonical} does not exist; create it (move shared rules there from CLAUDE.md if they live there) and re-run')
    except (OSError, ValueError):
        return TargetResult('pointer', None, cfg.canonical, state='unsafe-path', severity='error', detail='canonical path resolves outside the repository or is unreadable')
    return None


def insertion_offset(text):
    lines = text.splitlines(keepends=True)
    if lines and lines[0].rstrip('\r\n') == '---':
        offset = len(lines[0])
        for line in lines[1:]:
            offset += len(line)
            if line.rstrip('\r\n') == '---':
                return offset
    return 0


def unified_diff(path, old, new, *, missing=False):
    return ''.join(difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                       fromfile='/dev/null' if missing else 'a/' + path, tofile='b/' + path, n=3))


def evaluate_pointer(root, spec, cfg, options):
    target = TargetResult('pointer', spec.executor, spec.path)
    try:
        path = safe_path(root, spec.path)
        if path == safe_path(root, cfg.canonical):
            raise ValueError('pointer resolves to the canonical file')
        target.resolved_path = path.relative_to(root).as_posix() if path != root / spec.path else None
        missing = not path.exists()
        source = fsio.read_text_preserving(path)
        block = parse_block(source.text, 'pointer')
    except UnicodeError:
        target.state, target.severity, target.detail = 'decode-error', 'error', 'file is not valid UTF-8'
        return [target], None
    except MarkerError as exc:
        target.state, target.severity, target.detail = 'marker-error', 'error', str(exc)
        return [target], None
    except (OSError, ValueError):
        target.state, target.severity, target.detail = 'unsafe-path', 'error', 'path resolves outside the repository, is unreadable, or aliases the canonical file'
        return [target], None
    expected = render_block('pointer', render_pointer(spec, cfg.canonical, root)).replace('\n', source.newline)
    outside = source.text if block is None else source.text[:block.start] + source.text[block.end:]
    results = [target]
    if block is None:
        target.state = 'missing' if missing else 'block-missing'
        target.detail = 'pointer file does not exist' if missing else 'no corral pointer block'
        insert = insertion_offset(source.text)
        prefix = source.text[:insert]
        if prefix and not prefix.endswith('\n'):
            prefix += source.newline
        new = prefix + (source.newline if prefix else '') + expected + (source.newline if source.text[insert:] else '') + source.text[insert:]
    else:
        new = source.text[:block.start] + expected + source.text[block.end:]
        if block.digest != body_digest(block.body):
            target.state, target.detail = 'block-edited', 'managed block was edited; use --force to replace it'
        elif normalize(source.text[block.start:block.end]) != normalize(expected):
            target.state, target.detail = 'stale', 'managed block differs from the current template'
        else:
            new = source.text
    if outside.strip() and cfg.foreign_content != 'allow':
        count = sum(bool(line.strip()) for line in outside.splitlines())
        # Content-policy errors fail with 1; safety and structural failures use 2.
        severity = 'warning' if cfg.foreign_content == 'warn' else 'error'
        results.append(TargetResult('pointer', spec.executor, spec.path, state='foreign-content', severity=severity,
                                    detail=f'{count} non-blank lines outside the pointer block'))
    if target.state == 'ok':
        return results, None
    target.severity = 'drift'
    if target.state == 'block-edited' and not options.force:
        target.action = 'skip' if not (options.check or options.dry_run) else 'none'
        return results, None
    if options.dry_run:
        target.diff = unified_diff(spec.path, source.text, new, missing=missing)
    data = source.encode(new)
    def apply():
        if safe_path(root, spec.path) != path:
            raise fsio.ChangedDuringSync('changed during sync; re-run')
        fsio.write_bytes_atomic(path, data, expected_old=None if missing else source.raw)
    return results, PlannedWrite(target, apply, 'create' if missing else 'write')


def plan_pointer(root, spec, sync_cfg, options):
    return evaluate_pointer(root, spec, sync_cfg, options)[1]
