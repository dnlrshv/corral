"""Skill mirroring preserves user content and records copy provenance."""
import json
import stat

import pytest

from corral.instructions.config import SyncConfig
from corral.instructions.skills import skill_digest
from corral.instructions.sync import SyncOptions, run_sync


@pytest.fixture
def repo(tmp_path):
    (tmp_path / 'AGENTS.md').write_text('Run checks because they verify the change.\n')
    return tmp_path


def skill(root, side='claude', name='review', body='Review changes because the checklist explains verification.\n'):
    directory = root / ('.claude' if side == 'claude' else '.agents') / 'skills' / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'SKILL.md').write_text('---\nname: review\ndescription: Review changes\n---\n' + body)
    return directory


def sync(root, *, canonical='auto', mode='copy', **flags):
    cfg = SyncConfig(pointers=[])
    cfg.skills.canonical = canonical
    cfg.skills.mode = mode
    return run_sync(root, cfg, SyncOptions(**flags))


def mirror(root, side='agents'):
    return root / ('.claude' if side == 'claude' else '.agents') / 'skills/review'


def test_claude_only_skill_is_copied_to_agents_with_provenance(repo):
    source = skill(repo)
    script = source / 'check.sh'
    script.write_bytes(b'#!/bin/sh\nexit 0\n')
    script.chmod(0o755)
    (source / 'asset.bin').write_bytes(b'\x00\xff')
    report = sync(repo)
    assert report.exit_code() == 0
    target = mirror(repo)
    for name in ('SKILL.md', 'check.sh', 'asset.bin'):
        assert (source / name).read_bytes() == (target / name).read_bytes()
    assert stat.S_IMODE((target / 'check.sh').stat().st_mode) & 0o111
    record = json.loads((target / '.corral-mirror.json').read_text())
    assert record['schema_version'] == 1 and record['canonical_side'] == 'claude'
    assert record['canonical_path'] == '.claude/skills/review'
    assert record['digest'] == skill_digest(target)
    assert sync(repo, check=True).exit_code() == 0


def test_agents_only_skill_is_copied_to_claude_in_auto_mode(repo):
    source = skill(repo, 'agents')
    assert sync(repo).exit_code() == 0
    assert mirror(repo, 'claude').joinpath('SKILL.md').read_bytes() == source.joinpath('SKILL.md').read_bytes()


def test_identical_skill_on_both_sides_is_ok_without_provenance(repo):
    skill(repo)
    skill(repo, 'agents')
    assert sync(repo, check=True).exit_code() == 0
    assert not (mirror(repo) / '.corral-mirror.json').exists()


def test_divergent_skill_in_auto_mode_is_refused_even_with_force(repo):
    a = skill(repo, body='First rule.')
    b = skill(repo, 'agents', body='Second rule.')
    before = ((a / 'SKILL.md').read_bytes(), (b / 'SKILL.md').read_bytes())
    assert sync(repo, force=True).exit_code() == 1
    assert before == ((a / 'SKILL.md').read_bytes(), (b / 'SKILL.md').read_bytes())


def test_stale_mirror_with_matching_provenance_is_updated(repo):
    source = skill(repo)
    assert sync(repo).exit_code() == 0
    (source / 'guide.md').write_text('Additional reference.')
    report = sync(repo, check=True)
    assert any(t.state == 'skill-stale' for t in report.targets)
    assert sync(repo).exit_code() == 0
    assert mirror(repo).joinpath('guide.md').read_text() == 'Additional reference.'


def test_hand_edited_mirror_is_divergent_and_force_overwrites_with_explicit_canonical(repo):
    source = skill(repo)
    sync(repo)
    (mirror(repo) / 'SKILL.md').write_text('Hand edited rule.')
    assert sync(repo).exit_code() == 1
    assert sync(repo, canonical='claude').exit_code() == 1
    assert sync(repo, canonical='claude', force=True).exit_code() == 0
    assert (mirror(repo) / 'SKILL.md').read_bytes() == (source / 'SKILL.md').read_bytes()


def test_extra_file_kept_without_prune_and_deleted_with_prune_only_under_provenance(repo):
    source = skill(repo)
    (source / 'old.md').write_text('Old reference.')
    sync(repo)
    (source / 'old.md').unlink()
    report = sync(repo, canonical='claude')
    assert report.exit_code() == 1 and (mirror(repo) / 'old.md').exists()
    dry = sync(repo, canonical='claude', dry_run=True, prune=True)
    assert 'old.md' in ''.join(t.diff or '' for t in dry.targets)
    assert (mirror(repo) / 'old.md').exists()
    assert sync(repo, canonical='claude', prune=True).exit_code() == 0
    assert not (mirror(repo) / 'old.md').exists()
    (mirror(repo) / '.corral-mirror.json').unlink()
    (mirror(repo) / 'user.md').write_text('User content.')
    assert sync(repo, canonical='claude', prune=True, force=True).exit_code() == 1
    assert (mirror(repo) / 'user.md').exists()


def test_orphan_mirror_kept_without_prune_deleted_with_prune(repo):
    import shutil
    source = skill(repo)
    sync(repo)
    shutil.rmtree(source)
    assert sync(repo).exit_code() == 1
    assert mirror(repo).exists()
    assert sync(repo, prune=True).exit_code() == 0
    assert not mirror(repo).exists()


def test_skill_authored_on_non_canonical_side_is_never_deleted(repo):
    authored = skill(repo, 'agents')
    assert sync(repo, canonical='claude', force=True, prune=True).exit_code() == 1
    assert authored.joinpath('SKILL.md').exists()


def test_symlink_mode_creates_relative_link_and_replaces_identical_copy_only_with_provenance(repo):
    source = skill(repo)
    assert sync(repo, mode='symlink').exit_code() == 0
    assert mirror(repo).is_symlink()
    assert not mirror(repo).readlink().is_absolute()
    assert mirror(repo).resolve() == source.resolve()
    assert sync(repo, mode='symlink', check=True).exit_code() == 0
    mirror(repo).unlink()
    skill(repo, 'agents')
    assert sync(repo, mode='symlink').exit_code() == 1
    assert not mirror(repo).is_symlink()
    mirror(repo).joinpath('.corral-mirror.json').write_text(json.dumps(dict(schema_version=1, canonical_side='claude', canonical_path='.claude/skills/review', digest=skill_digest(mirror(repo)))))
    assert sync(repo, mode='symlink').exit_code() == 0
    assert mirror(repo).is_symlink()


def test_symlink_inside_skill_is_unsafe(repo):
    source = skill(repo)
    (source / 'unsafe.md').symlink_to('SKILL.md')
    report = sync(repo, force=True, prune=True)
    assert report.exit_code() == 2
    assert any(t.state == 'skill-unsafe' for t in report.targets)
    assert not mirror(repo).exists()


def test_directory_without_skill_md_is_ignored_with_info_line(repo):
    directory = repo / '.claude/skills/notes'
    directory.mkdir(parents=True)
    (directory / 'guide.md').write_text('Reference.')
    report = sync(repo)
    assert report.exit_code() == 0
    assert 'info' in report.render_text() and 'no SKILL.md' in report.render_text()
    assert not (repo / '.agents/skills/notes').exists()


def test_claude_only_frontmatter_keys_reported_as_info(repo):
    source = skill(repo)
    source.joinpath('SKILL.md').write_text('---\ndescription: Review changes\nallowed-tools: Read\nmodel: current\ndisable-model-invocation: true\n---\nReview changes.\n')
    report = sync(repo)
    assert report.exit_code() == 0
    for key in ('allowed-tools', 'model', 'disable-model-invocation'):
        assert key in report.render_text()
    assert source.joinpath('SKILL.md').read_bytes() == mirror(repo).joinpath('SKILL.md').read_bytes()


def test_skill_digest_ignores_ds_store_and_pycache(repo):
    source = skill(repo)
    before = skill_digest(source)
    source.joinpath('.DS_Store').write_bytes(b'metadata')
    source.joinpath('__pycache__').mkdir()
    source.joinpath('__pycache__/cached.pyc').write_bytes(b'cache')
    source.joinpath('module.pyc').write_bytes(b'cache')
    source.joinpath('.corral-mirror.json').write_text('{}')
    assert before == skill_digest(source)
    source.joinpath('script.sh').write_text('echo test')
    before = skill_digest(source)
    source.joinpath('script.sh').chmod(0o755)
    assert before != skill_digest(source)


def test_skills_disabled_skips_evaluation(repo):
    source = skill(repo)
    source.joinpath('unsafe.md').symlink_to('/outside/missing')
    cfg = SyncConfig(pointers=[])
    cfg.skills.enabled = False
    report = run_sync(repo, cfg, SyncOptions())
    assert report.exit_code() == 0 and not report.targets


def test_unsafe_skill_does_not_stop_other_skills(repo):
    bad = skill(repo, name='unsafe')
    (bad / 'link.md').symlink_to('SKILL.md')
    good = skill(repo, name='valid')
    assert sync(repo).exit_code() == 2
    assert (repo / '.agents/skills/valid/SKILL.md').read_bytes() == (good / 'SKILL.md').read_bytes()
    assert not (repo / '.agents/skills/unsafe').exists()


def test_symlink_conversion_keeps_extra_files_until_prune(repo):
    source = skill(repo)
    (source / 'old.md').write_text('Old reference.')
    sync(repo)
    (source / 'old.md').unlink()
    report = sync(repo, canonical='claude', mode='symlink', force=True)
    assert report.exit_code() == 1
    assert not mirror(repo).is_symlink() and (mirror(repo) / 'old.md').exists()
    assert sync(repo, canonical='claude', mode='symlink', force=True, prune=True).exit_code() == 0
    assert mirror(repo).is_symlink()
    assert not (mirror(repo) / 'old.md').exists()


def test_physical_side_alias_is_unsafe_and_never_changes_source(repo):
    source = skill(repo)
    before = (source / 'SKILL.md').read_bytes()
    (repo / '.agents').mkdir()
    (repo / '.agents/skills').symlink_to('../.claude/skills', target_is_directory=True)
    assert sync(repo, canonical='claude', mode='symlink', force=True, prune=True).exit_code() == 2
    assert (source / 'SKILL.md').read_bytes() == before


def test_skill_directory_symlink_outside_root_is_unsafe(repo):
    source = skill(repo)
    outside = repo.parent / (repo.name + '-outside-skill')
    outside.mkdir()
    outside.joinpath('SKILL.md').write_text('Outside instructions.')
    (source.parent / 'external').symlink_to(outside, target_is_directory=True)
    report = sync(repo, force=True, prune=True)
    assert report.exit_code() == 2
    assert not (repo / '.agents/skills/external').exists()
    assert outside.joinpath('SKILL.md').read_text() == 'Outside instructions.'


def test_skill_decode_error_is_reported_and_not_mirrored(repo):
    source = skill(repo)
    source.joinpath('SKILL.md').write_bytes(b'\xff')
    report = sync(repo)
    assert report.exit_code() == 2
    assert any(t.state == 'decode-error' for t in report.targets)
    assert not mirror(repo).exists()


def test_ignored_non_skill_directory_is_not_read(repo, monkeypatch):
    directory = repo / '.claude/skills/notes'
    directory.mkdir(parents=True)
    secret = directory / 'not-an-instruction.txt'
    secret.write_text('Local data.')
    original = type(secret).read_bytes
    def refuse(path):
        if path == secret:
            raise AssertionError('read ignored non-skill content')
        return original(path)
    monkeypatch.setattr(type(secret), 'read_bytes', refuse)
    assert sync(repo).exit_code() == 0


def test_concurrent_edit_is_not_claimed_by_new_provenance(repo, monkeypatch):
    from corral.instructions import fsio
    source = skill(repo)
    sync(repo)
    source.joinpath('guide.md').write_text('New reference.')
    original = fsio.write_bytes_atomic
    def concurrent(path, data, **kwargs):
        if path.name == 'guide.md':
            mirror(repo).joinpath('SKILL.md').write_text('Concurrent hand edit.')
        return original(path, data, **kwargs)
    monkeypatch.setattr(fsio, 'write_bytes_atomic', concurrent)
    assert sync(repo).exit_code() == 2
    assert mirror(repo).joinpath('SKILL.md').read_text() == 'Concurrent hand edit.'
    assert sync(repo).exit_code() == 1
    assert mirror(repo).joinpath('SKILL.md').read_text() == 'Concurrent hand edit.'


def test_provenance_race_aborts_before_copy(repo, monkeypatch):
    from corral.instructions import fsio
    source = skill(repo)
    sync(repo)
    source.joinpath('guide.md').write_text('New reference.')
    original = fsio.read_bytes
    calls = []
    def concurrent(path):
        if path.name == '.corral-mirror.json':
            calls.append(path)
            if len(calls) == 2:
                path.write_text('Concurrent provenance edit.')
        return original(path)
    monkeypatch.setattr(fsio, 'read_bytes', concurrent)
    assert sync(repo).exit_code() == 2
    assert mirror(repo).joinpath('.corral-mirror.json').read_text() == 'Concurrent provenance edit.'
    assert not mirror(repo).joinpath('guide.md').exists()


def test_occupied_non_skill_mirror_is_preserved(repo):
    skill(repo)
    destination = mirror(repo)
    destination.mkdir(parents=True)
    destination.joinpath('user.md').write_text('User content.')
    assert sync(repo, canonical='claude', force=True, prune=True).exit_code() == 2
    assert destination.joinpath('user.md').read_text() == 'User content.'
    assert not destination.joinpath('SKILL.md').exists()


def test_orphan_provenance_race_preserves_directory(repo, monkeypatch):
    import shutil
    from corral.instructions import fsio
    source = skill(repo)
    sync(repo)
    shutil.rmtree(source)
    original = fsio.read_bytes
    calls = []
    def concurrent(path):
        if path.name == '.corral-mirror.json':
            calls.append(path)
            if len(calls) == 2:
                data = json.loads(path.read_text())
                data['canonical_side'] = 'agents'
                data['canonical_path'] = '.agents/skills/review'
                path.write_text(json.dumps(data))
        return original(path)
    monkeypatch.setattr(fsio, 'read_bytes', concurrent)
    assert sync(repo, prune=True).exit_code() == 2
    assert mirror(repo).joinpath('SKILL.md').exists()


def test_orphan_prune_lists_metadata_and_ignored_paths(repo):
    import shutil
    source = skill(repo)
    sync(repo)
    mirror(repo).joinpath('.DS_Store').write_bytes(b'metadata')
    shutil.rmtree(source)
    report = sync(repo, dry_run=True, prune=True)
    diff = ''.join(t.diff or '' for t in report.targets)
    assert '.corral-mirror.json' in diff and '.DS_Store' in diff and 'SKILL.md' in diff
    assert mirror(repo).exists()
    written = sync(repo, prune=True)
    assert written.exit_code() == 0
    assert '.corral-mirror.json' in written.render_text()
    assert '.DS_Store' in written.render_text()


def test_symlink_inside_ignored_cache_is_unsafe(repo):
    source = skill(repo)
    cache = source / '__pycache__'
    cache.mkdir()
    cache.joinpath('link.pyc').symlink_to('../SKILL.md')
    assert sync(repo).exit_code() == 2
    assert not mirror(repo).exists()


def test_extra_file_parent_retargeting_is_refused(repo, monkeypatch):
    from corral.instructions import skills
    source = skill(repo)
    source.joinpath('sub').mkdir()
    source.joinpath('sub/extra.md').write_text('Original.')
    sync(repo)
    source.joinpath('sub/extra.md').unlink()
    victim = repo / 'unrelated'
    victim.mkdir()
    victim.joinpath('extra.md').write_text('Original.')
    original = skills._check_path
    changed = []
    def concurrent(root, path):
        if path.name == 'extra.md' and not changed:
            changed.append(path)
            mirror(repo).joinpath('sub').rename(mirror(repo) / 'old-sub')
            mirror(repo).joinpath('sub').symlink_to(victim, target_is_directory=True)
        return original(root, path)
    monkeypatch.setattr(skills, '_check_path', concurrent)
    assert sync(repo, canonical='claude', prune=True).exit_code() == 2
    assert victim.joinpath('extra.md').read_text() == 'Original.'


def test_cli_skill_dry_run_diff_and_copy(repo, capsys):
    from corral.instructions.cli import sync_main
    source = skill(repo)
    source.joinpath('asset.bin').write_bytes(b'\x00\xff')
    args = ['--root', str(repo), '--only', 'skills', '--json']
    assert sync_main([*args, '--dry-run']) == 1
    report = json.loads(capsys.readouterr().out)
    assert report['mode'] == 'dry-run' and report['exit_code'] == 1
    assert all(t['kind'] == 'skill' and t['executor'] is None for t in report['targets'])
    diff = ''.join(t['diff'] or '' for t in report['targets'])
    assert '+++ b/.agents/skills/review/SKILL.md' in diff and 'Binary file .agents/skills/review/asset.bin differs' in diff
    assert not mirror(repo).exists()
    assert sync_main(args) == 0
    capsys.readouterr()
    assert sync_main([*args, '--check']) == 0
    assert not repo.joinpath('CLAUDE.md').exists()


def test_case_alias_skill_sides_are_refused(repo):
    source = skill(repo)
    if not repo.joinpath('.CLAUDE/skills').exists():
        pytest.skip('requires a case-insensitive filesystem')
    before = source.joinpath('SKILL.md').read_bytes()
    source.joinpath('.corral-mirror.json').write_text(json.dumps(dict(schema_version=1, canonical_side='claude', canonical_path='.claude/skills/review', digest=skill_digest(source))))
    cfg = SyncConfig(pointers=[])
    cfg.skills.sides['agents'] = '.CLAUDE/skills'
    cfg.skills.canonical, cfg.skills.mode = 'claude', 'symlink'
    assert run_sync(repo, cfg, SyncOptions(force=True, prune=True)).exit_code() == 2
    assert source.joinpath('SKILL.md').read_bytes() == before
