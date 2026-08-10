"""CLI implementation for ``corral memory validate``."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from corral.config import load_config
from corral.memory import registry


def run_validate(args: argparse.Namespace) -> int:
    """Validate agent-memory files against the bundled JSON Schemas.

    With no flags, validates the configured gotcha registry
    (``preflight.gotchas``). ``--gotchas`` overrides that path and
    ``--refinements`` additionally validates a refinement-ledger file.
    Exit codes: 0 valid (or nothing to validate), 1 invalid/unreadable.
    """
    cfg = load_config(getattr(args, "config", None))
    root = Path(args.root) if getattr(args, "root", None) is not None else cfg.root

    gotchas_path = (
        Path(args.gotchas) if args.gotchas is not None else root / cfg.preflight.gotchas
    )
    explicit_gotchas = args.gotchas is not None

    failed = False

    if gotchas_path.is_file() or explicit_gotchas:
        failed |= not _validate_file(
            gotchas_path, registry.validate_gotchas_file, "gotcha registry"
        )
    elif args.refinements is None:
        print(f"note: gotcha registry not found at {gotchas_path}; nothing to validate")

    if args.refinements is not None:
        failed |= not _validate_file(
            Path(args.refinements), registry.validate_refinements_file, "refinement ledger"
        )

    return 1 if failed else 0


def _validate_file(path: Path, validator, label: str) -> bool:
    """Return True when *path* validates cleanly."""
    try:
        errors = validator(path)
    except registry.MissingOptionalDependencyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return False
    except FileNotFoundError:
        print(f"error: {label} file not found: {path}", file=sys.stderr)
        return False
    except json.JSONDecodeError as exc:
        print(f"error: {label} is not valid JSON: {path}: {exc}", file=sys.stderr)
        return False
    except OSError as exc:
        print(f"error: cannot read {label}: {path}: {exc}", file=sys.stderr)
        return False

    if errors:
        print(f"{label} INVALID: {path}")
        for error in errors:
            print(f"  - {error}")
        return False
    print(f"{label} valid: {path}")
    return True
