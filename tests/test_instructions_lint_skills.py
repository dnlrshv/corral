"""Skill description validation uses the routing frontmatter only."""
import pytest
import yaml

from corral.instructions.lint import default_lint_config, lint_text


def findings(description, *, include=True):
    data = {'name': 'review'}
    if include:
        data['description'] = description
    text = '---\n' + yaml.safe_dump(data, sort_keys=False) + '---\nReview changes because the checklist names verification.\n'
    return lint_text(text, path='.claude/skills/review/SKILL.md', kind='skill', config=default_lint_config())


@pytest.mark.parametrize('description,severities', [
    (' '.join(f'"case {i}"' for i in range(5)), []),
    (' '.join(f'"case {i}"' for i in range(6)), ['warning']),
    ((' '.join(f'"case {i}"' for i in range(4))).ljust(650, 'a'), ['warning']),
    ('a' * 1024, []),
    ('a' * 1025, ['error']),
])
def test_trigger_bloat_thresholds(description, severities):
    found = findings(description)
    assert [f.severity for f in found] == severities
    assert all(f.check == 'trigger-bloat' and f.line == 3 for f in found)


@pytest.mark.parametrize('value', [None, 1, ['review']])
def test_skill_missing_description_is_error(value):
    assert findings(value)[0].severity == 'error'
    assert findings(None, include=False)[0].check == 'trigger-bloat'


def test_description_faults_do_not_raise_on_malformed_frontmatter():
    found = lint_text('---\ndescription: [\n---\n', path='SKILL.md', kind='skill', config=default_lint_config())
    assert len(found) == 1 and found[0].check == 'trigger-bloat'


def test_urgency_in_description_is_exempt_from_prose_checks():
    assert not findings('MUST use this skill. NEVER skip it. IMPORTANT: "case one" "case two" "case three"')
