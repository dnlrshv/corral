"""Filesystem references are checked by reading, never by executing commands."""
import subprocess

from corral.instructions.lint import default_lint_config, lint_paths, lint_text


def write(root, path, text=''):
    p = root / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def refs(root, text, path='AGENTS.md', cfg=None):
    return lint_text(text, path=path, kind='instruction', config=cfg or default_lint_config(), root=root)


def test_dead_path_import_and_link_are_errors_code_is_warning(tmp_path):
    found = refs(tmp_path, '@docs/missing.md\n\n[x](docs/gone.md) and `src/old.py`')
    assert [(f.check, f.severity) for f in found] == [('dead-path', 'error'), ('dead-path', 'error'), ('dead-path', 'warning')]


def test_dead_path_resolves_relative_to_file_root_and_skill_dir(tmp_path):
    write(tmp_path, 'docs/local.md')
    write(tmp_path, 'src/app.py')
    write(tmp_path, '.claude/skills/review/helper.py')
    assert not refs(tmp_path, '[x](local.md) `src/app.py`', 'docs/AGENTS.md')
    assert not refs(tmp_path, '[x](helper.py)', '.claude/skills/review/docs/AGENTS.md')
    write(tmp_path, 'docs/my file.md')
    assert not refs(tmp_path, '[x](<docs/my file.md>)')
    write(tmp_path, 'docs/guide.md')
    assert not refs(tmp_path, '[x](docs/guide.md#section?query)')


def test_dead_path_skips_placeholders_globs_urls_home_absolute_refs(tmp_path):
    text = '`tests/test_<name>.py` `~/.config/x.yaml` `/etc/hosts` `origin/main` `owner/repo` `https://x/y.md` `src/**/*.py` `src/${NAME}.py` `docs\\guide.md` ![image](docs/missing.md)'
    assert not refs(tmp_path, text)


def test_dead_path_skips_git_ignored_paths(tmp_path):
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    write(tmp_path, '.gitignore', 'build/\n*.parquet\n')
    text = '`build/out.json` `code_map/edges.parquet`'
    assert not refs(tmp_path, text)
    cfg = default_lint_config()
    cfg.checks['dead-path'].options['check_ignored'] = False
    assert len(refs(tmp_path, text, cfg=cfg)) == 2


def test_dead_path_skips_creation_sentences_and_shell_outputs(tmp_path):
    assert not refs(tmp_path, 'create `NOTES.md` and generate `build/report.json`.\n\n```sh\nreport > build/report.json\nreport --output build/a.json\nmkdir newdir/\ntouch docs/new.md\n```')
    assert len(refs(tmp_path, 'Read `src/old.py`. Create `src/new.py`.')) == 1


def test_dead_path_never_follows_symlinks(tmp_path):
    (tmp_path / 'linked').symlink_to(tmp_path / 'absent', target_is_directory=True)
    assert not refs(tmp_path, '[x](linked/docs/missing.md)')
    # Explicit input beneath a symlink is also skipped, before any target read.
    report = lint_paths([tmp_path / 'linked/AGENTS.md'], config=default_lint_config(), root=tmp_path)
    assert report.files_checked == 0 and 'symbolic link' in report.input_errors[0]


def test_dead_path_outside_root_not_probed(tmp_path):
    assert not refs(tmp_path, '[outside](../../outside.md) `../../outside/file.py`')


def test_dead_command_make_targets(tmp_path):
    write(tmp_path, 'Makefile', '.PHONY: test lint\ntest lint:\n\t@echo ok\ninclude extra.mk\n')
    write(tmp_path, 'extra.mk', 'build: ;\n')
    write(tmp_path, 'sub/Makefile', 'x:\n')
    assert not refs(tmp_path, '`make test V=1 && make -j 2 build`\n`cd sub && make x`')
    found = refs(tmp_path, '`make deploy`')
    assert len(found) == 1 and found[0].check == 'dead-command' and found[0].severity == 'error'
    assert found[0].column == 7
    assert not refs(tmp_path, '`make -C sub x`')
    assert not refs(tmp_path, '`gmake --directory=sub --file=Makefile x`')


def test_dead_command_unverifiable_makefile_produces_nothing(tmp_path):
    for text in ['$(NAME):\n', 'include missing.mk\n', 'include ${CONFIG}\n', 'target-${NAME}:\n']:
        write(tmp_path, 'Makefile', text)
        assert not refs(tmp_path, '`make anything`')
    (tmp_path / 'Makefile').unlink()
    assert not refs(tmp_path, '`make anything`')


def test_dead_command_package_scripts(tmp_path):
    write(tmp_path, 'package.json', '{"scripts":{"test":"echo test","typecheck":"echo types"}}')
    assert not refs(tmp_path, '`npm run typecheck` `npm test` `npm install`')
    for cmd in ('npm run', 'npm run-script', 'pnpm run', 'yarn run', 'bun run'):
        found = refs(tmp_path, f'`{cmd} e2e`')
        assert len(found) == 1 and found[0].check == 'dead-command'
    assert not refs(tmp_path, '`npm run ${SCRIPT}`')
    write(tmp_path, 'sub/package.json', '{"scripts":{"local":"echo local"}}')
    assert not refs(tmp_path, '`npm --prefix sub run local`')
    assert not refs(tmp_path, '`cd sub && npm run local`')
    assert not refs(tmp_path, '`npm run local`', 'sub/AGENTS.md')
    write(tmp_path, 'package.json', '{bad')
    assert not refs(tmp_path, '`npm run e2e`')
    (tmp_path / 'package.json').unlink()
    assert not refs(tmp_path, '`npm run e2e`')


def test_reference_checks_skipped_without_root():
    assert not lint_text('@missing.md\n`make deploy`', path='AGENTS.md', kind='instruction', config=default_lint_config())


def test_git_unavailable_degrades_gracefully(tmp_path, monkeypatch):
    def unavailable(*args, **kwargs):
        raise FileNotFoundError('git')
    monkeypatch.setattr(subprocess, 'run', unavailable)
    write(tmp_path, 'AGENTS.md', '[x](docs/missing.md)')
    report = lint_paths([], config=default_lint_config(), root=tmp_path)
    assert len(report.input_errors) == 1
    assert not report.findings


def test_lint_never_executes_commands(tmp_path, monkeypatch):
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    write(tmp_path, 'AGENTS.md', '```sh\nrm -rf src/missing.py && curl https://example.invalid\n```\n`make deploy`')
    original = subprocess.run
    calls = []
    def recording(argv, **kwargs):
        calls.append(argv)
        assert argv[0] == 'git' and argv[3] in ('rev-parse', 'ls-files', 'check-ignore')
        assert not kwargs.get('shell')
        return original(argv, **kwargs)
    monkeypatch.setattr(subprocess, 'run', recording)
    lint_paths([], config=default_lint_config(), root=tmp_path)
    assert {a[3] for a in calls} == {'rev-parse', 'ls-files', 'check-ignore'}


def test_command_columns_skip_option_values(tmp_path):
    write(tmp_path, 'sub/Makefile', 'test:\n')
    found = refs(tmp_path, '`make -C sub sub`')
    assert len(found) == 1 and found[0].column == 14
    write(tmp_path, 'sub/package.json', '{"scripts":{"test":"echo test"}}')
    found = refs(tmp_path, '`npm --prefix sub run sub`')
    assert len(found) == 1 and found[0].column == 23


def test_make_variables_in_prerequisites_are_unverifiable(tmp_path):
    write(tmp_path, 'Makefile', 'test: $(DEPS)\n')
    assert not refs(tmp_path, '`make deploy`')


def test_markdown_link_titles_and_image_destinations_are_not_paths(tmp_path):
    write(tmp_path, 'docs/my file.md')
    write(tmp_path, 'docs/guide.md')
    assert not refs(tmp_path, '[guide](docs/guide.md "Reference title") [space](<docs/my file.md> "Reference title") ![image](docs/missing.md)')
    from corral.instructions.text_model import TextModel
    doc = TextModel('![image](NEVER/ALWAYS.md "IMPORTANT")')
    assert 'NEVER' not in doc.prose and 'IMPORTANT' not in doc.prose
    assert not doc.links


def test_package_command_message_includes_run(tmp_path):
    write(tmp_path, 'package.json', '{"scripts":{}}')
    assert '"npm run missing"' in refs(tmp_path, '`npm run missing`')[0].message
