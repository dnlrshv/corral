"""corral CLI glue for the ``corral telemetry`` subcommands."""

from __future__ import annotations

import argparse
import json
import os

from corral.telemetry.ci_outcome import REQUIRED_CI_CONTEXTS, fetch_ci_outcome_for_pr


def run_capture(args: argparse.Namespace) -> int:
    """Delegate to the fail-soft Stop-hook capture entrypoint."""
    from corral.telemetry.capture import main as capture_main

    argv: list[str] = []
    if args.spool_dir is not None:
        argv += ["--spool-dir", str(args.spool_dir)]
    if args.config is not None:
        argv += ["--config", str(args.config)]
    return capture_main(argv)


def run_rollup(args: argparse.Namespace) -> int:
    """Delegate to the weekly rollup entrypoint."""
    from corral.telemetry.rollup import main as rollup_main

    argv: list[str] = []
    if args.days is not None:
        argv += ["--days", str(args.days)]
    if args.week is not None:
        argv += ["--week", args.week]
    if args.output is not None:
        argv += ["--output", str(args.output)]
    if args.output_dir is not None:
        argv += ["--output-dir", str(args.output_dir)]
    if args.config is not None:
        argv += ["--config", str(args.config)]
    return rollup_main(argv)


def run_ci_outcome(args: argparse.Namespace) -> int:
    """Print the reconstructed CI outcome for one PR as JSON.

    Fail-soft on ``gh`` problems: a missing binary or failed API call yields
    null outcome fields and exit 0, matching the rollup's join behaviour.
    """
    repo = args.repo or os.environ.get("GITHUB_REPOSITORY", "")
    if not repo:
        print("corral telemetry ci-outcome: --repo or GITHUB_REPOSITORY is required", flush=True)
        return 1

    from corral.config import load_config

    cfg = load_config(args.config)
    required_contexts = REQUIRED_CI_CONTEXTS
    if cfg.telemetry.required_ci_contexts:
        required_contexts = tuple(cfg.telemetry.required_ci_contexts)

    outcome = fetch_ci_outcome_for_pr(args.pr, repo, required_contexts)
    print(json.dumps(outcome, indent=2, sort_keys=True))
    return 0
