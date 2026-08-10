"""Detect numeric literals that duplicate values in a constants module.

Numeric values are extracted from dataclass singleton fields in the configured
constants module. Python files under the configured scan directories are then
checked for matching AST literals. Matches can be suppressed globally, by
file/value pair, by constants-group scope, or inline with ``# magic-ok``.

Exit codes: 0 = clean or no constants module configured, 1 = violations,
2 = a configured constants module is missing or invalid.
"""

from __future__ import annotations

import argparse
import ast
import dataclasses
import fnmatch
import importlib.util
import inspect
import sys
import types
from collections import defaultdict
from pathlib import Path

import yaml

from corral.config import load_config

DEFAULT_SKIP_VALUES: frozenset[int | float] = frozenset({0, 1, -1, 100, 1000})
DEFAULT_HIGH_FREQUENCY_THRESHOLD = 50


@dataclasses.dataclass(frozen=True)
class ParsedPythonFile:
    path: Path
    rel_path: str
    lines: list[str]
    literals: list[tuple[int, int | float]]


def load_constants_module(constants_file: Path) -> types.ModuleType:
    """Import a configured Python constants module for inspection."""
    module_name = "_corral_magic_number_constants"
    spec = importlib.util.spec_from_file_location(module_name, constants_file)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load {constants_file}")
    module = importlib.util.module_from_spec(spec)
    # Register before execution so dataclass decorators can resolve __module__.
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)  # type: ignore[union-attr]
    finally:
        sys.modules.pop(module_name, None)
    return module


def extract_constant_map(module: types.ModuleType) -> dict[int | float, list[str]]:
    """Return numeric value -> dataclass singleton field labels."""
    value_map: dict[int | float, list[str]] = defaultdict(list)
    for _name, obj in inspect.getmembers(module):
        if not dataclasses.is_dataclass(obj) or isinstance(obj, type):
            continue
        public_name = type(obj).__name__.lstrip("_")
        for field in dataclasses.fields(obj):
            value = getattr(obj, field.name)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                value_map[value].append(f"{public_name}.{field.name}")
    return dict(value_map)


def extract_group_map(module: types.ModuleType) -> dict[int | float, set[str]]:
    """Return numeric value -> constants-group names owning that value."""
    group_map: dict[int | float, set[str]] = defaultdict(set)
    for _name, obj in inspect.getmembers(module):
        if not dataclasses.is_dataclass(obj) or isinstance(obj, type):
            continue
        public_name = type(obj).__name__.lstrip("_")
        for field in dataclasses.fields(obj):
            value = getattr(obj, field.name)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                group_map[value].add(public_name)
    return dict(group_map)


def load_allowlist(allowlist_file: Path) -> dict:
    """Load and normalize the optional allowlist YAML."""
    if not allowlist_file.exists():
        return {
            "skip_values": DEFAULT_SKIP_VALUES,
            "high_frequency_threshold": DEFAULT_HIGH_FREQUENCY_THRESHOLD,
            "exceptions": {},
            "scoped_constants": {},
        }

    with allowlist_file.open(encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}

    raw_skip = raw.get("skip_values", list(DEFAULT_SKIP_VALUES))
    threshold = raw.get("high_frequency_threshold", DEFAULT_HIGH_FREQUENCY_THRESHOLD)
    raw_exceptions = raw.get("exceptions", {}) or {}
    raw_scoped = raw.get("scoped_constants", {}) or {}

    return {
        "skip_values": frozenset(raw_skip),
        "high_frequency_threshold": int(threshold),
        "exceptions": {
            str(path): frozenset(values) for path, values in raw_exceptions.items()
        },
        "scoped_constants": {
            str(group): list(patterns) for group, patterns in raw_scoped.items()
        },
    }


def _build_parent_map(tree: ast.AST) -> dict[int, ast.AST]:
    parent: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent[id(child)] = node
    return parent


def get_numeric_literals(tree: ast.AST) -> list[tuple[int, int | float]]:
    """Return ``(lineno, effective_value)`` for numeric literals in *tree*.

    Unary signs are folded so ``-2.5`` reports ``-2.5`` rather than ``2.5``.
    Nested unary signs are folded in the same way.
    """
    parent_map = _build_parent_map(tree)
    results: list[tuple[int, int | float]] = []
    for node in ast.walk(tree):
        if (
            not isinstance(node, ast.Constant)
            or not isinstance(node.value, (int, float))
            or isinstance(node.value, bool)
        ):
            continue
        sign = 1
        current: ast.AST = node
        while True:
            parent = parent_map.get(id(current))
            if not isinstance(parent, ast.UnaryOp) or parent.operand is not current:
                break
            if isinstance(parent.op, ast.USub):
                sign *= -1
            elif not isinstance(parent.op, ast.UAdd):
                break
            current = parent
        results.append((node.lineno, node.value * sign))
    return results


def count_global_occurrences(
    root: Path,
    value: int | float,
    scan_dirs: tuple[str, ...],
) -> int:
    """Count occurrences of *value* across all configured scan directories."""
    return collect_numeric_literals(root, scan_dirs)[1].get(value, 0)


def collect_numeric_literals(
    root: Path,
    scan_dirs: tuple[str, ...],
) -> tuple[list[ParsedPythonFile], dict[int | float, int]]:
    """Parse scan directories once, returning files and global value counts."""
    parsed_files: list[ParsedPythonFile] = []
    counts: dict[int | float, int] = defaultdict(int)

    for directory in scan_dirs:
        scan_root = root / directory
        if not scan_root.is_dir():
            continue
        for file_path in sorted(scan_root.rglob("*.py")):
            try:
                source = file_path.read_text(encoding="utf-8")
                lines = source.splitlines()
                tree = ast.parse(source)
            except (SyntaxError, UnicodeDecodeError):
                continue

            literals = get_numeric_literals(tree)
            for _lineno, value in literals:
                counts[value] += 1
            parsed_files.append(
                ParsedPythonFile(
                    path=file_path,
                    rel_path=str(file_path.relative_to(root)),
                    lines=lines,
                    literals=literals,
                )
            )

    return parsed_files, dict(counts)


def _value_is_scoped_out(
    value: int | float,
    rel_path: str,
    group_map: dict[int | float, set[str]],
    scoped_constants: dict[str, list[str]],
) -> bool:
    """Return whether constants-group scoping excludes this file/value pair."""
    if not scoped_constants:
        return False
    owning_groups = group_map.get(value)
    if not owning_groups:
        return False
    if any(group not in scoped_constants for group in owning_groups):
        return False
    for group in owning_groups:
        for pattern in scoped_constants[group]:
            if fnmatch.fnmatch(rel_path, pattern):
                return False
    return True


def scan(
    root: Path,
    constants_file: Path,
    allowlist: dict,
    value_map: dict[int | float, list[str]],
    scan_dirs: tuple[str, ...] = (".",),
    group_map: dict[int | float, set[str]] | None = None,
) -> list[str]:
    """Scan *scan_dirs* and return violation messages."""
    skip_values: frozenset = allowlist["skip_values"]
    threshold: int = allowlist["high_frequency_threshold"]
    exceptions: dict[str, frozenset] = allowlist["exceptions"]
    scoped_constants: dict[str, list[str]] = allowlist.get("scoped_constants", {})
    group_map = group_map or {}

    parsed_files, occurrence_counts = collect_numeric_literals(root, scan_dirs)
    lintable_values = {
        value: labels
        for value, labels in value_map.items()
        if value not in skip_values and occurrence_counts.get(value, 0) <= threshold
    }

    violations: list[str] = []
    constants_abs = constants_file.resolve()
    for parsed_file in parsed_files:
        if parsed_file.path.resolve() == constants_abs:
            continue

        rel_path = parsed_file.rel_path
        file_allowed: frozenset = exceptions.get(rel_path, frozenset())
        for lineno, value in parsed_file.literals:
            if value not in lintable_values or value in file_allowed:
                continue
            if _value_is_scoped_out(value, rel_path, group_map, scoped_constants):
                continue
            line_text = (
                parsed_file.lines[lineno - 1] if lineno <= len(parsed_file.lines) else ""
            )
            if "# magic-ok" in line_text:
                continue

            labels = ", ".join(lintable_values[value])
            violations.append(f"{rel_path}:{lineno} — value {value} is defined in {labels}")

    return violations


def run(
    *,
    root: Path | None = None,
    config_path: Path | None = None,
    constants_path: Path | None = None,
    allowlist_path: Path | None = None,
    scan_dirs: list[str] | None = None,
) -> int:
    """Run the configured magic-number membership lint."""
    config = load_config(config_path)
    scan_root = (root or config.root).resolve()

    configured_constants = constants_path
    if configured_constants is None and config.hooks.magic_numbers.constants is not None:
        configured_constants = Path(config.hooks.magic_numbers.constants)
    if configured_constants is None:
        print(
            "Magic-number lint: no constants module configured; "
            "constants-membership checks skipped."
        )
        return 0

    constants_file = (scan_root / configured_constants).resolve()
    configured_allowlist = allowlist_path or Path(config.hooks.magic_numbers.allowlist)
    allowlist_file = (scan_root / configured_allowlist).resolve()
    configured_scan_dirs = (
        scan_dirs if scan_dirs is not None else config.hooks.magic_numbers.scan_dirs
    )
    if configured_scan_dirs is None:
        configured_scan_dirs = config.codemap.scan_dirs

    if not constants_file.exists():
        print(f"ERROR: constants file not found: {constants_file}", file=sys.stderr)
        return 2

    try:
        module = load_constants_module(constants_file)
    except Exception as exc:
        message = " ".join(str(exc).split())
        print(
            f"error: invalid constants module: {type(exc).__name__}: {message}",
            file=sys.stderr,
        )
        return 2
    value_map = extract_constant_map(module)
    group_map = extract_group_map(module)
    if not value_map:
        print("No numeric constants found in the configured module — nothing to lint.")
        return 0

    violations = scan(
        scan_root,
        constants_file,
        load_allowlist(allowlist_file),
        value_map,
        scan_dirs=tuple(configured_scan_dirs),
        group_map=group_map,
    )
    if violations:
        print(f"Magic-number lint: {len(violations)} violation(s) found\n")
        for violation in violations:
            print(f"  {violation}")
        print(
            "\nFix: use the named constant, add a # magic-ok: <reason> comment, "
            "or add the file/value pair to the configured allowlist."
        )
        return 1

    print("Magic-number lint: clean.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Lint for magic numbers duplicating values in a configured constants module."
    )
    parser.add_argument(
        "--constants",
        type=Path,
        default=None,
        help="Constants module path (default: hooks.magic_numbers.constants from corral.yaml)",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="Repository root (default: directory of corral.yaml, else cwd)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to corral.yaml (default: nearest corral.yaml above cwd)",
    )
    parser.add_argument(
        "--allowlist",
        type=Path,
        default=None,
        help="Allowlist YAML path (default: hooks.magic_numbers.allowlist)",
    )
    parser.add_argument(
        "--scan-dirs",
        nargs="+",
        default=None,
        metavar="DIR",
        help="Directories relative to --root (default: hooks setting or codemap.scan_dirs)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return run(
        root=args.root,
        config_path=args.config,
        constants_path=args.constants,
        allowlist_path=args.allowlist,
        scan_dirs=args.scan_dirs,
    )


if __name__ == "__main__":
    sys.exit(main())
