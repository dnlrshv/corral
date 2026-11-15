"""Read-only filesystem and Git helpers for instruction linting."""
from __future__ import annotations

import os
import json
import re
import shlex
from pathlib import Path
import stat
import subprocess


def git(root: Path, args: list[str], *, input: bytes | None = None, errors: list[str] | None = None):
    try:
        result = subprocess.run(['git', '-C', str(root), *args], input=input,
                                capture_output=True, check=False, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        result = None
    if result is None or (result.returncode not in (0, 1) and args[0] != 'rev-parse'):
        if errors is not None and 'git unavailable; reference checks may be unverifiable' not in errors:
            errors.append('git unavailable; reference checks may be unverifiable')
        return None
    return result


def is_git(root: Path, errors: list[str] | None = None) -> bool:
    result = git(root, ['rev-parse', '--is-inside-work-tree'], errors=errors)
    return result is not None and result.returncode == 0 and result.stdout.strip() == b'true'


def has_symlink(path: Path, root: Path) -> bool:
    """Probe components without traversing a symlink, including dangling links."""
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return True
    current = root
    for part in parts:
        current = current / part
        try:
            if stat.S_ISLNK(os.lstat(current).st_mode):
                return True
        except OSError:
            return False
    return False


# Reference checks appended after the prior slices land.

PATH_EXTENSIONS = ('.md', '.mdc', '.txt', '.py', '.pyi', '.ts', '.tsx', '.js', '.jsx', '.mjs', '.cjs', '.json', '.jsonc', '.yaml', '.yml', '.toml', '.ini', '.cfg', '.sh', '.bash', '.zsh', '.sql', '.html', '.css', '.lock', '.env.example', '.ipynb', '.parquet', '.csv')
SHELL_INFOS = ('', 'bash', 'sh', 'shell', 'zsh', 'console', 'terminal')
CREATION = re.compile(r'\b(?:create|creates|add|adds|write|writes|generate|generates|produce|produces|emit|emits|rename\w* (?:it )?to|move\w* (?:it )?to|output|save|new)\b', re.I)
MAKE_TARGET = re.compile(r'^[A-Za-z0-9_.][A-Za-z0-9_.\-/]*$')


def validate_dead_path_options(options):
    from .config import fail, strings
    if options.keys() - {'check_ignored', 'ignore'}:
        fail('lint.checks.dead-path', 'has unknown options')
    value = options.get('check_ignored', True)
    if not isinstance(value, bool):
        fail('lint.checks.dead-path.check_ignored', 'must be a boolean')
    ignored = strings(options.get('ignore', []), 'lint.checks.dead-path.ignore')
    return {'check_ignored': value, 'ignore': ignored}


def safe_inside(path, root):
    path = Path(os.path.abspath(path))
    return path if path.is_relative_to(root) else None


def reference_bases(path, root):
    if path.startswith('<'):
        return [root]
    relative = Path(path)
    directory = safe_inside(root / relative.parent, root)
    bases = [directory, root] if directory else [root]
    parts = relative.parts
    for index, part in enumerate(parts[:-2]):
        if part == 'skills':
            bases.append(root.joinpath(*parts[:index + 2]))
    return list(dict.fromkeys(bases))


def shell_sources(doc):
    return [span for span in doc.code_spans if not span.fenced or span.info.lower() in SHELL_INFOS]


def path_tokens(doc):
    for m in re.finditer(r'^\s*@(\S+)\s*$', doc.prose, re.M):
        yield 'import', m.start(1), m[1], ''
    for a, b, destination in doc.links:
        yield 'link', a, destination.split('#', 1)[0].split('?', 1)[0], ''
    for span in shell_sources(doc):
        if not span.fenced and not re.search(r'\s', span.text):
            # A code reference is not dead when the instruction asks to create it.
            sentence = next((s for a, b, s in doc.sentences(doc.original) if a <= span.start < b), '')
            prefix = sentence[:sentence.find(span.text)] if span.text in sentence else sentence
            if not CREATION.search(prefix):
                yield 'code', span.start, span.text, ''
        else:
            for line in re.finditer(r'[^\n]+', span.text):
                for segment in re.finditer(r'[^;&|]+', line[0]):
                    prefix = ''
                    for token in re.finditer(r'''"[^"\n]*"|'[^'\n]*'|[^\s]+''', segment[0]):
                        value = token[0]
                        if not re.search(r'(?:^|\s)(?:>|>>|-o|--output|--out|mkdir|touch)(?:\s|$)', prefix):
                            yield 'shell', span.start + line.start() + segment.start() + token.start(), value, prefix
                        prefix += value + ' '


def clean_path(token, source):
    token = token.strip('\'"').rstrip('.,;:)]')
    token = re.sub(r':\d+(?:-\d+)?$', '', token)
    if source in ('code', 'shell'):
        token = re.sub(r'::?[A-Za-z_]\w*$', '', token)
    if not token or token.startswith(('~', '/', '\\')) or re.match(r'^[A-Za-z][A-Za-z0-9+.-]*:', token):
        return None
    if any(c in token for c in '<>{}*?[]$%…') or '...' in token:
        return None
    if source not in ('import', 'link') and not (token.endswith('/') or ('/' in token and token.lower().endswith(PATH_EXTENSIONS))):
        return None
    return token


def resolved_without_symlinks(path, root):
    if has_symlink(path, root):
        return True
    try:
        os.lstat(path)
        return True
    except OSError:
        return False


def check_dead_path(doc, ctx):
    from .lint import path_matches
    opts = validate_dead_path_options(ctx.config.checks['dead-path'].options)
    root = ctx.root.absolute()
    unresolved = []
    bases = reference_bases(ctx.path, root)
    for source, offset, original, prefix in path_tokens(doc):
        token = clean_path(original, source)
        if token is None:
            continue
        paths = [candidate for base in bases if (candidate := safe_inside(base / token, root)) is not None]
        if not paths or any(resolved_without_symlinks(path, root) for path in paths):
            continue
        root_candidate = safe_inside(root / token, root)
        if root_candidate is None:
            continue
        relative = root_candidate.relative_to(root).as_posix()
        if any(path_matches(relative, pattern) for pattern in opts['ignore']):
            continue
        unresolved.append((source, offset, original, relative))
    ignored = set()
    worktree = None
    if unresolved and opts['check_ignored']:
        probe = git(root, ['rev-parse', '--is-inside-work-tree'], errors=ctx.input_errors)
        if probe is None:
            return []
        worktree = probe.returncode == 0 and probe.stdout.strip() == b'true'
    if worktree:
        data = b''.join(os.fsencode(p) + b'\0' for p in sorted({entry[3] for entry in unresolved}))
        result = git(root, ['check-ignore', '--no-index', '--stdin', '-z'], input=data, errors=ctx.input_errors)
        if result is None:
            return []
        if result is not None:
            ignored = {os.fsdecode(p) for p in result.stdout.split(b'\0') if p}
    out = []
    for source, offset, original, relative in unresolved:
        if relative in ignored:
            continue
        severity = 'warning' if ctx.kind == 'generated_rule' or source in ('code', 'shell') else 'error'
        directory = '.' if ctx.path.startswith('<') else Path(ctx.path).parent.as_posix()
        out.append(ctx.finding(doc, 'dead-path', offset, offset + len(original), f'{source} reference "{original}" does not resolve from {directory} or the repository root; fix the path or remove the reference', severity))
    return out


def read_safe(path, root):
    path = safe_inside(path, root)
    if path is None or has_symlink(path, root):
        return None
    try:
        return path.read_text(encoding='utf-8')
    except (OSError, UnicodeError):
        return None


def make_targets(path, root, visited=None):
    visited = set() if visited is None else visited
    path = safe_inside(path, root)
    if path is None:
        return None
    if path in visited:
        return set()
    visited.add(path)
    text = read_safe(path, root)
    if text is None:
        return None
    targets = set()
    for line in text.splitlines():
        if line.startswith('\t'):
            continue
        include = re.match(r'^\s*-?include\s+(.+)', line)
        target = re.match(r'^([^#\n]+?)\s*::?(?!=)', line)
        if include:
            if '$(' in include[1] or '${' in include[1]:
                return None
            try:
                paths = shlex.split(include[1].split('#', 1)[0])
            except ValueError:
                return None
            for name in paths:
                included = make_targets(path.parent / name, root, visited)
                if included is None:
                    return None
                targets.update(included)
        elif target:
            if '$(' in line or '${' in line:
                return None
            if '%' in target[1]:
                continue
            if re.fullmatch(r'[A-Za-z0-9_.\-/ ]+', target[1]):
                targets.update(target[1].split())
            if target[1] == '.PHONY':
                targets.update(line[target.end():].split())
    return targets


def command_segments(doc):
    for span in shell_sources(doc):
        for line in re.finditer(r'[^\n]+', span.text):
            directory = None
            for segment in re.finditer(r'[^;&|]+', line[0]):
                try:
                    lexer = shlex.shlex(segment[0], posix=True)
                    lexer.whitespace_split = True
                    lexer.commenters = ''
                    words, positions = [], []
                    while True:
                        before = lexer.instream.tell()
                        word = lexer.get_token()
                        if word is None:
                            break
                        while before < len(segment[0]) and segment[0][before].isspace():
                            before += 1
                        if before < len(segment[0]) and segment[0][before] in "\"'":
                            before += 1
                        words.append(word)
                        positions.append(before)
                except ValueError:
                    continue
                if not words:
                    continue
                if words[0] == 'cd' and len(words) == 2:
                    directory = words[1]
                else:
                    yield span.start + line.start() + segment.start(), segment[0], words, positions, directory


def check_dead_command(doc, ctx):
    root = ctx.root.absolute()
    out = []
    for offset, segment, words, positions, cd in command_segments(doc):
        executable = words[0]
        command_label = executable
        directory = root / cd if cd else None
        names = []
        manifest = None
        if executable in ('make', 'gmake'):
            filename = None
            cursor = 1
            while cursor < len(words):
                word = words[cursor]
                if word.startswith('-'):
                    option, sep, value = word.partition('=')
                    if option in ('-C', '-f', '--directory', '--file', '-j', '-l', '-I', '-o', '-W'):
                        if not sep:
                            cursor += 1
                            value = words[cursor] if cursor < len(words) else ''
                        if option in ('-C', '--directory'):
                            directory = (directory or root) / value
                        elif option in ('-f', '--file'):
                            filename = value
                    elif word.startswith('-C') and len(word) > 2:
                        directory = (directory or root) / word[2:]
                    elif word.startswith('-f') and len(word) > 2:
                        filename = word[2:]
                elif '=' not in word and MAKE_TARGET.fullmatch(word):
                    names.append((word, positions[cursor]))
                cursor += 1
            directory = directory or root
            if filename:
                manifest = directory / filename
            else:
                manifest = next((directory / name for name in ('GNUmakefile', 'makefile', 'Makefile') if read_safe(directory / name, root) is not None), None)
            defined = make_targets(manifest, root) if manifest else None
        elif executable in ('npm', 'pnpm', 'yarn', 'bun'):
            remaining = []
            cursor = 1
            while cursor < len(words):
                word = words[cursor]
                option, sep, value = word.partition('=')
                if option in ('--prefix', '-C', '--cwd'):
                    if not sep:
                        cursor += 1
                        value = words[cursor] if cursor < len(words) else ''
                    directory = (directory or root) / value
                else:
                    remaining.append((word, positions[cursor]))
                cursor += 1
            if len(remaining) < 2 or remaining[0][0] not in ('run', 'run-script'):
                continue
            command_label = executable + ' ' + remaining[0][0]
            name, name_position = remaining[1]
            if any(c in name for c in '$<{'):
                continue
            names = [(name, name_position)]
            if directory is not None:
                candidates = [directory]
            else:
                parent = reference_bases(ctx.path, root)[0]
                candidates = [p for p in (parent, *parent.parents) if p.is_relative_to(root)]
            manifest = next((d / 'package.json' for d in candidates if read_safe(d / 'package.json', root) is not None), None)
            if manifest is None:
                continue
            try:
                package = json.loads(read_safe(manifest, root))
                scripts = package.get('scripts', {})
                if not isinstance(scripts, dict):
                    continue
                defined = set(scripts)
            except (ValueError, TypeError, AttributeError):
                continue
        else:
            continue
        if defined is None:
            continue
        manifest = safe_inside(manifest, root)
        if manifest is None:
            continue
        for name, name_offset in names:
            if name not in defined:
                start = offset + max(0, name_offset)
                severity = 'warning' if ctx.kind == 'generated_rule' else 'error'
                out.append(ctx.finding(doc, 'dead-command', start, start + len(name), f'"{command_label} {name}" is not defined in {manifest.relative_to(root).as_posix()}; fix the command or remove it', severity))
    return out
