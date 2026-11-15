"""Precision and severity contracts for prose instruction checks."""
from pathlib import Path

import pytest

from corral.instructions.config import instructions_config_from_mapping
from corral.instructions.lint import default_lint_config, lint_paths, lint_text


def check(text, *, kind='instruction', cfg=None):
    return lint_text(text, path='AGENTS.md', kind=kind, config=cfg or default_lint_config())


def select(text, slug, **kwargs):
    return [f for f in check(text, **kwargs) if f.check == slug]


def test_caps_cluster_flags_unreasoned_pair_in_paragraph():
    found = select('NEVER edit generated files. ALWAYS run tests.', 'caps-emphasis')
    assert len(found) == 1 and found[0].column == 1
    assert '2 unreasoned' in found[0].message


@pytest.mark.parametrize('text', [
    'NEVER force-push, because CI publishes from main.',
    'NEVER force-push. This is because CI publishes from main.',
    'NEVER force-push. The reason is the publish trigger.',
    'NEVER force-push. Otherwise the release history changes.',
    'NEVER force-push, so that the release history stays stable.',
])
def test_caps_reasoned_occurrence_is_exempt(text):
    assert not select(text, 'caps-emphasis', kind='generated_rule')


def test_caps_single_tokens_in_separate_list_items_not_flagged():
    assert not select('- NEVER edit a\n- NEVER edit b\n- NEVER edit c', 'caps-emphasis')


@pytest.mark.parametrize('text', ['IMPORTANT: inspect changes.', '- **WARNING:** inspect changes.', 'NOTE — inspect changes.', '## IMPORTANT\n\nInspect changes.'])
def test_caps_booster_label_flagged(text):
    assert len(select(text, 'caps-emphasis')) == 1


def test_caps_density_is_info_and_threshold_exact():
    text = '\n'.join('- NEVER edit generated files.' for _ in range(4))
    assert not select(text, 'caps-emphasis')
    findings = select(text + '\n- NEVER edit generated files.', 'caps-emphasis')
    assert len(findings) == 1 and findings[0].severity == 'info'
    assert not select(text + '\n- NEVER edit generated files.\n\n' + 'ordinary ' * 1100, 'caps-emphasis')


def test_caps_rfc2119_file_exempt():
    assert not select('The keywords are interpreted as described in RFC 2119. MUST NEVER ALWAYS.', 'caps-emphasis')


def test_caps_registered_anchor_exempt(tmp_path):
    from corral.governance.registry import parse_registry
    root = Path(__file__).parent / 'fixtures/instructions_lint/repo'
    registry = (root / 'registry.yaml').read_text()
    assert parse_registry(registry)
    cfg = default_lint_config()
    cfg.registry = 'registry.yaml'
    (tmp_path / 'registry.yaml').write_text(registry)
    p = tmp_path / 'good/registered_anchor.md'
    p.parent.mkdir()
    p.write_text('Payment changes MUST include a rollback note.\n')
    # Lower the threshold to prove the exemption, even for a single token.
    cfg.checks['caps-emphasis'].options['paragraph_threshold'] = 1
    assert not lint_paths([p], config=cfg, root=tmp_path).findings
    (tmp_path / 'registry.yaml').unlink()
    assert lint_paths([p], config=cfg, root=tmp_path).findings


def test_bad_registry_reports_one_note_and_continues(tmp_path):
    cfg = default_lint_config()
    cfg.registry = 'registry.yaml'
    (tmp_path / 'registry.yaml').write_text('broken: true')
    p = tmp_path / 'AGENTS.md'
    p.write_text('Stop!!')
    report = lint_paths([p], config=cfg, root=tmp_path)
    assert len(report.input_errors) == 1
    assert 'anchor exemptions disabled' in report.input_errors[0]
    assert report.exit_code(strict=False) == 0
    assert report.findings[0].check == 'double-bang'


def test_caps_frontmatter_never_flagged():
    assert not select('---\ndescription: MUST NEVER ALWAYS use this skill\n---\n', 'caps-emphasis', kind='skill')


@pytest.mark.parametrize('text', ['Think step by step', 'thinking step-by-step', 'Take a deep breath', "don't overthink", 'do not think', 'think less', 'think more carefully', 'think longer', 'show your reasoning', '`<scratchpad>`', '<thinking>'])
def test_thinking_scaffold_patterns_and_agent_keywords(text):
    assert select(text, 'thinking-scaffold')
    assert not select('ultrathink; think harder', 'thinking-scaffold')
    assert select('ultrathink', 'thinking-scaffold', kind='generated_rule')
    assert not select('```\n<thinking>\n```', 'thinking-scaffold')
    assert not select('<!-- <thinking> -->', 'thinking-scaffold')


def test_word_cap_flags_words_not_characters_or_lines():
    assert select('Answer in at most 50 words.', 'word-cap')
    assert select('Keep responses under ten bullet points.', 'word-cap')
    assert not select('commit subject under 72 characters; functions under 50 lines; no longer than 72 characters', 'word-cap')


def test_history_dated_state_warning_others_info():
    found = select('As of 2026-08-23 main is green.\n\n(#123)\n\nRule (2026-10-05)\n\nafter the last outage', 'history-narrative')
    assert len(found) == 4
    assert found[0].severity == 'warning'
    assert all(f.severity == 'info' for f in found[1:])


def test_history_issue_ref_ignores_urls_and_anchors():
    assert not select('https://x/pull/123 [link](#section-12) &#123;', 'history-narrative')
    assert len(select('[#12](https://x/pull/12)', 'history-narrative')) == 1
    cfg = default_lint_config()
    cfg.registered_anchors = {'AGENTS.md': ('Rule (#123)',)}
    assert not select('Rule (#123)', 'history-narrative', cfg=cfg)


def test_migration_no_longer_than_is_not_flagged():
    assert not select('no longer than 72 characters; use X instead of Y', 'migration-relative')
    assert select('This no longer applies; now works differently; also counts', 'migration-relative')


def test_any_more_requires_negation():
    assert not select('Do we need any more files?', 'migration-relative')
    assert select('We do not need this any more.', 'migration-relative')


@pytest.mark.parametrize('text', ['You will be graded on precision', 'You are being evaluated', 'this task is not scored', 'hidden tests', 'judged by a reviewer'])
def test_grader_vocabulary_patterns(text):
    assert select(text, 'grader-vocabulary')
    assert not select('Read the rubric in the handbook.', 'grader-vocabulary')


def test_generated_rule_missing_reason():
    assert select('Run the migration check before merging.', 'missing-reason', kind='generated_rule')[0].severity == 'warning'
    assert not select('Run the migration check before merging because the queue tests the merge commit.', 'missing-reason', kind='generated_rule')
    assert not select('Run the migration check.', 'missing-reason')


@pytest.mark.parametrize('slug,text,severity', [
    ('caps-emphasis', 'NEVER edit generated files.', 'error'),
    ('double-bang', 'Stop!!', 'error'),
    ('retired-model-id', 'claude-2', 'error'),
    ('thinking-scaffold', 'Think step by step', 'error'),
    ('word-cap', 'at most 50 words', 'warning'),
    ('history-narrative', '(#123)', 'error'),
    ('migration-relative', 'previously', 'error'),
    ('grader-vocabulary', 'hidden tests', 'error'),
    ('missing-reason', 'Run the check.', 'warning'),
])
def test_generated_rule_kind_severity_table(slug, text, severity):
    assert select(text, slug, kind='generated_rule')[0].severity == severity


def test_severity_override_and_severity_by_kind():
    cfg = instructions_config_from_mapping({'lint': {'checks': {'caps-emphasis': {'severity': 'info', 'severity_by_kind': {'generated_rule': 'warning'}}}}}).lint
    assert select('NEVER do X and ALWAYS do Y.', 'caps-emphasis', cfg=cfg)[0].severity == 'info'
    assert select('NEVER do X.', 'caps-emphasis', cfg=cfg, kind='generated_rule')[0].severity == 'warning'


def test_reasoned_token_headings_do_not_add_density():
    text = '\n\n'.join('## IMPORTANT\n\nRun the check because it verifies the release.' for _ in range(5))
    assert not select(text, 'caps-emphasis')


def test_any_more_negation_does_not_cross_paragraphs():
    assert not select('Do not edit generated files\n\nDo we need any more files?', 'migration-relative')
