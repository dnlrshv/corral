"""Strict instruction sync configuration and shared config loading."""
import pytest
import yaml

from corral.config import load_config
from corral.instructions.config import instructions_config_from_mapping


def sync(raw=None):
    return instructions_config_from_mapping({'sync': raw or {}}).sync


def test_defaults():
    cfg = sync()
    assert cfg.canonical == 'AGENTS.md' and cfg.foreign_content == 'warn'
    assert [(p.executor, p.path, p.style) for p in cfg.pointers] == [('claude', 'CLAUDE.md', 'import'), ('gemini', 'GEMINI.md', 'import')]
    assert cfg.skills.enabled and cfg.skills.mode == 'copy' and cfg.skills.canonical == 'auto'
    assert cfg.skills.sides == {'claude': '.claude/skills', 'agents': '.agents/skills'}
    assert cfg.user.layers == ['overlay'] and cfg.user.harnesses['gemini'] == ['gemini', 'agy']


def test_pointers_mapping_replaces_defaults():
    assert sync({'pointers': {}}).pointers == []
    assert len(sync({'pointers': {'cursor': {'path': '.cursorrules', 'style': 'reference'}}}).pointers) == 1


@pytest.mark.parametrize('raw', [
    {'unknown': True}, {'skills': {'unknown': True}}, {'skills': {'sides': {'unknown': 'x'}}},
    {'user': {'unknown': True}}, {'user': {'harnesses': {'unknown': ['x']}}},
    {'pointers': {'claude': {'path': 'CLAUDE.md', 'unknown': True}}},
    {'pointers': {'unknown': {'path': 'unknown.md'}}},
])
def test_unknown_keys_rejected(raw):
    with pytest.raises(ValueError, match='corral.yaml: instructions.sync'):
        sync(raw)


@pytest.mark.parametrize('path', ['', '/', '../AGENTS.md', 'docs/../AGENTS.md', '/absolute/rules.md', 'C:\\rules.md', 'docs\\rules.md'])
def test_paths_must_be_relative_without_dotdot(path):
    for raw in ({'canonical': path}, {'pointers': {'claude': {'path': path}}}, {'skills': {'sides': {'claude': path}}}):
        with pytest.raises(ValueError):
            sync(raw)


def test_codex_pointer_rejected_when_canonical_is_agents_md():
    with pytest.raises(ValueError, match='codex reads AGENTS.md natively'):
        sync({'pointers': {'codex': {'path': 'CODEX.md'}}})
    assert sync({'canonical': 'docs/rules.md', 'pointers': {'codex': {'path': 'AGENTS.md'}}}).pointers[0].style == 'reference'


def test_pointer_equal_to_canonical_rejected():
    with pytest.raises(ValueError, match='canonical'):
        sync({'pointers': {'claude': {'path': 'AGENTS.md'}}})


def test_duplicate_pointer_paths_rejected():
    with pytest.raises(ValueError, match='distinct paths'):
        sync({'pointers': {'claude': {'path': 'shared.md'}, 'gemini': {'path': './shared.md'}}})


def test_load_config_exposes_instructions_section(tmp_path):
    path = tmp_path / 'corral.yaml'
    path.write_text(yaml.safe_dump({'instructions': {'sync': {'pointers': {}}, 'lint': {'min_severity': 'info'}}}))
    config = load_config(path)
    assert not config.instructions.sync.pointers
    assert config.instructions.lint.min_severity == 'info'
    assert config.root == tmp_path


@pytest.mark.parametrize('raw', [
    {'foreign_content': True}, {'foreign_content': 'unknown'},
    {'pointers': {'claude': {'path': 'CLAUDE.md', 'style': 'unknown'}}},
    {'skills': {'enabled': 1}}, {'skills': {'mode': 'unknown'}}, {'skills': {'canonical': 'unknown'}},
    {'skills': {'sides': {'agents': '.claude/skills/sub'}}},
    {'skills': {'sides': {'agents': '.claude/skills'}}},
    {'user': {'layers': []}}, {'user': {'layers': ['unknown']}},
    {'user': {'harnesses': {'claude': []}}}, {'user': {'harnesses': {'claude': ['']}}},
])
def test_enum_types_and_nested_sides_rejected(raw):
    with pytest.raises(ValueError):
        sync(raw)


def test_user_harness_override_preserves_other_defaults():
    cfg = sync({'user': {'harnesses': {'claude': ['custom-claude']}, 'layers': ['packaged', 'overlay']}})
    assert cfg.user.harnesses['claude'] == ['custom-claude']
    assert cfg.user.harnesses['gemini'] == ['gemini', 'agy']
    assert cfg.user.layers == ['packaged', 'overlay']


def test_default_instruction_configs_do_not_share_option_lists():
    first = instructions_config_from_mapping({})
    second = instructions_config_from_mapping({})
    first.lint.checks['caps-emphasis'].options['reason_markers'].append('a custom reason')
    first.sync.user.harnesses['claude'].append('a custom harness')
    assert 'a custom reason' not in second.lint.checks['caps-emphasis'].options['reason_markers']
    assert 'a custom harness' not in second.sync.user.harnesses['claude']
    third = instructions_config_from_mapping({})
    assert 'a custom reason' not in third.lint.checks['caps-emphasis'].options['reason_markers']
