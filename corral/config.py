"""Project configuration for corral.

corral reads its settings from a ``corral.yaml`` file, normally placed at
the repository root. Every key is optional; anything missing falls back to
the defaults defined here. See ``corral.example.yaml`` in the repository
root for a documented starting point.

Layout::

    codemap:
      output_dir: code_map
      scan_dirs: ["."]
      skip_dirs: [".venv", "data", "tests/fixtures", ".claude/worktrees"]
    lineage:
      output: code_map/edges.parquet
      pipeline_yaml: config/data_pipeline.yaml
      yaml_manifest_schema: {sources: table, groups: target_table}
      config_loaders: {}
      config_loader_key_prefixes: {}
    hooks:
      surfaces: surfaces.yaml
      magic_numbers:
        constants: null
        allowlist: .magic-number-allowlist.yaml
        # scan_dirs omitted: inherit codemap.scan_dirs
    preflight:
      gotchas: agent_memory/gotchas.json
      model: claude-haiku-4-5-20251001
      max_tokens: 1500
      quota_status_file: null
      output: null
      recognized_modules: null
      workflow_kinds: {}
    telemetry:
      spool_dir: null
      rollup_output_dir: agent_telemetry
      lookback_days: 7
      required_ci_contexts: ["lint", "test"]
    seats_file: seats.yaml
    retro:
      drafter_seat: retro-drafter
      verifier_seats: [retro-verifier]
      require_distinct_provider: true
      verification_timeout_s: 300
      gotcha_unavailable_policy: proceed-unverified
      instruction_unavailable_policy: fail-closed
      repository: null
      issue_sink: stdout
      fixup_glob: null
      gotchas_path: agent_memory/gotchas.json
      refinements_path: agent_memory/refinements.jsonl
      confidence_threshold: 0.70
      max_tokens: 900
      drafting_timeout_s: 300
      allowed_severities: [info, P2, P1, P0]
      severe_severities: []
      github:
        assignee: null
        gotcha_label: agent-gotcha
        timeout_s: 30
      evidence:
        min_root_incidents: 2
        max_candidates: 3
        ignored_title_patterns: ["weekly gotcha retrospective", "weekly agent rollup"]
        ignored_path_globs:
          ["CLAUDE.md", "AGENTS.md", "README.md", "CLAUDE/*", "wiki/*",
           "agent_telemetry/*", "agent_memory/gotchas.json"]
      bridge:
        memory_roots: []
        run_artifact_roots: []
      proposals:
        enabled: false
        max: 3
        min_incidents: 2
        target_globs: []
    governance:
      reviewer: null
      staleness:
        retain_rate: 0.20
        retain_days: 90
        retain_workflow_count: 2
        demote_rate: 0.10
        demote_days: 180
        min_sessions: 30
        demote_target_glob: null
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from corral.governance.config import GovernanceConfig, governance_config_from_mapping
from corral.preflight.auth import DEFAULT_PREFLIGHT_MODEL

CONFIG_FILENAME = "corral.yaml"

DEFAULT_CODEMAP_OUTPUT_DIR = "code_map"
DEFAULT_CODEMAP_SCAN_DIRS = (".",)
DEFAULT_CODEMAP_SKIP_DIRS = (".venv", "data", "tests/fixtures", ".claude/worktrees")
DEFAULT_LINEAGE_OUTPUT = "code_map/edges.parquet"
DEFAULT_PIPELINE_YAML = "config/data_pipeline.yaml"
#: Section-name -> table-key mapping for the pipeline manifest YAML.
DEFAULT_YAML_MANIFEST_SCHEMA = {"sources": "table", "groups": "target_table"}
DEFAULT_SURFACES_PATH = "surfaces.yaml"
DEFAULT_MAGIC_NUMBER_ALLOWLIST = ".magic-number-allowlist.yaml"
DEFAULT_GOTCHAS_PATH = "agent_memory/gotchas.json"
DEFAULT_PREFLIGHT_MAX_TOKENS = 1500
DEFAULT_TELEMETRY_ROLLOUT_OUTPUT_DIR = "agent_telemetry"
DEFAULT_TELEMETRY_LOOKBACK_DAYS = 7
DEFAULT_REQUIRED_CI_CONTEXTS = ("lint", "test")
DEFAULT_SEATS_FILE = "seats.yaml"
DEFAULT_RETRO_DRAFTER_SEAT = "retro-drafter"
DEFAULT_RETRO_VERIFIER_SEATS = ("retro-verifier",)
DEFAULT_RETRO_VERIFICATION_TIMEOUT_S = 300
DEFAULT_GOTCHA_UNAVAILABLE_POLICY = "proceed-unverified"
DEFAULT_INSTRUCTION_UNAVAILABLE_POLICY = "fail-closed"
DEFAULT_RETRO_ISSUE_SINK = "stdout"
DEFAULT_RETRO_GOTCHAS_PATH = "agent_memory/gotchas.json"
DEFAULT_RETRO_REFINEMENTS_PATH = "agent_memory/refinements.jsonl"
DEFAULT_RETRO_CONFIDENCE_THRESHOLD = 0.70
DEFAULT_RETRO_PROPOSALS_MAX = 3
DEFAULT_RETRO_PROPOSALS_MIN_INCIDENTS = 2
DEFAULT_RETRO_MAX_TOKENS = 900
DEFAULT_RETRO_DRAFTING_TIMEOUT_S = 300
DEFAULT_RETRO_ALLOWED_SEVERITIES = ("info", "P2", "P1", "P0")
DEFAULT_RETRO_GOTCHA_LABEL = "agent-gotcha"
DEFAULT_RETRO_GH_TIMEOUT_S = 30
DEFAULT_RETRO_MIN_ROOT_INCIDENTS = 2
DEFAULT_RETRO_MAX_CANDIDATES = 3


@dataclass
class CodemapConfig:
    """Settings for ``corral codemap build`` and ``corral codemap query``."""

    #: Directory that receives ``imports.parquet`` / ``symbols.parquet``.
    output_dir: str = DEFAULT_CODEMAP_OUTPUT_DIR
    #: Repo-root-relative directories to scan; ``["."]`` walks the whole tree.
    scan_dirs: list[str] = field(default_factory=lambda: list(DEFAULT_CODEMAP_SCAN_DIRS))
    #: Repo-root-relative directories that are never scanned.
    skip_dirs: list[str] = field(default_factory=lambda: list(DEFAULT_CODEMAP_SKIP_DIRS))


@dataclass
class LineageConfig:
    """Settings for ``corral lineage build``."""

    #: Where ``edges.parquet`` ends up (build writes into the parent dir).
    #: ``corral codemap query`` reads lineage edges from this exact path.
    output: str = DEFAULT_LINEAGE_OUTPUT
    #: True when ``output`` was set explicitly in ``corral.yaml``. The query
    #: layer treats a missing edges file at a configured location as an
    #: error instead of silently continuing without lineage edges.
    output_configured: bool = False
    #: Pipeline manifest YAML declaring ``module:entrypoint -> table`` writes.
    pipeline_yaml: str = DEFAULT_PIPELINE_YAML
    #: Section-name -> table-key mapping for the pipeline manifest YAML.
    #: Each mapped section contributes ``writes_table`` edges, reading the
    #: table name from the given key of every entry.
    yaml_manifest_schema: dict[str, str] = field(
        default_factory=lambda: dict(DEFAULT_YAML_MANIFEST_SCHEMA)
    )
    #: Config-loader function name -> config file path recorded as the edge
    #: target for ``reads_config`` edges. Empty by default: no loader call
    #: produces edges until the project declares its loaders here.
    config_loaders: dict[str, str] = field(default_factory=dict)
    #: Optional loader name -> key prefix the wrapper loader already
    #: traversed (e.g. a loader returning ``cfg["metrics"]`` maps to
    #: ``"metrics"``).
    config_loader_key_prefixes: dict[str, str] = field(default_factory=dict)


@dataclass
class MagicNumbersConfig:
    """Settings for ``corral hooks magic-numbers``."""

    #: Repo-root-relative constants module. With no configured path, the
    #: membership lint reports that it was skipped and exits successfully.
    constants: str | None = None
    #: Repo-root-relative allowlist YAML.
    allowlist: str = DEFAULT_MAGIC_NUMBER_ALLOWLIST
    #: Directories to scan. ``None`` means inherit ``codemap.scan_dirs``.
    scan_dirs: list[str] | None = None


@dataclass
class HooksConfig:
    """Settings shared by corral's enforcement hooks."""

    #: Repo-root-relative registry used by both surface hooks.
    surfaces: str = DEFAULT_SURFACES_PATH
    magic_numbers: MagicNumbersConfig = field(default_factory=MagicNumbersConfig)


@dataclass
class PreflightConfig:
    """Settings for ``corral preflight`` and ``corral memory validate``."""

    #: Repo-root-relative gotcha registry consumed by preflight briefs and
    #: validated by ``corral memory validate``.
    gotchas: str = DEFAULT_GOTCHAS_PATH
    #: Anthropic model ID for LLM brief generation (canonical literal lives
    #: in :mod:`corral.preflight.auth`).
    model: str = DEFAULT_PREFLIGHT_MODEL
    #: Maximum output tokens for the LLM call.
    max_tokens: int = DEFAULT_PREFLIGHT_MAX_TOKENS
    #: Optional repo-local quota snapshot file. When unset or the file is
    #: absent, quota telemetry is skipped silently.
    quota_status_file: str | None = None
    #: Default output file for ``corral preflight``; ``None`` means stdout.
    output: str | None = None
    #: Top-level modules under which the LLM may propose NEW files during
    #: brief post-validation. ``None`` derives the set from the code-map
    #: artifacts (see :func:`corral.preflight.brief_validation.
    #: recognized_modules_from_code_map`).
    recognized_modules: list[str] | None = None
    #: CI workflow-name -> workflow-kind mapping used to match gotcha
    #: ``workflow_kinds`` against ``$GITHUB_WORKFLOW``. Empty by default:
    #: unrecognized or missing names never filter on workflow kind.
    workflow_kinds: dict[str, str] = field(default_factory=dict)


@dataclass
class TelemetryConfig:
    """Settings for ``corral telemetry`` capture, rollup, and CI outcomes."""

    #: Session-record spool directory for the Stop-hook capture. ``None``
    #: defers to ``$CORRAL_TELEMETRY_DIR``, else the XDG-style default
    #: ``~/.cache/corral/telemetry``.
    spool_dir: str | None = None
    #: Directory (relative to the repository root) that receives the weekly
    #: ``rollup_<YYYY-Www>.parquet`` files.
    rollup_output_dir: str = DEFAULT_TELEMETRY_ROLLOUT_OUTPUT_DIR
    #: Artifact lookback window for the rollup, in days.
    lookback_days: int = DEFAULT_TELEMETRY_LOOKBACK_DAYS
    #: Branch-protection required status checks used by CI-outcome
    #: reconstruction. Per-repository CI configuration; the defaults are a
    #: common minimal pair.
    required_ci_contexts: list[str] = field(
        default_factory=lambda: list(DEFAULT_REQUIRED_CI_CONTEXTS)
    )


@dataclass
class RetroConfig:
    """Settings for retrospective drafting and independent verification."""

    drafter_seat: str = DEFAULT_RETRO_DRAFTER_SEAT
    verifier_seats: list[str] = field(
        default_factory=lambda: list(DEFAULT_RETRO_VERIFIER_SEATS)
    )
    require_distinct_provider: bool = True
    verification_timeout_s: int = DEFAULT_RETRO_VERIFICATION_TIMEOUT_S
    gotcha_unavailable_policy: str = DEFAULT_GOTCHA_UNAVAILABLE_POLICY
    instruction_unavailable_policy: str = DEFAULT_INSTRUCTION_UNAVAILABLE_POLICY
    #: GitHub repository (``owner/name``) the retrospective mines. No default:
    #: adopters must opt in by configuring it before ``corral retro run``.
    repository: str | None = None
    #: Where severe-candidate review issues go: ``github`` (file via gh),
    #: ``stdout`` (render only, the default), or ``off``.
    issue_sink: str = DEFAULT_RETRO_ISSUE_SINK
    #: Repo-root-relative glob locating committed fix-up pair parquets.
    #: ``None`` derives ``<telemetry.rollup_output_dir>/fixup_*.parquet``.
    fixup_glob: str | None = None
    #: Repo-root-relative gotcha registry written by ``corral retro run``.
    gotchas_path: str = DEFAULT_RETRO_GOTCHAS_PATH
    #: Repo-root-relative refinement ledger (audit records + revert input).
    refinements_path: str = DEFAULT_RETRO_REFINEMENTS_PATH
    confidence_threshold: float = DEFAULT_RETRO_CONFIDENCE_THRESHOLD
    max_tokens: int = DEFAULT_RETRO_MAX_TOKENS
    drafting_timeout_s: int = DEFAULT_RETRO_DRAFTING_TIMEOUT_S
    #: Severity vocabulary offered to the drafter.
    allowed_severities: list[str] = field(
        default_factory=lambda: list(DEFAULT_RETRO_ALLOWED_SEVERITIES)
    )
    #: Severities that trigger immediate review-issue filing. Default empty:
    #: no issue filing unless adopters opt in.
    severe_severities: list[str] = field(default_factory=list)
    github: "RetroGithubConfig" = field(default_factory=lambda: RetroGithubConfig())
    evidence: "RetroEvidenceConfig" = field(default_factory=lambda: RetroEvidenceConfig())
    bridge: "RetroBridgeConfig" = field(default_factory=lambda: RetroBridgeConfig())
    proposals: "RetroProposalsConfig" = field(default_factory=lambda: RetroProposalsConfig())


@dataclass
class RetroGithubConfig:
    """GitHub-side conventions for ``corral retro run`` issue filing."""

    #: Issue assignee; ``None`` omits the flag entirely.
    assignee: str | None = None
    #: Label used for open-issue dedup and for filed review issues.
    gotcha_label: str = DEFAULT_RETRO_GOTCHA_LABEL
    #: Per-gh-invocation timeout in seconds.
    timeout_s: int = DEFAULT_RETRO_GH_TIMEOUT_S


@dataclass
class RetroEvidenceConfig:
    """Bounds for evidence mining (anti-Goodhart defaults from the source)."""

    #: Minimum DISTINCT ROOT INCIDENTS before a group merits a seat call.
    min_root_incidents: int = DEFAULT_RETRO_MIN_ROOT_INCIDENTS
    #: Cap on candidates drafted per weekly run, highest-evidence first.
    max_candidates: int = DEFAULT_RETRO_MAX_CANDIDATES
    #: Case-insensitive title substrings excluding a fix-up pair from mining
    #: (the retrospective's own weekly output must not mine itself).
    ignored_title_patterns: list[str] = field(
        default_factory=lambda: ["weekly gotcha retrospective", "weekly agent rollup"]
    )
    #: A pair whose ONLY shared files match these globs is housekeeping
    #: correlation, not defect evidence.
    ignored_path_globs: list[str] = field(
        default_factory=lambda: [
            "CLAUDE.md",
            "AGENTS.md",
            "README.md",
            "CLAUDE/*",
            "wiki/*",
            "agent_telemetry/*",
            "agent_memory/gotchas.json",
        ]
    )


@dataclass
class RetroBridgeConfig:
    """File-backed bridge evidence roots. Default EMPTY: adopters opt in."""

    memory_roots: list[str] = field(default_factory=list)
    run_artifact_roots: list[str] = field(default_factory=list)


@dataclass
class RetroProposalsConfig:
    """Instruction-file (doc/skill) proposal pass.

    Verified proposals are rendered human-review-only in the weekly summary;
    ``corral retro run`` never auto-applies them.
    """

    enabled: bool = False
    #: Cap on accepted doc/skill proposals per weekly run, ON TOP of the
    #: gotcha cap (hard-capped at 3 to keep the combined PR reviewable).
    max: int = DEFAULT_RETRO_PROPOSALS_MAX
    #: Minimum DISTINCT root incidents behind one proposal.
    min_incidents: int = DEFAULT_RETRO_PROPOSALS_MIN_INCIDENTS
    #: Repo-relative globs the drafter may target with prose/skill edits.
    #: No default: adopters declare their own instruction-file ladder.
    target_globs: list[str] = field(default_factory=list)


@dataclass
class Config:
    """Merged view of ``corral.yaml`` plus defaults for any missing keys."""

    codemap: CodemapConfig = field(default_factory=CodemapConfig)
    lineage: LineageConfig = field(default_factory=LineageConfig)
    hooks: HooksConfig = field(default_factory=HooksConfig)
    preflight: PreflightConfig = field(default_factory=PreflightConfig)
    telemetry: TelemetryConfig = field(default_factory=TelemetryConfig)
    retro: RetroConfig = field(default_factory=RetroConfig)
    governance: GovernanceConfig = field(default_factory=GovernanceConfig)
    #: Repo-root-relative registry used by retrospective model seats.
    seats_file: str = DEFAULT_SEATS_FILE
    #: Resolved path of the loaded ``corral.yaml``; ``None`` when no file
    #: was found and plain defaults apply.
    source_path: Path | None = None

    @property
    def root(self) -> Path:
        """Repository root: the directory holding ``corral.yaml``, else cwd."""
        if self.source_path is not None:
            return self.source_path.parent
        return Path.cwd()


def find_config_file(start: Path | None = None) -> Path | None:
    """Walk up from *start* (default: cwd) looking for ``corral.yaml``."""
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        config_path = candidate / CONFIG_FILENAME
        if config_path.is_file():
            return config_path
    return None


def _as_str(value: object, key: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"corral.yaml: {key} must be a string, got {type(value).__name__}")
    return value


def _as_optional_str(value: object, key: str) -> str | None:
    if value is None:
        return None
    return _as_str(value, key)


def _as_int(value: object, key: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"corral.yaml: {key} must be an integer, got {type(value).__name__}")
    return value


def _as_float(value: object, key: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"corral.yaml: {key} must be a number, got {type(value).__name__}")
    return float(value)


def _as_bool(value: object, key: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"corral.yaml: {key} must be a boolean, got {type(value).__name__}")
    return value


def _as_choice(value: object, key: str, choices: set[str]) -> str:
    # YAML 1.1 parses bare `off`/`on` as booleans; map them back so
    # unquoted values like `issue_sink: off` work as written.
    if value is False:
        value = "off"
    elif value is True:
        value = "on"
    parsed = _as_str(value, key)
    if parsed not in choices:
        allowed = ", ".join(sorted(choices))
        raise ValueError(f"corral.yaml: {key} must be one of {allowed}, got {parsed!r}")
    return parsed


def _as_str_list(value: object, key: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"corral.yaml: {key} must be a list of strings")
    return list(value)


def _as_str_map(value: object, key: str) -> dict[str, str]:
    if not isinstance(value, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in value.items()
    ):
        raise ValueError(f"corral.yaml: {key} must be a mapping of string to string")
    return dict(value)


def _as_section(document: dict, key: str) -> dict:
    """Return ``document[key]`` as a mapping, rejecting non-mapping values.

    A missing or empty (``None``) section is fine and yields ``{}``; a
    falsey-but-invalid value such as ``[]``, ``false`` or ``0`` is an error
    naming the offending key.
    """
    raw = document.get(key)
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(
            f"corral.yaml: {key!r} must be a mapping, got {type(raw).__name__}"
        )
    return raw


def load_config(path: Path | str | None = None) -> Config:
    """Load ``corral.yaml`` and return a :class:`Config` with defaults applied.

    When *path* is given it must exist. Otherwise the nearest
    ``corral.yaml`` above the current working directory is used, and plain
    defaults apply when no file is found at all.
    """
    if path is not None:
        config_path = Path(path)
        if not config_path.is_file():
            raise FileNotFoundError(f"corral config file not found: {config_path}")
    else:
        found = find_config_file()
        if found is None:
            return Config()
        config_path = found

    document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if document is None:
        document = {}
    if not isinstance(document, dict):
        raise ValueError(
            "corral.yaml must contain a mapping at the top level, "
            f"got {type(document).__name__}: {config_path}"
        )

    codemap_raw = _as_section(document, "codemap")
    lineage_raw = _as_section(document, "lineage")
    hooks_raw = _as_section(document, "hooks")
    magic_numbers_raw = _as_section(hooks_raw, "magic_numbers")
    preflight_raw = _as_section(document, "preflight")
    telemetry_raw = _as_section(document, "telemetry")
    retro_raw = _as_section(document, "retro")
    governance_raw = _as_section(document, "governance")

    codemap = CodemapConfig()
    if "output_dir" in codemap_raw:
        codemap.output_dir = _as_str(codemap_raw["output_dir"], "codemap.output_dir")
    if "scan_dirs" in codemap_raw:
        codemap.scan_dirs = _as_str_list(codemap_raw["scan_dirs"], "codemap.scan_dirs")
    if "skip_dirs" in codemap_raw:
        codemap.skip_dirs = _as_str_list(codemap_raw["skip_dirs"], "codemap.skip_dirs")

    lineage = LineageConfig()
    if "output" in lineage_raw:
        lineage.output = _as_str(lineage_raw["output"], "lineage.output")
        lineage.output_configured = True
    if "pipeline_yaml" in lineage_raw:
        lineage.pipeline_yaml = _as_str(lineage_raw["pipeline_yaml"], "lineage.pipeline_yaml")
    if "yaml_manifest_schema" in lineage_raw:
        lineage.yaml_manifest_schema = _as_str_map(
            lineage_raw["yaml_manifest_schema"], "lineage.yaml_manifest_schema"
        )
    if "config_loaders" in lineage_raw:
        lineage.config_loaders = _as_str_map(lineage_raw["config_loaders"], "lineage.config_loaders")
    if "config_loader_key_prefixes" in lineage_raw:
        lineage.config_loader_key_prefixes = _as_str_map(
            lineage_raw["config_loader_key_prefixes"], "lineage.config_loader_key_prefixes"
        )

    hooks = HooksConfig()
    if "surfaces" in hooks_raw:
        hooks.surfaces = _as_str(hooks_raw["surfaces"], "hooks.surfaces")
    if "constants" in magic_numbers_raw:
        hooks.magic_numbers.constants = _as_optional_str(
            magic_numbers_raw["constants"], "hooks.magic_numbers.constants"
        )
    if "allowlist" in magic_numbers_raw:
        hooks.magic_numbers.allowlist = _as_str(
            magic_numbers_raw["allowlist"], "hooks.magic_numbers.allowlist"
        )
    if "scan_dirs" in magic_numbers_raw:
        hooks.magic_numbers.scan_dirs = _as_str_list(
            magic_numbers_raw["scan_dirs"], "hooks.magic_numbers.scan_dirs"
        )

    preflight = PreflightConfig()
    if "gotchas" in preflight_raw:
        preflight.gotchas = _as_str(preflight_raw["gotchas"], "preflight.gotchas")
    if "model" in preflight_raw:
        preflight.model = _as_str(preflight_raw["model"], "preflight.model")
    if "max_tokens" in preflight_raw:
        preflight.max_tokens = _as_int(preflight_raw["max_tokens"], "preflight.max_tokens")
    if "quota_status_file" in preflight_raw:
        preflight.quota_status_file = _as_optional_str(
            preflight_raw["quota_status_file"], "preflight.quota_status_file"
        )
    if "output" in preflight_raw:
        preflight.output = _as_optional_str(preflight_raw["output"], "preflight.output")
    if "recognized_modules" in preflight_raw:
        preflight.recognized_modules = (
            None
            if preflight_raw["recognized_modules"] is None
            else _as_str_list(
                preflight_raw["recognized_modules"], "preflight.recognized_modules"
            )
        )
    if "workflow_kinds" in preflight_raw:
        preflight.workflow_kinds = _as_str_map(
            preflight_raw["workflow_kinds"], "preflight.workflow_kinds"
        )

    telemetry = TelemetryConfig()
    if "spool_dir" in telemetry_raw:
        telemetry.spool_dir = _as_optional_str(telemetry_raw["spool_dir"], "telemetry.spool_dir")
    if "rollup_output_dir" in telemetry_raw:
        telemetry.rollup_output_dir = _as_str(
            telemetry_raw["rollup_output_dir"], "telemetry.rollup_output_dir"
        )
    if "lookback_days" in telemetry_raw:
        telemetry.lookback_days = _as_int(telemetry_raw["lookback_days"], "telemetry.lookback_days")
    if "required_ci_contexts" in telemetry_raw:
        telemetry.required_ci_contexts = _as_str_list(
            telemetry_raw["required_ci_contexts"], "telemetry.required_ci_contexts"
        )

    retro = RetroConfig()
    if "drafter_seat" in retro_raw:
        retro.drafter_seat = _as_str(retro_raw["drafter_seat"], "retro.drafter_seat")
    if "verifier_seats" in retro_raw:
        retro.verifier_seats = _as_str_list(
            retro_raw["verifier_seats"], "retro.verifier_seats"
        )
        if not retro.verifier_seats:
            raise ValueError("corral.yaml: retro.verifier_seats must contain at least one seat")
    if "require_distinct_provider" in retro_raw:
        retro.require_distinct_provider = _as_bool(
            retro_raw["require_distinct_provider"], "retro.require_distinct_provider"
        )
    if "verification_timeout_s" in retro_raw:
        retro.verification_timeout_s = _as_int(
            retro_raw["verification_timeout_s"], "retro.verification_timeout_s"
        )
        if retro.verification_timeout_s <= 0:
            raise ValueError("corral.yaml: retro.verification_timeout_s must be positive")
    if "gotcha_unavailable_policy" in retro_raw:
        retro.gotcha_unavailable_policy = _as_choice(
            retro_raw["gotcha_unavailable_policy"],
            "retro.gotcha_unavailable_policy",
            {"proceed-unverified", "fail-closed", "refute"},
        )
    if "instruction_unavailable_policy" in retro_raw:
        retro.instruction_unavailable_policy = _as_choice(
            retro_raw["instruction_unavailable_policy"],
            "retro.instruction_unavailable_policy",
            {"fail-closed", "refute"},
        )
    if "repository" in retro_raw:
        retro.repository = _as_optional_str(retro_raw["repository"], "retro.repository")
    if "issue_sink" in retro_raw:
        retro.issue_sink = _as_choice(
            retro_raw["issue_sink"], "retro.issue_sink", {"github", "stdout", "off"}
        )
    if "fixup_glob" in retro_raw:
        retro.fixup_glob = _as_optional_str(retro_raw["fixup_glob"], "retro.fixup_glob")
    if "gotchas_path" in retro_raw:
        retro.gotchas_path = _as_str(retro_raw["gotchas_path"], "retro.gotchas_path")
    if "refinements_path" in retro_raw:
        retro.refinements_path = _as_str(
            retro_raw["refinements_path"], "retro.refinements_path"
        )
    if "confidence_threshold" in retro_raw:
        retro.confidence_threshold = _as_float(
            retro_raw["confidence_threshold"], "retro.confidence_threshold"
        )
        if not 0.0 <= retro.confidence_threshold <= 1.0:
            raise ValueError("corral.yaml: retro.confidence_threshold must be between 0.0 and 1.0")
    if "max_tokens" in retro_raw:
        retro.max_tokens = _as_int(retro_raw["max_tokens"], "retro.max_tokens")
        if retro.max_tokens <= 0:
            raise ValueError("corral.yaml: retro.max_tokens must be positive")
    if "drafting_timeout_s" in retro_raw:
        retro.drafting_timeout_s = _as_int(
            retro_raw["drafting_timeout_s"], "retro.drafting_timeout_s"
        )
        if retro.drafting_timeout_s <= 0:
            raise ValueError("corral.yaml: retro.drafting_timeout_s must be positive")
    if "allowed_severities" in retro_raw:
        retro.allowed_severities = _as_str_list(
            retro_raw["allowed_severities"], "retro.allowed_severities"
        )
        if not retro.allowed_severities:
            raise ValueError("corral.yaml: retro.allowed_severities must not be empty")
    if "severe_severities" in retro_raw:
        retro.severe_severities = _as_str_list(
            retro_raw["severe_severities"], "retro.severe_severities"
        )

    github_raw = _as_section(retro_raw, "github")
    if "assignee" in github_raw:
        retro.github.assignee = _as_optional_str(github_raw["assignee"], "retro.github.assignee")
    if "gotcha_label" in github_raw:
        label = _as_str(github_raw["gotcha_label"], "retro.github.gotcha_label")
        if not label.strip():
            raise ValueError("corral.yaml: retro.github.gotcha_label must be non-empty")
        retro.github.gotcha_label = label
    if "timeout_s" in github_raw:
        retro.github.timeout_s = _as_int(github_raw["timeout_s"], "retro.github.timeout_s")
        if retro.github.timeout_s <= 0:
            raise ValueError("corral.yaml: retro.github.timeout_s must be positive")

    evidence_raw = _as_section(retro_raw, "evidence")
    if "min_root_incidents" in evidence_raw:
        retro.evidence.min_root_incidents = _as_int(
            evidence_raw["min_root_incidents"], "retro.evidence.min_root_incidents"
        )
        if retro.evidence.min_root_incidents < DEFAULT_RETRO_MIN_ROOT_INCIDENTS:
            raise ValueError(
                "corral.yaml: retro.evidence.min_root_incidents must be >= "
                f"{DEFAULT_RETRO_MIN_ROOT_INCIDENTS}"
            )
    if "max_candidates" in evidence_raw:
        retro.evidence.max_candidates = _as_int(
            evidence_raw["max_candidates"], "retro.evidence.max_candidates"
        )
        if retro.evidence.max_candidates < 1:
            raise ValueError("corral.yaml: retro.evidence.max_candidates must be >= 1")
    if "ignored_title_patterns" in evidence_raw:
        retro.evidence.ignored_title_patterns = _as_str_list(
            evidence_raw["ignored_title_patterns"], "retro.evidence.ignored_title_patterns"
        )
    if "ignored_path_globs" in evidence_raw:
        retro.evidence.ignored_path_globs = _as_str_list(
            evidence_raw["ignored_path_globs"], "retro.evidence.ignored_path_globs"
        )

    bridge_raw = _as_section(retro_raw, "bridge")
    if "memory_roots" in bridge_raw:
        retro.bridge.memory_roots = _as_str_list(
            bridge_raw["memory_roots"], "retro.bridge.memory_roots"
        )
    if "run_artifact_roots" in bridge_raw:
        retro.bridge.run_artifact_roots = _as_str_list(
            bridge_raw["run_artifact_roots"], "retro.bridge.run_artifact_roots"
        )

    proposals_raw = _as_section(retro_raw, "proposals")
    if "enabled" in proposals_raw:
        retro.proposals.enabled = _as_bool(proposals_raw["enabled"], "retro.proposals.enabled")
    if "max" in proposals_raw:
        retro.proposals.max = _as_int(proposals_raw["max"], "retro.proposals.max")
        if retro.proposals.max < 1:
            raise ValueError("corral.yaml: retro.proposals.max must be >= 1")
        if retro.proposals.max > DEFAULT_RETRO_PROPOSALS_MAX:
            raise ValueError(
                "corral.yaml: retro.proposals.max may not exceed the hard cap of "
                f"{DEFAULT_RETRO_PROPOSALS_MAX}"
            )
    if "min_incidents" in proposals_raw:
        retro.proposals.min_incidents = _as_int(
            proposals_raw["min_incidents"], "retro.proposals.min_incidents"
        )
        if retro.proposals.min_incidents < DEFAULT_RETRO_PROPOSALS_MIN_INCIDENTS:
            raise ValueError(
                "corral.yaml: retro.proposals.min_incidents must be >= "
                f"{DEFAULT_RETRO_PROPOSALS_MIN_INCIDENTS}"
            )
    if "target_globs" in proposals_raw:
        retro.proposals.target_globs = _as_str_list(
            proposals_raw["target_globs"], "retro.proposals.target_globs"
        )

    governance = governance_config_from_mapping(governance_raw)

    seats_file = DEFAULT_SEATS_FILE
    if "seats_file" in document:
        seats_file = _as_str(document["seats_file"], "seats_file")

    return Config(
        codemap=codemap,
        lineage=lineage,
        hooks=hooks,
        preflight=preflight,
        telemetry=telemetry,
        retro=retro,
        governance=governance,
        seats_file=seats_file,
        source_path=config_path.resolve(),
    )
