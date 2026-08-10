"""Trusted-base instruction-governance gate tests."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from corral.governance.config import GovernanceConfig
from corral.governance.proposals import (
    check_governance,
    diff_registries,
    parse_proposal_block,
    validate_proposal_contract,
)
from corral.governance.registry import check_consistency

ROOT = Path(__file__).resolve().parents[1]


def rule(
    anchor: str = "Changes MUST use staged validation.",
    *,
    concern: str = "staged-validation",
    path: str = "config/payments.yaml",
) -> dict:
    return {
        "file": "AGENTS.md",
        "anchor": anchor,
        "concern_key": concern,
        "modality": "MUST",
        "selectors": {"paths": [path]},
        "review_by": "maintainers",
    }


def proposal(rule_id: str = "R-PAY-002", **updates) -> dict:
    value = {
        "operation": "add_rule",
        "target_tier": "core",
        "concern_key": "staged-validation",
        "review_by": "maintainers",
        "rule_ids": [rule_id],
        "evidence": [{"root_incident": "INC-101"}],
        "supersedes": [],
        "why_sharpen_is_insufficient": "The selector is a distinct service boundary.",
        "existing_rules_considered": ["R-PAY-001"],
        "control": {"type": "core"},
    }
    value.update(updates)
    return value


def governance_findings(
    *,
    base: dict | None = None,
    head: dict | None = None,
    block: dict | None = None,
    changed_paths: list[str] | None = None,
    added_lines: dict[str, list[str]] | None = None,
    base_text: str = "Changes MUST use staged validation.\n",
    config: GovernanceConfig | None = None,
):
    return check_governance(
        base_registry=base or {"R-PAY-001": rule()},
        head_registry=head or {"R-PAY-001": rule()},
        base_read_file=lambda _path: base_text,
        changed_paths=changed_paths or [],
        added_by_file=added_lines or {},
        proposal_block=block,
        proposal_errors=[],
        config=config or GovernanceConfig(instruction_globs=["AGENTS.md"]),
    )


def test_registry_head_consistency_accepts_live_anchor() -> None:
    assert check_consistency({"R-PAY-001": rule()}, lambda _path: rule()["anchor"]) == []


def test_registry_head_consistency_rejects_stale_anchor() -> None:
    findings = check_consistency({"R-PAY-001": rule()}, lambda _path: "different")
    assert findings[0].check == "consistency"
    assert "stale registry" in findings[0].message


def test_normative_signature_is_exactly_four_fields() -> None:
    base = {"R-PAY-001": rule()}
    changed_note = {"R-PAY-001": {**rule(), "note": "new", "review_by": "other"}}
    changed_selector = {
        "R-PAY-001": {**rule(), "selectors": {"paths": ["src/other.py"]}}
    }
    assert diff_registries(base, changed_note) == (set(), set(), set())
    assert diff_registries(base, changed_selector) == (set(), set(), set())
    for field in ("file", "anchor", "concern_key", "modality"):
        modified = {"R-PAY-001": {**rule(), field: f"changed-{field}"}}
        assert diff_registries(base, modified)[1] == {"R-PAY-001"}


def test_seeding_preexisting_anchor_needs_no_proposal() -> None:
    base = {"R-PAY-001": rule()}
    seeded = rule("Existing MUST remain documented.", concern="existing")
    head = {**base, "R-PAY-002": seeded}
    findings = governance_findings(
        base=base,
        head=head,
        base_text="Changes MUST use staged validation.\nExisting MUST remain documented.\n",
    )
    assert findings == []


def test_genuinely_new_rule_requires_proposal() -> None:
    head = {"R-PAY-001": rule(), "R-PAY-002": rule("New rule MUST be registered.")}
    findings = governance_findings(head=head)
    assert any("no machine-readable proposal block" in finding.message for finding in findings)


def test_unregistered_normative_line_is_rejected() -> None:
    findings = governance_findings(
        added_lines={"AGENTS.md": ["- Deployments MUST have a rollback plan."]}
    )
    assert [finding.check for finding in findings] == ["registry-entry"]


def test_narrative_line_does_not_trigger_registry_check() -> None:
    findings = governance_findings(
        added_lines={"AGENTS.md": ["Deployments must generally be understandable."]}
    )
    assert findings == []


def test_path_guard_uses_configured_globs() -> None:
    cfg = GovernanceConfig(protected_paths=["corral/governance/**"])
    findings = governance_findings(
        block={"proposals": [proposal("R-NONE-999", operation="delete")]},
        changed_paths=["corral/governance/proposals.py"],
        config=cfg,
    )
    assert any(finding.check == "path-guard" for finding in findings)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"operation": None}, "operation"),
        ({"target_tier": None}, "target_tier"),
        ({"concern_key": ""}, "missing concern_key"),
        ({"review_by": ""}, "missing review_by"),
        ({"rule_ids": []}, "rule_ids"),
        ({"evidence": []}, "evidence"),
        ({"evidence": [{}]}, "root_incident"),
        ({"why_sharpen_is_insufficient": ""}, "why_sharpen_is_insufficient"),
        ({"existing_rules_considered": None}, "existing_rules_considered"),
        ({"control": None}, "control"),
    ],
)
def test_proposal_contract_missing_fields_have_specific_errors(
    mutation: dict, message: str
) -> None:
    item = proposal()
    if "existing_rules_considered" in mutation and mutation["existing_rules_considered"] is None:
        item.pop("existing_rules_considered")
    else:
        item.update(mutation)
    errors = validate_proposal_contract({"proposals": [item]})
    assert any(message in error for error in errors)


def test_proposal_contract_accepts_complete_add_rule() -> None:
    assert validate_proposal_contract({"proposals": [proposal()]}) == []


def test_duplicate_root_incidents_rejected() -> None:
    item = proposal(
        evidence=[{"root_incident": "INC-1"}, {"root_incident": "INC-1"}]
    )
    assert any("DISTINCT" in error for error in validate_proposal_contract({"proposals": [item]}))


def test_max_three_proposals_enforced() -> None:
    items = [proposal(f"R-PAY-00{index}") for index in range(2, 6)]
    errors = validate_proposal_contract({"proposals": items})
    assert any("exceeds the cap of 3" in error for error in errors)


def test_near_duplicate_requires_supersedes() -> None:
    new = rule("A new service MUST stage validation.")
    head = {"R-PAY-001": rule(), "R-PAY-002": new}
    findings = governance_findings(
        head=head, block={"proposals": [proposal()]}
    )
    assert any(finding.check == "supersede" for finding in findings)


def test_near_duplicate_accepts_declared_supersede() -> None:
    new = rule("A new service MUST stage validation.")
    head = {"R-PAY-001": rule(), "R-PAY-002": new}
    item = proposal(supersedes=["R-PAY-001"])
    findings = governance_findings(head=head, block={"proposals": [item]})
    assert findings == []


def test_nonoverlapping_same_concern_is_not_near_duplicate() -> None:
    new = rule(
        "Another service MUST stage validation.", path="src/api/orders.py"
    )
    head = {"R-PAY-001": rule(), "R-PAY-002": new}
    findings = governance_findings(head=head, block={"proposals": [proposal()]})
    assert not any(finding.check == "supersede" for finding in findings)


def test_exact_diff_to_proposal_mapping_rejects_extra_id() -> None:
    findings = governance_findings(block={"proposals": [proposal("R-PAY-999")]})
    assert any("does not change" in finding.message for finding in findings)


def test_multiple_proposal_blocks_rejected() -> None:
    body = "```yaml\nproposals: []\n```\n```yml\nproposals: []\n```"
    block, errors = parse_proposal_block(body)
    assert block is None
    assert "exactly one" in errors[0]


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, text=True, capture_output=True, check=True
    )
    return result.stdout.strip()


def test_ci_gate_executes_base_validator_not_malicious_head(
    tmp_path: Path,
) -> None:
    """Base package judges a head that edits both registry and validator."""
    repo = tmp_path / "repo"
    repo.mkdir()
    shutil.copytree(
        ROOT / "corral",
        repo / "corral",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    (repo / "corral.yaml").write_text(
        "governance:\n  instruction_globs: [AGENTS.md]\n"
    )
    (repo / "AGENTS.md").write_text("- Changes MUST use staged validation.\n")
    (repo / "instruction_rules.yaml").write_text(
        """schema_version: 1
rules:
  R-PAY-001:
    file: AGENTS.md
    anchor: Changes MUST use staged validation.
    concern_key: staged-validation
    modality: MUST
    selectors: {paths: [config/payments.yaml]}
    review_by: maintainers
"""
    )
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "tests@example.invalid")
    _git(repo, "config", "user.name", "Corral Tests")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    base_ref = _git(repo, "rev-parse", "HEAD")

    with (repo / "AGENTS.md").open("a") as handle:
        handle.write("- New changes MUST preserve rollback evidence.\n")
    with (repo / "instruction_rules.yaml").open("a") as handle:
        handle.write(
            """  R-PAY-002:
    file: AGENTS.md
    anchor: New changes MUST preserve rollback evidence.
    concern_key: rollback-evidence
    modality: MUST
    selectors: {paths: [config/payments.yaml]}
    review_by: maintainers
"""
        )
    malicious = repo / "corral" / "governance" / "proposals.py"
    text = malicious.read_text()
    needle = "    cfg = config or GovernanceConfig()\n    findings: list[Finding] = []\n    added, changed, removed = diff_registries(base_registry, head_registry)"
    assert needle in text
    malicious.write_text(text.replace(needle, "    return []", 1))
    # Canary: sitecustomize.py is imported automatically at interpreter
    # startup from any sys.path entry, so if the nested BASE validator ever
    # sees a HEAD-controlled directory (e.g. via an inherited PYTHONPATH)
    # this file executes and records the marker env it ran under.
    (repo / "sitecustomize.py").write_text(
        "import os, pathlib\n"
        "marker = os.environ.get('CORRAL_GOVERNANCE_BASE_EXECUTED', 'outer')\n"
        "pathlib.Path(__file__).resolve().with_name(\n"
        "    f'sitecustomize-canary-{marker}').write_text(marker)\n"
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "malicious head")
    head_ref = _git(repo, "rev-parse", "HEAD")

    command = [
        sys.executable,
        "-c",
        "import sys; from corral.cli import main; raise SystemExit(main(sys.argv[1:]))",
        "governance",
        "check",
        "--root",
        str(repo),
        "--base-ref",
        base_ref,
        "--head-ref",
        head_ref,
    ]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(repo)

    # Normal CI topology materializes BASE and rejects the same HEAD.
    trusted = subprocess.run(
        command, cwd=repo, env=environment, text=True, capture_output=True, check=False
    )
    assert trusted.returncode == 1, trusted.stderr
    assert "no machine-readable proposal block" in trusted.stderr
    # The nested BASE validator must never have executed HEAD code: the
    # canary keyed by the base-ref marker (set only in the nested child)
    # must not exist. The launcher itself runs HEAD code by construction in
    # this topology and may write the "outer" canary.
    assert not (repo / f"sitecustomize-canary-{base_ref}").exists()

    # Prove the malicious HEAD would accept if explicitly (and unsafely)
    # treated as trusted validator code.
    unsafe_env = {**environment, "CORRAL_GOVERNANCE_BASE_EXECUTED": base_ref}
    unsafe = subprocess.run(
        command, cwd=repo, env=unsafe_env, text=True, capture_output=True, check=False
    )
    assert unsafe.returncode == 0, unsafe.stderr
