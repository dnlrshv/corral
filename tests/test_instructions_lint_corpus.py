"""A fixed corpus catches both missed signals and new false positives."""
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

from corral.cli import main
from corral.config import load_config
from corral.instructions.lint import lint_paths

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'tests/fixtures/instructions_lint/repo'


@pytest.fixture
def corpus(tmp_path):
    root = tmp_path / 'repo'
    shutil.copytree(FIXTURE, root)
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    subprocess.run(['git', '-C', str(root), 'add', '-A'], check=True)
    config = load_config(root / 'corral.yaml')
    cfg = config.instructions.lint
    cfg.min_severity = "info"
    cfg.registry = config.governance.registry
    cfg.registry_config = config.governance
    cfg.governance_globs = config.governance.instruction_globs
    return root, lint_paths([], config=cfg, root=root)


def test_corpus_matches_expected_manifest(corpus):
    root, report = corpus
    entries = yaml.safe_load((root / 'expected.yaml').read_text())['findings']
    expected = {(e['path'], e['check'], e['line'], e['severity']) for e in entries}
    actual = {(f.path, f.check, f.line, f.severity) for f in report.findings}
    assert actual == expected
    assert not report.input_errors
    assert report.suppressed == 1


def test_good_fixtures_have_no_warnings_or_errors(corpus):
    _, report = corpus
    assert not [f for f in report.findings if f.path.startswith('good/') and f.severity in ('warning', 'error')]


def test_shipped_examples_lint_clean(capsys):
    assert main(['instructions', 'lint', '--root', str(ROOT), '--config', str(ROOT / 'examples/demo/corral.yaml'), str(ROOT / 'examples/governance/instructions')]) == 0
    assert 'instructions lint:' in capsys.readouterr().out


def test_pre_commit_hook_declared():
    hooks = yaml.safe_load((ROOT / '.pre-commit-hooks.yaml').read_text())
    hook = next(h for h in hooks if h['id'] == 'instructions-lint')
    assert hook['pass_filenames'] is True and hook['types'] == ['text']
    assert hook['entry'].startswith('corral-instructions-lint --only-configured')
    assert 'corral-instructions-lint = "corral.instructions.cli:lint_main"' in (ROOT / 'pyproject.toml').read_text()
