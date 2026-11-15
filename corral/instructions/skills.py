"""Provenance-backed skill mirroring; unowned files are kept."""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import stat

from . import fsio
from .pointers import safe_path, unified_diff
from .sync import PlannedWrite, TargetResult
from .text_model import TextModel

PROVENANCE = '.corral-mirror.json'


def _paths_overlap(first, second):
    for child, parent in ((first, second), (second, first)):
        if child.is_relative_to(parent):
            return True
        if parent.exists() and any(p.exists() and p.samefile(parent) for p in (child, *child.parents)):
            return True
    return False


def skill_files(path: Path) -> dict[str, tuple[bytes, int]]:
    files = {}
    def unreadable(error):
        raise error
    for directory, dirs, names in os.walk(path, followlinks=False, onerror=unreadable):
        current = Path(directory)
        for name in dirs + names:
            if (current / name).is_symlink():
                raise ValueError('a symlink inside the skill is unsafe')
        for name in names:
            p = current / name
            if '__pycache__' in p.relative_to(path).parts or name in (PROVENANCE, '.DS_Store') or name.endswith('.pyc'):
                continue
            mode = os.lstat(p).st_mode
            if not stat.S_ISREG(mode):
                raise ValueError('non-regular file inside the skill is unsafe')
            files[p.relative_to(path).as_posix()] = (p.read_bytes(), stat.S_IMODE(mode))
    return files


def digest_files(files):
    rows = ''.join(f'{name}\0{hashlib.sha256(data).hexdigest()}\0{bool(mode & 0o111)}\n' for name, (data, mode) in sorted(files.items()))
    return hashlib.sha256(rows.encode('utf-8')).hexdigest()


def skill_digest(path: Path) -> str:
    return digest_files(skill_files(path))


def provenance(path):
    try:
        value = json.loads((path / PROVENANCE).read_bytes())
    except (OSError, ValueError, UnicodeError):
        return None
    if not isinstance(value, dict) or value.get('schema_version') != 1 or value.get('canonical_side') not in ('claude', 'agents') or not isinstance(value.get('canonical_path'), str) or not isinstance(value.get('digest'), str) or not re.fullmatch(r'[0-9a-f]{64}', value['digest']):
        return None
    return value


def discover_skills(root: Path, side: str) -> dict[str, Path]:
    directory = safe_path(root, side)
    if not directory.exists():
        return {}
    found = {}
    for p in sorted(directory.iterdir()):
        if not p.resolve().is_relative_to(root):
            raise ValueError('skill symlink points outside the repository')
        if p.is_dir() and (p / 'SKILL.md').is_file():
            found[p.name] = p
    return found


def evaluate_skills(root, cfg, options):
    """Return evaluated results and deferred plans, before any target is changed."""
    if not cfg.enabled:
        return [], []
    results, plans, sides = [], [], {}
    try:
        real_sides = [safe_path(root, cfg.sides[s]) for s in ('claude', 'agents')]
        if _paths_overlap(real_sides[0], real_sides[1]):
            raise ValueError('skill sides resolve to overlapping directories')
    except (OSError, ValueError):
        return [TargetResult('skill', None, cfg.sides['agents'], state='skill-unsafe', severity='error', detail='skill sides are unsafe or resolve to overlapping directories')], []
    blocked = set()
    unsafe_side = False
    for side, relative in sorted(cfg.sides.items()):
        try:
            directory = safe_path(root, relative)
            found = {}
            if directory.exists():
                for path in sorted(directory.iterdir()):
                    display = relative + '/' + path.name
                    if path.is_symlink():
                        try:
                            valid_link = path.resolve().is_relative_to(root)
                        except (OSError, RuntimeError):
                            valid_link = False
                    else:
                        valid_link = True
                    if not valid_link:
                        results.append(TargetResult('skill', None, display, state='skill-unsafe', severity='error', detail='skill symlink points outside the repository'))
                        blocked.add(path.name)
                        continue
                    if not path.is_dir():
                        continue
                    if (path / 'SKILL.md').is_symlink():
                        blocked.add(path.name)
                        results.append(TargetResult('skill', None, display, state='skill-unsafe', severity='error', detail='SKILL.md is a symlink'))
                        continue
                    if not (path / 'SKILL.md').is_file():
                        results.append(TargetResult('skill', None, display, detail='info: directory has no SKILL.md; ignored'))
                        continue
                    found[path.name] = path
            sides[side] = found
        except (OSError, ValueError):
            results.append(TargetResult('skill', None, relative, state='skill-unsafe', severity='error', detail='skill side is unreadable, outside the repository, or contains a symlink'))
            sides[side] = {}
            unsafe_side = True
    if unsafe_side:
        return results, plans
    # An unsafe skill should not make unrelated skills on that side disappear.
    for name in sorted(set().union(*(set(skills) for skills in sides.values()))):
        if name in blocked:
            continue
        paths = {side: skills[name] for side, skills in sides.items() if name in skills}
        files = {}
        bad = False
        for side, path in paths.items():
            display = cfg.sides[side] + '/' + name
            try:
                files[side] = skill_files(path)
                text = files[side]['SKILL.md'][0].decode('utf-8-sig')
                doc = TextModel(text)
                if isinstance(doc.frontmatter, dict):
                    for key in ('allowed-tools', 'model', 'disable-model-invocation'):
                        if key in doc.frontmatter:
                            results.append(TargetResult('skill', None, display, detail=f'info: Claude-specific frontmatter key {key} is copied unchanged; other agents may ignore it'))
            except UnicodeError:
                results.append(TargetResult('skill', None, display, state='decode-error', severity='error', detail='SKILL.md is not valid UTF-8'))
                bad = True
            except (OSError, ValueError, KeyError):
                results.append(TargetResult('skill', None, display, state='skill-unsafe', severity='error', detail='skill contains a symlink, unreadable file, or unsupported file type'))
                bad = True
        if bad:
            continue
        canonical = cfg.canonical
        digests = {s: digest_files(f) for s, f in files.items()}
        if canonical == 'auto':
            if len(paths) == 1:
                present = next(iter(paths))
                record = provenance(paths[present])
                other = 'agents' if present == 'claude' else 'claude'
                if record and record['canonical_side'] == other and record['canonical_path'] == cfg.sides[other] + '/' + name:
                    canonical = other
                else:
                    canonical = present
            elif len(set(digests.values())) == 1:
                # Preserve provenance direction even when both copies are identical.
                known = []
                for candidate_mirror, candidate_path in paths.items():
                    candidate_record = provenance(candidate_path)
                    other = 'agents' if candidate_mirror == 'claude' else 'claude'
                    if candidate_record and candidate_record['canonical_side'] == other and candidate_record['canonical_path'] == cfg.sides[other] + '/' + name:
                        known.append(other)
                canonical = known[0] if len(known) == 1 else next((s for s in ('claude', 'agents') if s in paths and not paths[s].is_symlink()), 'claude')
            else:
                candidates = []
                for mirror in paths:
                    other = 'agents' if mirror == 'claude' else 'claude'
                    record = provenance(paths[mirror])
                    if record and record['canonical_side'] == other and record['canonical_path'] == cfg.sides[other] + '/' + name and record['digest'] == digests[mirror]:
                        candidates.append(other)
                if len(candidates) != 1:
                    results.append(TargetResult('skill', None, cfg.sides['agents'] + '/' + name, state='skill-divergent', severity='drift', action='skip', detail=f'skill {name} differs on both sides; set skills.canonical or reconcile by hand'))
                    continue
                canonical = candidates[0]
        mirror = 'agents' if canonical == 'claude' else 'claude'
        source = root / cfg.sides[canonical] / name
        destination = root / cfg.sides[mirror] / name
        display = cfg.sides[mirror] + '/' + name
        record = provenance(destination) if mirror in paths else None
        owned = bool(record and record['canonical_side'] == canonical and record['canonical_path'] == cfg.sides[canonical] + '/' + name)
        target = TargetResult('skill', None, display)
        results.append(target)
        if canonical not in paths:
            if owned:
                target.state, target.severity, target.detail = 'skill-orphan', 'drift', 'canonical skill is absent; use --prune to remove this corral mirror'
                if options.prune:
                    _append_plan(plans, target, lambda: _remove_plan(root, destination, target, files[mirror], record, options))
            else:
                target.state, target.severity = 'skill-divergent', 'drift'
                target.detail = f'skill {name} exists only in {mirror}; move it to {canonical} or set skills.canonical: auto'
            continue
        try:
            real_source, real_destination = source.resolve(), destination.resolve()
            same = real_source == real_destination or (real_source.exists() and real_destination.exists() and real_source.samefile(real_destination))
            nested = not same and _paths_overlap(real_source, real_destination)
            alias_replacement = same and cfg.mode == 'symlink' and not destination.is_symlink()
            if nested or alias_replacement:
                raise ValueError('skill paths alias or contain the canonical directory')
        except (OSError, ValueError, RuntimeError):
            target.state, target.severity, target.detail = 'skill-unsafe', 'error', 'skill paths overlap the canonical directory; choose separate sides'
            continue
        source_files = files[canonical]
        mirror_files = files.get(mirror, {})
        extra = sorted(mirror_files.keys() - source_files.keys())
        identical = mirror in paths and digests[canonical] == digests[mirror]
        if cfg.mode == 'symlink' and destination.is_symlink() and destination.resolve() == source.resolve():
            continue
        if cfg.mode == 'copy' and identical:
            continue
        if cfg.mode == 'symlink' and identical:
            target.state, target.severity, target.detail = 'skill-not-linked', 'drift', 'identical copy is not linked; replacement requires corral provenance'
            if not owned:
                continue
        elif mirror not in paths:
            if destination.exists() or destination.is_symlink():
                target.state, target.severity, target.detail = 'skill-unsafe', 'error', 'mirror path is occupied by a directory without SKILL.md; reconcile it before mirroring'
                continue
            target.state, target.severity, target.detail = 'skill-missing-mirror', 'drift', f'copy from {cfg.sides[canonical]}/{name}'
        else:
            unchanged = owned and record['digest'] == digests[mirror]
            same_common = all(name in mirror_files and mirror_files[name][0] == data and bool(mirror_files[name][1] & 0o111) == bool(mode & 0o111) for name, (data, mode) in source_files.items())
            if extra and same_common and cfg.mode == 'copy':
                target.detail = 'canonical files match; mirror has additional files'
            elif unchanged and cfg.mode == 'copy':
                target.state, target.severity, target.detail = 'skill-stale', 'drift', 'unmodified corral mirror differs from the canonical skill'
            else:
                target.state, target.severity, target.detail = 'skill-divergent', 'drift', 'mirror differs and is not provably an unmodified corral copy; use --force with an explicit canonical side'
                if not options.force or cfg.canonical == 'auto':
                    continue
        if cfg.mode == 'symlink' and mirror in paths and not owned:
            target.detail = 'replacing a directory with a link requires corral provenance'
            continue
        for name_extra in extra:
            entry = TargetResult('skill', None, display + '/' + name_extra, state='skill-extra-file', severity='drift', detail='extra file is kept; deletion requires --prune and corral provenance')
            results.append(entry)
            if options.prune and owned:
                entry.detail = 'remove owned extra file under --prune'
                _append_plan(plans, entry, lambda: _delete_file_plan(root, destination / name_extra, entry, mirror_files[name_extra], destination, record, options))
        if cfg.mode == 'symlink' and extra and not options.prune:
            target.detail = 'mirror has extra files; use --prune before replacing it with a link'
            continue
        needs_copy = target.state != 'ok' or (options.prune and owned and bool(extra))
        if needs_copy:
            _append_plan(plans, target, lambda: plan_skill(root, source, destination, canonical, cfg, source_files, {k: v for k, v in mirror_files.items() if not (options.prune and owned and k in extra)}, target, record, options, pruned=frozenset(extra) if options.prune and owned else frozenset()))
    def plan_order(plan):
        for side, relative in sorted(cfg.sides.items()):
            if plan.target.path.startswith(relative + '/'):
                return side, plan.target.path[len(relative) + 1:].split('/')[0]
        return '', plan.target.path
    return results, sorted(plans, key=plan_order)


def _diff_files(display, before, after):
    parts = []
    for name in sorted(set(before) | set(after)):
        old, new = before.get(name, (b'', 0)), after.get(name, (b'', 0))
        if old == new:
            continue
        try:
            parts.append(unified_diff(display + '/' + name, old[0].decode('utf-8'), new[0].decode('utf-8'), missing=name not in before))
        except UnicodeError:
            parts.append(f'Binary file {display}/{name} differs\n')
        if bool(old[1] & 0o111) != bool(new[1] & 0o111):
            parts.append(f'executable bit {display}/{name}: {bool(old[1] & 0o111)} -> {bool(new[1] & 0o111)}\n')
    return ''.join(parts)


def _check_path(root, path):
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError) as exc:
        raise fsio.ChangedDuringSync('path changed during sync; re-run') from exc
    if not resolved.is_relative_to(root):
        raise fsio.ChangedDuringSync('path changed during sync; re-run')
    return resolved


def _tree_paths(destination):
    paths = set()
    def unreadable(error):
        raise error
    for directory, dirs, names in os.walk(destination, followlinks=False, onerror=unreadable):
        for name in dirs + names:
            path = Path(directory) / name
            if path.is_symlink():
                raise fsio.ChangedDuringSync('skill contains a symlink; re-run')
            paths.add(path.relative_to(destination).as_posix())
    return paths


def _append_plan(plans, target, factory):
    try:
        plans.append(factory())
    except (OSError, ValueError, RuntimeError):
        target.severity, target.action = 'error', 'skip'
        target.detail = 'skill changed or became unreadable during evaluation; re-run'


def _record_guard(root, destination, expected_record):
    path = destination / PROVENANCE
    try:
        _check_path(root, path)
        resolved = path.resolve()
        if path.is_symlink():
            raise fsio.ChangedDuringSync('changed during sync; re-run')
        before = fsio.read_bytes(path)
        if expected_record is not None and json.loads(before) != expected_record:
            raise fsio.ChangedDuringSync('provenance changed during sync; re-run')
    except (OSError, ValueError, RuntimeError, TypeError) as exc:
        raise fsio.ChangedDuringSync('provenance is unreadable or changed during sync; re-run') from exc
    def guard():
        current = _check_path(root, path)
        if path.is_symlink() or current != resolved or fsio.read_bytes(path) != before:
            raise fsio.ChangedDuringSync('provenance changed during sync; re-run')
    return guard, before


def _delete_file_plan(root, path, target, old, destination, record, options):
    resolved = path.resolve()
    guard, _ = _record_guard(root, destination, record)
    if options.dry_run:
        target.diff = _diff_files(target.path.rsplit('/', 1)[0], {path.name: old}, {})
    def apply():
        guard()
        current = _check_path(root, path)
        if path.is_symlink() or current != resolved or fsio.read_bytes(path) != old[0] or bool(path.stat().st_mode & 0o111) != bool(old[1] & 0o111):
            raise fsio.ChangedDuringSync('changed during sync; re-run')
        path.unlink()
    return PlannedWrite(target, apply, 'remove')


def _remove_plan(root, destination, target, before, record, options):
    resolved = destination.resolve()
    guard, _ = _record_guard(root, destination, record)
    removed = _tree_paths(destination)
    deleted_paths = [target.path] + [target.path + '/' + name for name in sorted(removed)]
    target.detail = 'remove orphan paths: ' + ', '.join(deleted_paths)
    if options.dry_run:
        target.diff = _diff_files(target.path, before, {}) + ''.join('remove ' + path + '\n' for path in deleted_paths)
    def apply():
        guard()
        current = _check_path(root, destination)
        if destination.is_symlink() or current != resolved or skill_digest(destination) != digest_files(before) or _tree_paths(destination) != removed:
            raise fsio.ChangedDuringSync('changed during sync; re-run')
        shutil.rmtree(destination)
    return PlannedWrite(target, apply, 'remove')


def plan_skill(root, source, destination, canonical, cfg, source_files, mirror_files, target, record, options, *, pruned=frozenset()):
    relative_link = os.path.relpath(source, destination.parent)
    source_resolved, destination_resolved = source.resolve(), destination.resolve()
    guard, record_before = _record_guard(root, destination, record)
    replaced = (_tree_paths(destination) - pruned) if cfg.mode == 'symlink' and mirror_files else set()
    replaced_paths = [target.path + '/' + name for name in sorted(replaced)]
    if replaced_paths:
        target.detail += '; replace directory entries: ' + ', '.join(replaced_paths)
    if options.dry_run:
        target.diff = (f'symlink {target.path} -> {relative_link}\n' + ''.join('replace ' + path + '\n' for path in replaced_paths)) if cfg.mode == 'symlink' else _diff_files(target.path, mirror_files, source_files)
    def apply():
        guard()
        current_source = _check_path(root, source)
        current_destination = _check_path(root, destination)
        if current_source != source_resolved or current_destination != destination_resolved:
            raise fsio.ChangedDuringSync('path changed during sync; re-run')
        if mirror_files:
            if skill_digest(destination) != digest_files(mirror_files):
                raise fsio.ChangedDuringSync('changed during sync; re-run')
        elif destination.exists() or destination.is_symlink():
            raise fsio.ChangedDuringSync('changed during sync; re-run')
        if cfg.mode == 'symlink':
            if destination.exists() or destination.is_symlink():
                if destination.is_symlink() or record is None or _tree_paths(destination) != replaced:
                    raise fsio.ChangedDuringSync('changed during sync; re-run')
                shutil.rmtree(destination)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.symlink_to(relative_link, target_is_directory=True)
            return
        for name, (data, mode) in sorted(source_files.items()):
            path = destination / name
            current = _check_path(root, path)
            if path.is_symlink() or current != destination_resolved / name:
                raise fsio.ChangedDuringSync('changed during sync; re-run')
            previous = mirror_files.get(name)
            if previous is not None and data == previous[0] and bool(mode & 0o111) == bool(previous[1] & 0o111):
                continue
            fsio.write_bytes_atomic(path, data, expected_old=previous[0] if previous else None, mode=mode)
        expected_digest = digest_files({**mirror_files, **source_files})
        if skill_digest(destination) != expected_digest:
            raise fsio.ChangedDuringSync('changed during sync; re-run')
        guard()
        metadata = dict(schema_version=1, canonical_side=canonical, canonical_path=source.relative_to(root).as_posix(), digest=expected_digest)
        record_path = destination / PROVENANCE
        fsio.write_bytes_atomic(record_path, (json.dumps(metadata, sort_keys=True) + '\n').encode(), expected_old=record_before)
    return PlannedWrite(target, apply, 'write' if mirror_files else 'create')
