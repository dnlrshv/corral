"""Validated configuration for instruction tools."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

DEFAULT_LINT_INCLUDE = [
    'AGENTS.md', '**/AGENTS.md', 'CLAUDE.md', '**/CLAUDE.md', 'CLAUDE.local.md',
    'GEMINI.md', '**/GEMINI.md', '.cursorrules', '.cursor/rules/**',
    '.github/copilot-instructions.md', '.github/instructions/**',
    '.claude/skills/**/SKILL.md', '.agents/skills/**/SKILL.md',
    '.claude/commands/**', '.claude/agents/**', '.claude/rules/**',
]
DEFAULT_LINT_EXCLUDE = [
    '**/node_modules/**', '**/.venv/**', '**/venv/**', '.git/**',
    '.claude/worktrees/**', '**/vendor/**', '**/site-packages/**',
]
SEVERITIES = ('info', 'warning', 'error')
KINDS = ('instruction', 'skill', 'generated_rule')


@dataclass
class CheckConfig:
    enabled: bool = True
    severity: str | None = None
    severity_by_kind: dict[str, str] = field(default_factory=dict)
    exclude: list[str] = field(default_factory=list)
    options: dict[str, object] = field(default_factory=dict)


def default_check_configs() -> dict[str, CheckConfig]:
    from .lint_checks import CHECKS
    return {slug: CheckConfig(options=spec.validate_options({})) for slug, spec in CHECKS.items()}


@dataclass
class LintConfig:
    include: list[str] = field(default_factory=lambda: list(DEFAULT_LINT_INCLUDE))
    extend_include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=lambda: list(DEFAULT_LINT_EXCLUDE))
    use_governance_globs: bool = True
    governance_globs: list[str] = field(default_factory=list)
    max_file_bytes: int = 1_048_576
    min_severity: str = 'warning'
    checks: dict[str, CheckConfig] = field(default_factory=default_check_configs)
    registered_anchors: dict[str, tuple[str, ...]] = field(default_factory=dict)
    registry: str = 'instruction_rules.yaml'


@dataclass
class InstructionsConfig:
    lint: LintConfig = field(default_factory=LintConfig)


def fail(key: str, description: str) -> None:
    raise ValueError(f'corral.yaml: instructions.{key} {description}')


def strings(value: Any, key: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(s, str) for s in value):
        fail(key, 'must be a list of strings')
    return list(value)


def mapping(value: Any, key: str) -> dict:
    if not isinstance(value, dict):
        fail(key, 'must be a mapping')
    return value


def validate_lint_config(cfg: LintConfig) -> None:
    """Also validate programmatically constructed configurations at the API boundary."""
    from .lint_checks import CHECKS
    if not isinstance(cfg, LintConfig):
        fail('lint', 'must be a LintConfig')
    for key in ('include', 'extend_include', 'exclude', 'governance_globs'):
        strings(getattr(cfg, key), f'lint.{key}')
    if not isinstance(cfg.use_governance_globs, bool):
        fail('lint.use_governance_globs', 'must be a boolean')
    if isinstance(cfg.max_file_bytes, bool) or not isinstance(cfg.max_file_bytes, int) or cfg.max_file_bytes < 1:
        fail('lint.max_file_bytes', 'must be a positive integer')
    if cfg.min_severity not in SEVERITIES:
        fail('lint.min_severity', 'must be info, warning or error')
    mapping(cfg.checks, 'lint.checks')
    for slug, check in cfg.checks.items():
        key = f'lint.checks.{slug}'
        if slug not in CHECKS:
            fail(key, 'is an unknown check')
        if not isinstance(check, CheckConfig):
            fail(key, 'must be a CheckConfig')
        if not isinstance(check.enabled, bool):
            fail(key + '.enabled', 'must be a boolean')
        if check.severity is not None and check.severity not in SEVERITIES:
            fail(key + '.severity', 'must be null, info, warning or error')
        mapping(check.severity_by_kind, key + '.severity_by_kind')
        if any(k not in KINDS or v not in SEVERITIES for k, v in check.severity_by_kind.items()):
            fail(key + '.severity_by_kind', 'must map file kinds to severities')
        strings(check.exclude, key + '.exclude')
        try:
            CHECKS[slug].validate_options(mapping(check.options, key))
        except ValueError as exc:
            if str(exc).startswith('corral.yaml: instructions.'):
                raise
            fail(key, str(exc))


def instructions_config_from_mapping(raw: dict | None) -> InstructionsConfig:
    raw = {} if raw is None else mapping(raw, '')
    for key in raw.keys() - {'lint'}:
        fail(str(key), 'is an unknown key')
    cfg = InstructionsConfig()
    lint = mapping(raw.get('lint', {}), 'lint')
    for key, value in lint.items():
        if key in ('include', 'extend_include', 'exclude'):
            setattr(cfg.lint, key, strings(value, 'lint.' + key))
        elif key in ('use_governance_globs', 'max_file_bytes', 'min_severity'):
            setattr(cfg.lint, key, value)
        elif key == 'checks':
            for slug, block in mapping(value, 'lint.checks').items():
                if slug not in cfg.lint.checks:
                    fail(f'lint.checks.{slug}', 'is an unknown check')
                block = mapping(block, f'lint.checks.{slug}')
                check = cfg.lint.checks[slug]
                for option, option_value in block.items():
                    if option in ('enabled', 'severity', 'severity_by_kind', 'exclude'):
                        setattr(check, option, option_value)
                    else:
                        check.options[option] = option_value
        else:
            fail('lint.' + str(key), 'is an unknown key')
    validate_lint_config(cfg.lint)
    return cfg
