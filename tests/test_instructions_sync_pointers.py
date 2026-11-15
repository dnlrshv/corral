"""Managed pointer ownership, byte preservation, safety, and reporting."""
import json
import os
import stat

import pytest

from corral.instructions.cli import sync_main
from corral.instructions.config import PointerSpec, SyncConfig
from corral.instructions.markers import body_digest, parse_block, render_block
from corral.instructions.sync import SyncOptions, run_sync
from corral.instructions.templates import render_pointer


@pytest.fixture
def repo(tmp_path):
    (tmp_path / 'AGENTS.md').write_text('Run checks because they verify the change.\n')
    return tmp_path


def invoke(root, *flags):
    return sync_main(['--root', str(root), *flags])


def test_missing_pointer_files_are_created_with_import_block(repo, capsys):
    assert invoke(repo) == 0
    for executor, name in [('claude', 'CLAUDE.md'), ('gemini', 'GEMINI.md')]:
        expected = render_block('pointer', render_pointer(PointerSpec(executor, name, 'import'), 'AGENTS.md', repo))
        assert (repo / name).read_text() == expected
    assert invoke(repo) == 0
    assert capsys.readouterr().out.splitlines()[-1] == 'Instructions sync: clean.'


def test_check_reports_missing_and_exits_1_without_writing(repo, capsys):
    assert invoke(repo, '--check') == 1
    assert not (repo / 'CLAUDE.md').exists()
    assert 'missing' in capsys.readouterr().out


def test_block_inserted_at_top_preserving_existing_content(repo):
    old = b'Agent-specific settings.\nNo final newline'
    (repo / 'CLAUDE.md').write_bytes(old)
    assert invoke(repo) == 0
    assert (repo / 'CLAUDE.md').read_bytes().endswith(b'\n\n' + old)


def test_block_inserted_after_yaml_frontmatter(repo):
    old = b'---\nsetting: true\n---\n\nAgent settings.\n'
    (repo / 'CLAUDE.md').write_bytes(old)
    assert invoke(repo) == 0
    new = (repo / 'CLAUDE.md').read_bytes()
    assert new.startswith(b'---\nsetting: true\n---\n\n<!-- corral:pointer:begin')
    assert new.endswith(b'\nAgent settings.\n')


def test_stale_block_is_rewritten_and_digest_updated(repo):
    (repo / 'CLAUDE.md').write_text(render_block('pointer', 'Old generated template.\n'))
    assert invoke(repo, '--check') == 1
    assert invoke(repo) == 0
    block = parse_block((repo / 'CLAUDE.md').read_text(), 'pointer')
    assert block.digest == body_digest(block.body)
    assert '@AGENTS.md' in block.body


def test_hand_edited_block_is_refused_without_force_and_rewritten_with_force(repo):
    text = render_block('pointer', 'Old generated template.\n').replace('Old generated', 'Hand edited')
    (repo / 'CLAUDE.md').write_text(text)
    assert invoke(repo) == 1
    assert (repo / 'CLAUDE.md').read_text() == text
    assert (repo / 'GEMINI.md').exists()
    assert invoke(repo, '--force') == 0
    assert '@AGENTS.md' in (repo / 'CLAUDE.md').read_text()


@pytest.mark.parametrize('text', [
    '<!-- corral:pointer:begin sha256=0123456789ab -->\n<!-- corral:pointer:begin sha256=0123456789ab -->\n<!-- corral:pointer:end -->\n',
    '<!-- corral:pointer:end -->\n<!-- corral:pointer:begin sha256=0123456789ab -->\n',
    '<!-- corral:pointer:begin sha256=0123456789ab -->\n',
    '<!-- corral:pointer:end -->\n',
    '<!-- corral:pointer:begin sha256=bad -->\n',
    '<!-- corral:pointer:begn -->\n',
])
def test_marker_errors_refuse_target(repo, text):
    (repo / 'CLAUDE.md').write_text(text)
    assert invoke(repo, '--force') == 2
    assert (repo / 'CLAUDE.md').read_text() == text
    assert (repo / 'GEMINI.md').exists()


def test_crlf_file_keeps_crlf_and_compares_clean(repo):
    (repo / 'CLAUDE.md').write_bytes(b'Agent settings.\r\n')
    assert invoke(repo) == 0
    raw = (repo / 'CLAUDE.md').read_bytes()
    assert b'\n' not in raw.replace(b'\r\n', b'')
    assert invoke(repo, '--check') == 0


def test_bom_is_preserved(repo):
    (repo / 'CLAUDE.md').write_bytes(b'\xef\xbb\xbfAgent settings.\n')
    assert invoke(repo) == 0
    assert (repo / 'CLAUDE.md').read_bytes().startswith(b'\xef\xbb\xbf')
    assert invoke(repo, '--check') == 0


def test_no_trailing_newline_outside_block_is_preserved(repo):
    (repo / 'CLAUDE.md').write_bytes(b'Agent settings without newline')
    assert invoke(repo) == 0
    assert (repo / 'CLAUDE.md').read_bytes().endswith(b'Agent settings without newline')


def test_foreign_content_warning_default_error_and_allow(repo):
    (repo / 'CLAUDE.md').write_text('Agent settings.\n')
    assert invoke(repo) == 0
    assert invoke(repo, '--check', '--strict') == 1
    cfg = SyncConfig(foreign_content='error')
    assert run_sync(repo, cfg, SyncOptions(check=True)).exit_code() == 1
    cfg.foreign_content = 'allow'
    assert run_sync(repo, cfg, SyncOptions(check=True, strict=True)).exit_code() == 0


def test_reference_style_uses_relative_link_for_nested_pointer(repo):
    cfg = SyncConfig(pointers=[PointerSpec('copilot', '.github/copilot-instructions.md', 'reference')])
    assert run_sync(repo, cfg, SyncOptions()).exit_code() == 0
    assert '[AGENTS.md](../AGENTS.md)' in (repo / '.github/copilot-instructions.md').read_text()


def test_import_style_nested_pointer_uses_relative_import(repo):
    cfg = SyncConfig(pointers=[PointerSpec('claude', 'nested/CLAUDE.md', 'import')])
    assert run_sync(repo, cfg, SyncOptions()).exit_code() == 0
    assert '@../AGENTS.md' in (repo / 'nested/CLAUDE.md').read_text()


def test_canonical_missing_exits_2(tmp_path):
    assert invoke(tmp_path) == 2
    assert not (tmp_path / 'CLAUDE.md').exists()


def test_pointer_symlink_outside_root_is_unsafe(repo, tmp_path):
    outside = repo.parent / (repo.name + '-outside.md')
    outside.write_text('Outside content')
    (repo / 'CLAUDE.md').symlink_to(outside)
    assert invoke(repo, '--force') == 2
    assert outside.read_text() == 'Outside content'


def test_pointer_symlink_inside_root_is_written_through(repo):
    (repo / 'agent-specific.md').write_text('Agent settings.')
    (repo / 'CLAUDE.md').symlink_to('agent-specific.md')
    assert invoke(repo) == 0
    assert (repo / 'CLAUDE.md').is_symlink()
    assert '@AGENTS.md' in (repo / 'agent-specific.md').read_text()


def test_dry_run_prints_unified_diff_and_writes_nothing(repo, capsys):
    assert invoke(repo, '--dry-run') == 1
    out = capsys.readouterr().out
    assert '--- /dev/null' in out and '+++ b/CLAUDE.md' in out
    assert not (repo / 'CLAUDE.md').exists()


def test_json_report_schema(repo, capsys):
    assert invoke(repo, '--check', '--json') == 1
    value = json.loads(capsys.readouterr().out)
    assert value['schema_version'] == 1 and value['exit_code'] == 1
    assert value['mode'] == 'check' and value['scope'] == 'repo'
    assert set(value['summary']) == {'ok', 'drift', 'warning', 'error'}
    assert set(value['targets'][0]) == {'kind', 'executor', 'path', 'resolved_path', 'state', 'severity', 'action', 'detail', 'diff'}
    assert str(repo) not in json.dumps(value)


def test_concurrent_edit_aborts_target(repo, monkeypatch):
    from corral.instructions import fsio
    (repo / 'CLAUDE.md').write_text('Old content.')
    original = fsio.read_bytes
    seen = set()
    def edit(path):
        if path.name == 'CLAUDE.md':
            if path in seen:
                path.write_text('Concurrent content.')
            seen.add(path)
        return original(path)
    monkeypatch.setattr(fsio, 'read_bytes', edit)
    assert invoke(repo) == 2
    assert (repo / 'CLAUDE.md').read_text() == 'Concurrent content.'
    assert (repo / 'GEMINI.md').exists()


def test_new_file_mode_respects_umask_and_existing_mode_is_kept(repo, monkeypatch):
    from corral.instructions import fsio
    (repo / 'CLAUDE.md').write_text('Settings.')
    (repo / 'CLAUDE.md').chmod(0o751)
    monkeypatch.setattr(fsio, '_NEW_FILE_MODE', None)
    old = os.umask(0o027)
    try:
        assert invoke(repo) == 0
    finally:
        os.umask(old)
    assert stat.S_IMODE((repo / 'CLAUDE.md').stat().st_mode) == 0o751
    assert stat.S_IMODE((repo / 'GEMINI.md').stat().st_mode) == 0o640


def test_repo_scope_never_reads_home_or_overlay(repo, monkeypatch):
    from corral.instructions import user_blocks
    monkeypatch.setenv('HOME', str(repo / 'missing-home'))
    monkeypatch.setenv('CORRAL_OVERLAY_FILE', str(repo / 'unreadable-overlay'))
    def forbidden(*args, **kwargs):
        raise AssertionError('repo scope accessed overlay')
    monkeypatch.setattr(user_blocks, '_load_layers', forbidden)
    assert invoke(repo) == 0
    assert not (repo / 'missing-home').exists()


def test_pointer_templates_pass_instruction_lint(repo):
    from corral.instructions.lint import default_lint_config, lint_text
    for style in ('import', 'reference'):
        text = render_pointer(PointerSpec('claude', 'CLAUDE.md', style), 'AGENTS.md', repo)
        assert not [f for f in lint_text(text, path='CLAUDE.md', kind='instruction', config=default_lint_config()) if f.severity != 'info']


def test_unavailable_scopes_and_targets_return_2(repo):
    assert invoke(repo, '--scope', 'user') == 2
    assert invoke(repo, '--scope', 'all') == 2
    assert invoke(repo, '--only', 'user') == 2


def test_body_bom_is_a_hand_edit(repo):
    text = render_block('pointer', 'Generated template.\n')
    text = text.replace('Generated template.', '\ufeffGenerated template.')
    (repo / 'CLAUDE.md').write_bytes(text.encode('utf-8'))
    assert invoke(repo) == 1
    assert (repo / 'CLAUDE.md').read_bytes() == text.encode('utf-8')


def test_pointer_symlink_loop_is_unsafe(repo):
    (repo / 'CLAUDE.md').symlink_to('CLAUDE.md')
    assert invoke(repo, '--force') == 2
    assert (repo / 'CLAUDE.md').is_symlink()


def test_explicit_skills_selection_skips_pointers(repo):
    assert invoke(repo, '--only', 'skills') == 0
    assert not (repo / 'CLAUDE.md').exists()
    assert not (repo / 'GEMINI.md').exists()


def test_decode_errors_do_not_stop_other_pointers(repo):
    (repo / 'CLAUDE.md').write_bytes(b'\xff\xff')
    assert invoke(repo, '--force') == 2
    assert (repo / 'CLAUDE.md').read_bytes() == b'\xff\xff'
    assert (repo / 'GEMINI.md').exists()


def test_canonical_case_alias_is_never_rewritten(repo):
    alias = repo / 'agents.md'
    if not alias.exists():
        pytest.skip('requires a case-insensitive filesystem')
    before = repo.joinpath('AGENTS.md').read_bytes()
    config = SyncConfig(pointers=[PointerSpec('claude', 'agents.md', 'import')])
    assert run_sync(repo, config, SyncOptions(force=True)).exit_code() == 2
    assert repo.joinpath('AGENTS.md').read_bytes() == before


def test_canonical_hardlink_is_never_rewritten(repo):
    os.link(repo / 'AGENTS.md', repo / 'CLAUDE.md')
    before = repo.joinpath('AGENTS.md').read_bytes()
    assert invoke(repo, '--force') == 2
    assert repo.joinpath('AGENTS.md').read_bytes() == before
