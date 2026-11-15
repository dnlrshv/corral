"""Public lint contracts, discovery, suppressions, and CLI behaviour."""
import json
from importlib.resources import files
import subprocess
import time

import pytest
import yaml

from corral.instructions.cli import lint_main
from corral.instructions.config import instructions_config_from_mapping
from corral.instructions.lint import default_lint_config, discover_files, lint_paths, lint_text
from corral.instructions.text_model import TextModel


def write(root, path, text='ordinary instruction'):
    p = root / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def run(text, *, path='AGENTS.md', kind='instruction', config=None):
    return lint_text(text, path=path, kind=kind, config=config or default_lint_config())


def test_discover_files_default_include_and_exclude(tmp_path):
    for p in ['AGENTS.md', 'sub/CLAUDE.md', '.cursorrules', '.claude/skills/a/SKILL.md', 'node_modules/AGENTS.md', 'docs/guide.md']:
        write(tmp_path, p)
    (tmp_path / 'GEMINI.md').symlink_to('missing')
    assert {p.relative_to(tmp_path).as_posix() for p in discover_files(default_lint_config(), tmp_path)} == {'AGENTS.md', 'sub/CLAUDE.md', '.cursorrules', '.claude/skills/a/SKILL.md'}


def test_discover_files_uses_git_and_skips_ignored(tmp_path):
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    write(tmp_path, '.gitignore', 'ignored/\n')
    write(tmp_path, 'AGENTS.md')
    write(tmp_path, 'ignored/AGENTS.md')
    assert discover_files(default_lint_config(), tmp_path) == [tmp_path / 'AGENTS.md']


def test_discover_files_uses_governance_globs(tmp_path):
    p = write(tmp_path, 'rules/readme.md')
    cfg = default_lint_config()
    cfg.governance_globs = ['rules/**']
    assert discover_files(cfg, tmp_path) == [p]
    cfg.use_governance_globs = False
    assert discover_files(cfg, tmp_path) == []


def test_discover_files_skips_settings_json(tmp_path):
    p = write(tmp_path, '.claude/agents/x.json')
    p.write_bytes(b'\xff' * 10)
    report = lint_paths([], config=default_lint_config(), root=tmp_path)
    assert report.files_checked == 0
    assert not report.input_errors


def test_text_model_masks_code_frontmatter_comments_links():
    text = '---\ndescription: MUST\n---\nprose `MUST` [label](gone.md) <!-- NEVER -->\n```sh\n!!\n```\n'
    doc = TextModel(text)
    assert len(doc.prose) == len(text)
    assert [i for i, c in enumerate(doc.prose) if c == '\n'] == [i for i, c in enumerate(text) if c == '\n']
    assert 'MUST' not in doc.prose and 'NEVER' not in doc.prose
    assert 'label' in doc.prose and 'gone.md' not in doc.prose
    assert 'MUST' in doc.code and '!!' in doc.code
    assert doc.position(text.index('prose')) == (4, 1)
    assert TextModel('`` `x` ``').code.strip() == '`x`'


def test_unclosed_fence_runs_to_eof():
    assert not run('```\n!!\n')
    assert not TextModel('~~~\nNEVER').prose.strip()


def test_double_bang_flags_prose_not_code_or_admonition():
    found = run('Stop!!\n`!!`\n```\n!!\n```\n!!! warning\n!!str\n')
    assert [(f.check, f.line, f.column) for f in found] == [('double-bang', 1, 5)]


RETIRED = [
    'claude-opus-4-1-20250805', 'claude-opus-4-20250514', 'claude-sonnet-4-0',
    'claude-3-5-sonnet-20241022', 'claude-2.1', 'claude-instant-1.2',
    'Claude Opus 4', 'claude-sonnet-4-5', 'claude-mythos-preview',
]


@pytest.mark.parametrize('sample', RETIRED)
def test_retired_model_ids_flagged_with_replacement(sample):
    found = run(sample)
    assert len(found) == 1
    assert 'table as of 2026-11-15' in found[0].message
    assert '; use ' in found[0].message
    table = yaml.safe_load(files('corral.instructions').joinpath('data/model_ids.yaml').read_text())
    assert table['schema_version'] == 1


ACTIVE = ['claude-opus-4-5', 'claude-opus-4-6', 'claude-opus-4-7', 'claude-opus-4-8', 'claude-sonnet-4-6', 'claude-haiku-4-5', 'claude-opus-5', 'claude-opus-5-5', 'claude-sonnet-5', 'claude-sonnet-5-5', 'claude-haiku-5-5', 'claude-fable-5', 'claude-fable-5-1', 'claude-mythos-5', 'claude-mythos-5-1', 'Claude Opus 4.8', 'Claude Haiku 4.5', 'Claude Opus 4.5', 'Claude Sonnet 4.6', 'Claude Opus 5.5', 'Claude Sonnet 5.5', 'Claude Haiku 5.5', 'Claude Fable 5.1', 'Claude Mythos 5.1']


@pytest.mark.parametrize('sample', ACTIVE)
@pytest.mark.parametrize('template', ['{}', '`{}`', '```\n{}\n```'])
def test_active_model_ids_never_flagged(sample, template):
    assert not run(template.format(sample))


@pytest.mark.parametrize('sample', ['anthropic.claude-3-opus-20240229-v1:0', 'claude-opus-4@20250514'])
def test_bedrock_and_vertex_forms_flagged(sample):
    assert len(run(sample)) == 1


def test_retired_model_frontmatter_only_description_and_model():
    assert not run('---\nname: claude-2\n---\n')
    assert len(run('---\nmodel: claude-2\n---\n')) == 1
    assert len(run('---\ndescription: |\n  use claude-2\n---\n')) == 1


def test_retired_model_config_extra_allow_exclude():
    cfg = instructions_config_from_mapping({'lint': {'checks': {'retired-model-id': {'extra': [{'id': 'old', 'status': 'retired', 'replacement': 'current', 'pattern': 'old-model'}], 'allow': ['claude-2'], 'exclude': ['archive/**']}}}}).lint
    assert len(run('old-model claude-2', config=cfg)) == 1
    assert not run('old-model', path='archive/AGENTS.md', config=cfg)


def test_suppression_next_line_line_and_file(tmp_path):
    text = '<!-- corral-lint: disable-next-line=double-bang -->\n\nStop!!\nStop!! <!-- corral-lint: disable-line=double-bang -->\n'
    p = write(tmp_path, 'AGENTS.md', text)
    report = lint_paths([p], config=default_lint_config(), root=tmp_path)
    assert report.suppressed == 2
    assert not report.findings
    assert not run('Stop!!\n<!-- corral-lint: disable-file=all -->')


def test_suppression_hash_form_only_outside_markdown():
    text = '# corral-lint: disable-next-line=double-bang\nStop!!'
    assert not run(text, path='.cursorrules')
    assert run(text, path='AGENTS.md')[0].check == 'double-bang'


def test_unknown_and_unused_suppression_reported():
    found = run('<!-- corral-lint: disable-next-line=absent -->\nordinary\n')
    assert {f.check for f in found} == {'unknown-suppression', 'unused-suppression'}


def test_json_output_schema_v1(tmp_path, capsys):
    p = write(tmp_path, 'AGENTS.md', 'Stop!!\n<!-- corral-lint: disable-next-line=double-bang -->\nordinary\n')
    assert lint_main(['--root', str(tmp_path), '--json', str(p)]) == 0
    output = capsys.readouterr().out
    value = json.loads(output)
    assert output.strip() == json.dumps(value, sort_keys=True)
    assert str(tmp_path) not in output
    assert value['schema_version'] == 1 and value['summary']['info'] == 1
    assert len(value['findings']) == 1
    assert set(value) == {'schema_version', 'tool', 'corral_version', 'files_checked', 'min_severity', 'summary', 'findings', 'input_errors', 'exit_code'}


def test_text_output_format_and_summary_line(tmp_path, capsys):
    write(tmp_path, 'AGENTS.md', 'Stop!!')
    assert lint_main(['--root', str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert 'AGENTS.md:1:5: warning double-bang' in out
    assert out.splitlines()[-1].startswith('instructions lint: 0 errors, 1 warnings')


def test_exit_codes_strict_and_non_strict(tmp_path):
    p = write(tmp_path, 'AGENTS.md', 'Stop!!')
    assert lint_main(['--root', str(tmp_path)]) == 0
    assert lint_main(['--root', str(tmp_path), '--strict']) == 1
    p.write_text('claude-2')
    assert lint_main(['--root', str(tmp_path)]) == 1
    p.write_text('ordinary')
    assert lint_main(['--root', str(tmp_path), '--strict']) == 0


def test_exit_code_2_on_missing_path_and_bad_config(tmp_path):
    assert lint_main(['--root', str(tmp_path), 'missing.md']) == 2
    assert lint_main(['--root', str(tmp_path / 'absent')]) == 2
    assert lint_main(['--select', 'absent']) == 2
    cfg = write(tmp_path, 'corral.yaml', 'instructions:\n  lint:\n    mystery: 1\n')
    assert lint_main(['--config', str(cfg)]) == 2


def test_only_configured_drops_non_matching_paths(tmp_path):
    p = write(tmp_path, 'ordinary.md', 'claude-2')
    assert lint_main(['--root', str(tmp_path), '--only-configured', str(p)]) == 0
    assert lint_main(['--root', str(tmp_path), str(p)]) == 1


def test_lint_text_generated_rule_severities_for_slice1_checks():
    found = run('Stop!! Use claude-2', kind='generated_rule')
    assert {f.check: f.severity for f in found if f.check in ('double-bang', 'retired-model-id')} == {'double-bang': 'error', 'retired-model-id': 'error'}


def test_skipped_files_do_not_fail(tmp_path):
    cfg = default_lint_config()
    cfg.max_file_bytes = 20
    a = write(tmp_path, 'AGENTS.md', 'x' * 21)
    b = write(tmp_path, 'CLAUDE.md')
    b.write_bytes(b'\xff\xff')
    c = tmp_path / 'GEMINI.md'
    c.symlink_to('absent')
    report = lint_paths([a, b, c], config=cfg, root=tmp_path)
    assert report.exit_code(strict=True) == 0
    assert len(report.input_errors) == 3


def test_long_line_is_linear():
    start = time.monotonic()
    assert not run('a' * 200_000)
    assert time.monotonic() - start < 2
    start = time.monotonic()
    assert not run('[' * 200_000)
    assert time.monotonic() - start < 2
    assert len(run('Stop!! ' * 1000)) == 1000


def test_select_ignore_and_list_checks(capsys, tmp_path):
    write(tmp_path, 'AGENTS.md', 'claude-2 Stop!!')
    assert lint_main(['--root', str(tmp_path), '--select', 'double-bang']) == 0
    assert lint_main(['--root', str(tmp_path), '--ignore', 'retired-model-id']) == 0
    assert lint_main(['--list-checks']) == 0
    assert 'retired-model-id' in capsys.readouterr().out


def test_config_errors_do_not_expose_absolute_paths(tmp_path, capsys):
    assert lint_main(['--config', str(tmp_path / 'absent.yaml')]) == 2
    assert str(tmp_path) not in capsys.readouterr().err
