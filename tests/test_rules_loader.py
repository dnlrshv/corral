"""Rules are shared inputs; exercise precedence and file trust separately."""
import json
import re
import subprocess
import sys
from dataclasses import asdict
from importlib.resources import files
from pathlib import Path

import pytest
import yaml

from corral.cli import main
from corral.rules import (RULES_FILE_SCHEMA_V1, Layers, load_layers, load_packaged,
                          load_rules_file, overlay_path)


def rule(id="project.test", **kw):
    return {"id": id, "text": "Run checks.", "reason": "Checks demonstrate behavior.", **kw}


def write(path, rules, **extra):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"schema_version": 1, "rules": rules, **extra}))
    return path


def test_packaged_defaults_load_and_match_the_schema_file():
    packaged = load_packaged()
    assert len(packaged.rules) == 4
    assert json.loads(files("corral.rules").joinpath("schemas/rules.schema.json").read_text()) == RULES_FILE_SCHEMA_V1
    import jsonschema
    jsonschema.validate(yaml.safe_load(files("corral.rules").joinpath("defaults.yaml").read_text()), RULES_FILE_SCHEMA_V1)
    subprocess.run([sys.executable, "-c", "import sys; import corral.rules; assert not any(k.startswith('corral.execution') for k in sys.modules)"], check=True)


def test_packaged_defaults_are_generic():
    for r in load_packaged().rules:
        assert r.id.startswith("corral.") and r.reason
        assert not any(asdict(r.applies_to).values())
        assert not re.search(r"[A-Za-z0-9-]+\.(local|lan)|@", r.text + r.reason)


def test_repo_layer_replaces_a_packaged_rule_by_id_whole(tmp_path):
    id = load_packaged().rules[0].id
    path = write(tmp_path / "rules.yaml", [rule(id)])
    repo = load_rules_file(path, source="repo", workspace=tmp_path)
    merged = Layers(load_packaged(), repo, None).merged()
    assert merged[0] == repo.rules[0]
    assert len(merged) == 4


def test_overlay_replaces_repo_and_packaged(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    id = load_packaged().rules[0].id
    write(root / "rules.yaml", [rule(id, text="Repo.")])
    overlay = write(tmp_path / "overlay.yaml", [rule(id, text="Overlay.")])
    layers = load_layers(workspace=root, repo_rules_file="rules.yaml", env={"CORRAL_OVERLAY_FILE": str(overlay)})
    assert layers.merged()[0].text == "Overlay."
    assert layers.merged()[0].source == "overlay"


def test_supersedes_removes_the_named_ids(tmp_path):
    old = load_packaged().rules[0].id
    overlay = load_rules_file(write(tmp_path / "overlay", [rule(supersedes=[old])]), source="overlay")
    assert old not in [r.id for r in Layers(load_packaged(), None, overlay).merged()]


def test_disabled_record_removes_a_packaged_default(tmp_path):
    old = load_packaged().rules[0].id
    overlay = load_rules_file(write(tmp_path / "overlay", [rule(old, enabled=False)]), source="overlay")
    assert old not in [r.id for r in Layers(load_packaged(), None, overlay).merged()]


def test_duplicate_id_inside_one_layer_is_a_value_error_naming_the_file(tmp_path):
    path = write(tmp_path / "rules", [rule(), rule()])
    with pytest.raises(ValueError, match=f"{path}.*duplicate id project.test"):
        load_rules_file(path, source="repo")


def test_unknown_record_key_is_a_value_error(tmp_path):
    with pytest.raises(ValueError, match="unknown record key"):
        load_rules_file(write(tmp_path / "rules", [rule(appliez_to={})]), source="overlay")


def test_missing_overlay_is_none_and_missing_declared_repo_file_is_an_error(tmp_path):
    env = {"CORRAL_OVERLAY_FILE": str(tmp_path.parent / "absent-overlay")}
    assert load_layers(workspace=tmp_path, repo_rules_file=None, env=env).overlay is None
    with pytest.raises(ValueError, match="missing.yaml"):
        load_layers(workspace=tmp_path, repo_rules_file="missing.yaml", env=env)


@pytest.mark.parametrize("raw", ["[", "[]", "schema_version: 2\nrules: []", "schema_version: true\nrules: []", "schema_version: 1\nrules: {}"])
def test_malformed_overlay_raises_value_error_with_the_path(tmp_path, raw):
    path = tmp_path / "overlay"
    path.write_text(raw)
    with pytest.raises(ValueError, match=str(path)):
        load_layers(workspace=None, repo_rules_file=None, env={"CORRAL_OVERLAY_FILE": str(path)})


def test_overlay_inside_the_workspace_is_refused(tmp_path):
    with pytest.raises(PermissionError, match="overlay must not live"):
        load_layers(workspace=tmp_path, repo_rules_file=None, env={"CORRAL_OVERLAY_FILE": str(tmp_path / "missing")})


def test_overlay_path_honours_env_then_xdg_then_home(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert overlay_path({}) == tmp_path / ".config/corral/overlay.yaml"
    assert overlay_path({"XDG_CONFIG_HOME": str(tmp_path)}) == tmp_path / "corral/overlay.yaml"
    assert overlay_path({"XDG_CONFIG_HOME": "ignored", "CORRAL_OVERLAY_FILE": "chosen"}) == Path("chosen")


def test_applicability_matches_repo_name_or_workspace_glob_tier_phase_harness(tmp_path):
    row = rule(applies_to={"repos": ["demo-*", "*/checkout"], "tiers": ["small"], "phases": ["repair"], "harnesses": ["codex"]})
    layer = load_rules_file(write(tmp_path / "rules", [row]), source="repo")
    layers = Layers(layer, None, None)
    args = dict(repo="demo-parser", workspace="/tmp/checkout", tier="small", phase="repair", harness="codex")
    assert layers.applicable(**args) == layer.rules
    for key, value in [("tier", "standard"), ("phase", "review"), ("harness", "agy")]:
        assert not layers.applicable(**{**args, key: value})
    assert layers.applicable(**{**args, "repo": "other"})
    assert not layers.applicable(**{**args, "repo": "other", "workspace": "/tmp/other"})


def test_credential_shaped_rule_text_is_refused(tmp_path):
    with pytest.raises(PermissionError, match="credential-shaped rule text refused"):
        load_rules_file(write(tmp_path / "rules", [rule(text="api_key = 'not-a-real-secret-value'")]), source="overlay")


def test_unknown_top_level_keys_are_preserved_in_extra(tmp_path):
    extra = {"instruction_files": {"targets": ["AGENTS.md"]}}
    assert load_rules_file(write(tmp_path / "rules", [], **extra), source="overlay").extra == extra


def test_merged_order_is_stable_and_digest_is_deterministic(tmp_path):
    defaults = load_packaged()
    overlay = load_rules_file(write(tmp_path / "rules", [rule(defaults.rules[1].id), rule()]), source="overlay")
    layers = Layers(defaults, None, overlay)
    assert [r.id for r in layers.merged()] == [r.id for r in defaults.rules] + ["project.test"]
    from corral.execution.store import digest
    assert layers.digest() == digest([asdict(r) for r in layers.merged()])
    assert layers.digest() == Layers(load_packaged(), None, overlay).digest()


def test_cli_rules_show_lists_applicable_rules_and_sources(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("CORRAL_OVERLAY_FILE", str(tmp_path.parent / "absent"))
    (tmp_path / "corral.yaml").write_text("executor_brief:\n  rules_file: rules.yaml\n")
    write(tmp_path / "rules.yaml", [rule()])
    assert main(["rules", "show", "--root", str(tmp_path)]) == 0
    assert "[project.test] (repo) Run checks. — because" in capsys.readouterr().out
    assert main(["rules", "show", "--root", str(tmp_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[-1]["id"] == "project.test"
    (tmp_path / "rules.yaml").write_text("[]")
    assert main(["rules", "show", "--root", str(tmp_path)]) == 1
    assert "rules.yaml" in capsys.readouterr().err


@pytest.mark.parametrize("name", ["../escape.yaml", "/absolute.yaml", ".env", "link.yaml"])
def test_repository_path_checks(tmp_path, name):
    (tmp_path / "link.yaml").symlink_to(tmp_path / "target")
    with pytest.raises((ValueError, PermissionError)):
        load_layers(workspace=tmp_path, repo_rules_file=name, env={})
