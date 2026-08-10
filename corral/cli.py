"""Command-line interface for corral.

Subcommands:

- ``corral codemap build``  — build imports.parquet / symbols.parquet.
- ``corral codemap query``  — query the unified code-map graph.
- ``corral lineage build``  — build edges.parquet.
- ``corral hooks ...``      — run repository and editor enforcement hooks.
- ``corral preflight``      — generate a per-task preflight brief.
- ``corral memory validate``— validate agent-memory files against schemas.
- ``corral telemetry ...``  — capture/roll up agent session telemetry.
- ``corral governance ...`` — instruction gate, replay, corpus, budgets, and
  the staleness report.

Defaults for roots, output locations and scan behaviour come from
``corral.yaml`` (see :mod:`corral.config`); every flag overrides the
configured value.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from corral import __version__
from corral.config import load_config


def _cmd_codemap_build(args: argparse.Namespace) -> int:
    from corral.codemap.build import build_code_map_with_cache

    cfg = load_config(args.config)
    root = Path(args.root) if args.root is not None else cfg.root
    output_dir = Path(args.output_dir) if args.output_dir is not None else root / cfg.codemap.output_dir
    build_code_map_with_cache(
        root,
        output_dir,
        use_cache=not args.no_cache,
        scan_dirs=cfg.codemap.scan_dirs,
        skip_dirs=cfg.codemap.skip_dirs,
    )
    return 0


def _cmd_codemap_query_simple(query_args: list[str]) -> int:
    from corral.codemap.query import main as query_main

    return query_main(query_args)


def _cmd_codemap_query(args: argparse.Namespace) -> int:
    # Reached only when the remainder starts with a positional token and
    # argparse itself routed the subcommand here.
    return _cmd_codemap_query_simple(args.query_args)


def _cmd_lineage_build(args: argparse.Namespace) -> int:
    from corral.lineage.build import build_and_write_lineage

    cfg = load_config(args.config)
    root = Path(args.root) if args.root is not None else cfg.root
    output_dir = (
        Path(args.output_dir)
        if args.output_dir is not None
        else root / Path(cfg.lineage.output).parent
    )
    pipeline_yaml = (
        Path(args.pipeline_yaml) if args.pipeline_yaml is not None else root / cfg.lineage.pipeline_yaml
    )
    build_and_write_lineage(
        root,
        output_dir,
        pipeline_yaml=pipeline_yaml,
        scan_dirs=cfg.codemap.scan_dirs,
        skip_dirs=cfg.codemap.skip_dirs,
        config_loaders=cfg.lineage.config_loaders,
        loader_key_prefixes=cfg.lineage.config_loader_key_prefixes,
        yaml_manifest_schema=cfg.lineage.yaml_manifest_schema,
    )
    return 0


def _cmd_surface_check(args: argparse.Namespace) -> int:
    from corral.hooks.surface_check import run

    return run(
        warn_only=args.warn_only,
        surfaces_path=args.surfaces,
        config_path=args.config,
    )


def _cmd_surface_reminder(args: argparse.Namespace) -> int:
    from corral.hooks.surface_reminder import run

    return run(config_path=args.config, surfaces_path=args.surfaces)


def _cmd_magic_numbers(args: argparse.Namespace) -> int:
    from corral.hooks.magic_numbers import run

    return run(
        root=args.root,
        config_path=args.config,
        constants_path=args.constants,
        allowlist_path=args.allowlist,
        scan_dirs=args.scan_dirs,
    )


def _cmd_preflight(args: argparse.Namespace) -> int:
    from corral.preflight.cli import run as preflight_run

    return preflight_run(args)


def _cmd_memory_validate(args: argparse.Namespace) -> int:
    from corral.memory.cli import run_validate

    return run_validate(args)


def _cmd_telemetry_capture(args: argparse.Namespace) -> int:
    from corral.telemetry.cli import run_capture

    return run_capture(args)


def _cmd_telemetry_rollup(args: argparse.Namespace) -> int:
    from corral.telemetry.cli import run_rollup

    return run_rollup(args)


def _cmd_telemetry_ci_outcome(args: argparse.Namespace) -> int:
    from corral.telemetry.cli import run_ci_outcome

    return run_ci_outcome(args)


def _cmd_retro_seats_check(args: argparse.Namespace) -> int:
    from corral.retro.cli import run_seats_check

    return run_seats_check(args)


def _cmd_retro_run(args: argparse.Namespace) -> int:
    from corral.retro.cli import run_retro

    return run_retro(args)


def _cmd_retro_revert_refinement(args: argparse.Namespace) -> int:
    from corral.retro.cli import run_revert_refinement

    return run_revert_refinement(args)


def _cmd_governance_check(args: argparse.Namespace) -> int:
    from corral.governance.cli import run_check_command

    return run_check_command(args)


def _cmd_governance_replay(args: argparse.Namespace) -> int:
    from corral.governance.cli import run_replay_command

    return run_replay_command(args)


def _cmd_governance_build_corpus(args: argparse.Namespace) -> int:
    from corral.governance.cli import run_build_corpus_command

    return run_build_corpus_command(args)


def _cmd_governance_lint_budget(args: argparse.Namespace) -> int:
    from corral.governance.cli import run_lint_budget_command

    return run_lint_budget_command(args)


def _cmd_governance_staleness(args: argparse.Namespace) -> int:
    from corral.governance.cli import run_staleness_command

    return run_staleness_command(args)


def _add_config_aware_build_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="Repository or project root to scan (default: directory of corral.yaml, else cwd)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to corral.yaml (default: nearest corral.yaml above cwd)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="corral",
        description="Repo infrastructure for teams operating fleets of coding agents.",
    )
    parser.add_argument("--version", action="version", version=f"corral {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    # -- corral codemap -----------------------------------------------------
    codemap = sub.add_parser("codemap", help="Code map builders and queries")
    codemap_sub = codemap.add_subparsers(dest="codemap_command", required=True)

    codemap_build = codemap_sub.add_parser(
        "build", help="Build imports.parquet and symbols.parquet"
    )
    _add_config_aware_build_args(codemap_build)
    codemap_build.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Directory that receives imports.parquet and symbols.parquet "
        "(default: <root>/<codemap.output_dir> from corral.yaml)",
    )
    codemap_build.add_argument(
        "--no-cache",
        action="store_true",
        help="Force a direct rebuild without reading or writing the per-tree-sha cache",
    )
    codemap_build.set_defaults(handler=_cmd_codemap_build)

    # `corral codemap query` delegates to the query CLI, which owns its own
    # (richer) argument parser; add_help=False so -h/--help passes through.
    codemap_query = codemap_sub.add_parser(
        "query",
        help="Query the unified code-map graph (impact / lineage / path / render)",
        add_help=False,
    )
    codemap_query.add_argument("query_args", nargs=argparse.REMAINDER)
    codemap_query.set_defaults(handler=_cmd_codemap_query)

    # -- corral lineage -----------------------------------------------------
    lineage = sub.add_parser("lineage", help="Data-lineage builders")
    lineage_sub = lineage.add_subparsers(dest="lineage_command", required=True)

    lineage_build = lineage_sub.add_parser("build", help="Build edges.parquet")
    _add_config_aware_build_args(lineage_build)
    lineage_build.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Directory that receives edges.parquet "
        "(default: parent of lineage.output from corral.yaml)",
    )
    lineage_build.add_argument(
        "--pipeline-yaml",
        type=Path,
        default=None,
        help="Path to the pipeline manifest YAML (default: <root>/<lineage.pipeline_yaml>)",
    )
    lineage_build.set_defaults(handler=_cmd_lineage_build)

    # -- corral hooks -------------------------------------------------------
    hooks = sub.add_parser("hooks", help="Repository and coding-agent enforcement hooks")
    hooks_sub = hooks.add_subparsers(dest="hooks_command", required=True)

    surface_check = hooks_sub.add_parser(
        "surface-check", help="Check staged changes against declared surfaces"
    )
    surface_check.add_argument(
        "--warn-only", action="store_true", help="Never block; always exit 0"
    )
    surface_check.add_argument(
        "--surfaces", type=Path, default=None, help="Path to surfaces.yaml"
    )
    surface_check.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to corral.yaml (default: nearest corral.yaml above cwd)",
    )
    surface_check.set_defaults(handler=_cmd_surface_check)

    surface_reminder = hooks_sub.add_parser(
        "surface-reminder", help="Emit a PreToolUse reminder for a declared surface"
    )
    surface_reminder.add_argument(
        "--surfaces", type=Path, default=None, help="Path to surfaces.yaml"
    )
    surface_reminder.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to corral.yaml (default: nearest file above the project root)",
    )
    surface_reminder.set_defaults(handler=_cmd_surface_reminder)

    magic_numbers = hooks_sub.add_parser(
        "magic-numbers", help="Lint literals that duplicate configured constants"
    )
    _add_config_aware_build_args(magic_numbers)
    magic_numbers.add_argument(
        "--constants",
        type=Path,
        default=None,
        help="Constants module path (default: hooks.magic_numbers.constants)",
    )
    magic_numbers.add_argument(
        "--allowlist",
        type=Path,
        default=None,
        help="Allowlist YAML path (default: hooks.magic_numbers.allowlist)",
    )
    magic_numbers.add_argument(
        "--scan-dirs",
        nargs="+",
        default=None,
        metavar="DIR",
        help="Directories relative to --root (default: hooks setting or codemap.scan_dirs)",
    )
    magic_numbers.set_defaults(handler=_cmd_magic_numbers)

    # -- corral preflight ---------------------------------------------------
    preflight = sub.add_parser(
        "preflight",
        help="Generate a per-task preflight brief (LLM with deterministic fallback)",
    )
    _add_config_aware_build_args(preflight)
    from corral.preflight.cli import add_preflight_arguments

    add_preflight_arguments(preflight)
    preflight.set_defaults(handler=_cmd_preflight)

    # -- corral memory --------------------------------------------------------
    memory = sub.add_parser("memory", help="Agent-memory registry tooling")
    memory_sub = memory.add_subparsers(dest="memory_command", required=True)

    memory_validate = memory_sub.add_parser(
        "validate", help="Validate agent-memory files against the bundled schemas"
    )
    _add_config_aware_build_args(memory_validate)
    memory_validate.add_argument(
        "--gotchas",
        type=Path,
        default=None,
        help="Gotcha registry path to validate (default: <root>/<preflight.gotchas>)",
    )
    memory_validate.add_argument(
        "--refinements",
        type=Path,
        default=None,
        help="Additionally validate a refinement-ledger file",
    )
    memory_validate.set_defaults(handler=_cmd_memory_validate)

    # -- corral telemetry ----------------------------------------------------
    telemetry = sub.add_parser(
        "telemetry", help="Agent session telemetry capture and weekly rollup"
    )
    telemetry_sub = telemetry.add_subparsers(dest="telemetry_command", required=True)

    telemetry_capture = telemetry_sub.add_parser(
        "capture",
        help="Capture a session record from a Stop-hook payload on stdin (fail-soft)",
    )
    telemetry_capture.add_argument(
        "--spool-dir",
        type=Path,
        default=None,
        help="Telemetry spool directory (default: $CORRAL_TELEMETRY_DIR, else "
        "telemetry.spool_dir, else ~/.cache/corral/telemetry)",
    )
    telemetry_capture.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to corral.yaml (telemetry.spool_dir applies when the "
        "environment variable is unset)",
    )
    telemetry_capture.set_defaults(handler=_cmd_telemetry_capture)

    telemetry_rollup = telemetry_sub.add_parser(
        "rollup", help="Roll up weekly session artifacts into parquet"
    )
    telemetry_rollup.add_argument(
        "--days",
        type=int,
        default=None,
        help="Artifact lookback window in days (default: telemetry.lookback_days)",
    )
    telemetry_rollup.add_argument(
        "--week",
        default=None,
        help="ISO week label YYYY-WW for the output file (default: current week)",
    )
    telemetry_rollup.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output parquet path (default: auto-named from the ISO week)",
    )
    telemetry_rollup.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Directory receiving the rollup parquet "
        "(default: <root>/<telemetry.rollup_output_dir>)",
    )
    telemetry_rollup.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to corral.yaml (default: nearest corral.yaml above cwd)",
    )
    telemetry_rollup.set_defaults(handler=_cmd_telemetry_rollup)

    telemetry_ci_outcome = telemetry_sub.add_parser(
        "ci-outcome",
        help="Reconstruct first/final-push CI outcomes for one PR (gh fail-soft)",
    )
    telemetry_ci_outcome.add_argument("--pr", type=int, required=True, help="PR number")
    telemetry_ci_outcome.add_argument(
        "--repo",
        default=None,
        help="Repository as owner/name (default: $GITHUB_REPOSITORY)",
    )
    telemetry_ci_outcome.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to corral.yaml (telemetry.required_ci_contexts applies)",
    )
    telemetry_ci_outcome.set_defaults(handler=_cmd_telemetry_ci_outcome)

    # -- corral retro --------------------------------------------------------
    retro = sub.add_parser("retro", help="Retrospective model-seat tooling")
    retro_sub = retro.add_subparsers(dest="retro_command", required=True)
    retro_seats = retro_sub.add_parser("seats", help="Inspect configured model seats")
    retro_seats_sub = retro_seats.add_subparsers(dest="retro_seats_command", required=True)
    retro_seats_check = retro_seats_sub.add_parser(
        "check", help="Probe every configured seat and report availability"
    )
    retro_seats_check.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to corral.yaml (default: nearest corral.yaml above cwd)",
    )
    retro_seats_check.set_defaults(handler=_cmd_retro_seats_check)

    retro_run = retro_sub.add_parser(
        "run", help="Run the weekly retrospective (mine, draft, verify, write)"
    )
    retro_run.add_argument(
        "--dry-run",
        action="store_true",
        help="Print candidates without writing the gotcha registry or filing issues",
    )
    retro_run.add_argument(
        "--week",
        default=None,
        help="ISO week label YYYY-WW to mine (default: current week)",
    )
    retro_run.add_argument(
        "--since", default=None, help="Start date YYYY-MM-DD (default: the week's Monday)"
    )
    retro_run.add_argument(
        "--until", default=None, help="End date YYYY-MM-DD (default: the week's Sunday)"
    )
    retro_run.add_argument(
        "--output-summary",
        type=Path,
        default=None,
        help="Summary Markdown path (default: <telemetry rollup dir>/retrospective_<week>.md)",
    )
    retro_run.add_argument(
        "--base-ref",
        default=None,
        help="Single-writer base ref (e.g. origin/main) to re-resolve before writing",
    )
    retro_run.add_argument(
        "--expected-base",
        default=None,
        help="Commit id the base ref must still resolve to; refuse the write when it moved",
    )
    retro_run.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to corral.yaml (default: nearest corral.yaml above cwd)",
    )
    retro_run.set_defaults(handler=_cmd_retro_run)

    retro_revert = retro_sub.add_parser(
        "revert-refinement",
        help="Render (never apply) the reverse patch for one refinement-ledger record",
    )
    retro_revert.add_argument(
        "refinement_id", help="Ledger id from the merged retrospective PR"
    )
    retro_revert.add_argument(
        "--ledger",
        type=Path,
        default=None,
        help="Refinement ledger path (default: retro.refinements_path)",
    )
    retro_revert.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional patch output path; target files are never written",
    )
    retro_revert.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to corral.yaml (default: nearest corral.yaml above cwd)",
    )
    retro_revert.set_defaults(handler=_cmd_retro_revert_refinement)

    # -- corral governance ---------------------------------------------------
    governance = sub.add_parser(
        "governance", help="Instruction governance and deterministic replay"
    )
    governance_sub = governance.add_subparsers(dest="governance_command", required=True)

    governance_check = governance_sub.add_parser(
        "check", help="Check registry consistency or gate a base..head range"
    )
    _add_config_aware_build_args(governance_check)
    governance_check.add_argument("--base-ref", default=None)
    governance_check.add_argument("--head-ref", default=None)
    governance_check.add_argument("--pr-body-file", type=Path, default=None)
    governance_check.add_argument("--json", action="store_true")
    governance_check.set_defaults(handler=_cmd_governance_check)

    governance_replay = governance_sub.add_parser(
        "replay", help="Replay the reviewed retrieval corpus"
    )
    _add_config_aware_build_args(governance_replay)
    governance_replay.add_argument("--manifest", type=Path, default=None)
    governance_replay.add_argument("--trigger-rules", type=Path, default=None)
    governance_replay.add_argument("--corpus", type=Path, default=None)
    governance_replay.add_argument("--min-recall", type=float, default=None)
    governance_replay.set_defaults(handler=_cmd_governance_replay)

    governance_build = governance_sub.add_parser(
        "build-corpus", help="Build a corpus from reviewed local case metadata"
    )
    _add_config_aware_build_args(governance_build)
    governance_build.add_argument("--reviewed-cases", type=Path, required=True)
    governance_build.add_argument("--output", type=Path, default=None)
    governance_build.add_argument("--manifest", type=Path, default=None)
    governance_build.add_argument("--trigger-rules", type=Path, default=None)
    governance_build.add_argument("--profile", default=None)
    governance_build.add_argument("--generated-on", default=None)
    governance_build.set_defaults(handler=_cmd_governance_build_corpus)

    governance_budget = governance_sub.add_parser(
        "lint-budget", help="Lint manifest and configured tier token ceilings"
    )
    _add_config_aware_build_args(governance_budget)
    governance_budget.add_argument("--manifest", type=Path, default=None)
    from datetime import date

    governance_budget.add_argument("--as-of", type=date.fromisoformat, default=None)
    governance_budget.set_defaults(handler=_cmd_governance_lint_budget)

    governance_staleness = governance_sub.add_parser(
        "staleness",
        help="Deterministic instruction-staleness report (demotion proposals are human-actioned)",
    )
    _add_config_aware_build_args(governance_staleness)
    governance_staleness.add_argument(
        "--as-of",
        type=date.fromisoformat,
        default=None,
        help="Report anchor date YYYY-MM-DD (default: today, UTC)",
    )
    governance_staleness.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional report file path (default: report goes to stdout only)",
    )
    governance_staleness.add_argument(
        "--issue-sink",
        choices=("stdout", "github"),
        default="stdout",
        help="Where demotion-proposal issues go: github (file via gh, opt-in) or "
        "stdout (render only; the default)",
    )
    governance_staleness.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the report; write nothing, file no issues",
    )
    governance_staleness.set_defaults(handler=_cmd_governance_staleness)

    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # `corral codemap query` delegates to the query CLI, which owns its own
    # richer argument parser. Hand it the remainder verbatim — argparse
    # REMAINDER cannot swallow leading ``--option`` tokens.
    for index in range(len(argv) - 1):
        if argv[index : index + 2] == ["codemap", "query"]:
            return _cmd_codemap_query_simple(argv[index + 2 :])
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 1
    return handler(args)


if __name__ == "__main__":
    import sys

    sys.exit(main())
