"""Check staged files against declared high-risk surfaces.

The hook matches staged file paths and changed line ranges against a surfaces
registry. It warns for every match and exits non-zero when a matched surface
has ``needs_human: true``. ``--warn-only`` acknowledges the findings by
downgrading blocking matches to warnings.

Exit codes:
    0 -- no surfaces hit, warn-only mode, or only warning surfaces hit
    1 -- one or more surfaces with ``needs_human: true`` are touched
    2 -- the configured surfaces registry is invalid
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from corral.config import load_config


@dataclass
class Surface:
    """One monitored surface from the registry."""

    name: str
    description: str
    paths: list[str]
    line_ranges: list[str]
    needs_human: bool
    needs_shadow_run: bool = False
    needs_equivalence_check: bool = False
    needs_validation: bool = False
    notes: str = ""
    yaml_block_selectors: list[str] = field(default_factory=list)


@dataclass
class Hit:
    """A surface and the staged changes that matched it."""

    surface: Surface
    matched_files: list[str] = field(default_factory=list)
    matched_line_ranges: list[str] = field(default_factory=list)

    @property
    def blocks(self) -> bool:
        return self.surface.needs_human


def load_surfaces(surfaces_path: Path) -> list[Surface]:
    """Load surfaces from *surfaces_path*."""
    data = yaml.safe_load(surfaces_path.read_text(encoding="utf-8"))
    surfaces = []
    for name, attrs in data.get("surfaces", {}).items():
        surfaces.append(
            Surface(
                name=name,
                description=attrs.get("description", ""),
                paths=attrs.get("paths", []),
                line_ranges=attrs.get("line_ranges", []),
                needs_human=attrs.get("needs_human", False),
                needs_shadow_run=attrs.get("needs_shadow_run", False),
                needs_equivalence_check=attrs.get("needs_equivalence_check", False),
                needs_validation=attrs.get("needs_validation", False),
                notes=attrs.get("notes", ""),
                yaml_block_selectors=attrs.get("yaml_block_selectors", []),
            )
        )
    return surfaces


def get_staged_files() -> list[str]:
    """Return repository-relative paths currently staged in Git."""
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "-z"],
        capture_output=True,
        check=True,
    )
    stdout = result.stdout if isinstance(result.stdout, bytes) else result.stdout.encode()
    return [
        path.decode("utf-8", errors="replace")
        for path in stdout.split(b"\0")
        if path
    ]


_GIT_C_ESCAPES = {
    "a": b"\a",
    "b": b"\b",
    "t": b"\t",
    "n": b"\n",
    "v": b"\v",
    "f": b"\f",
    "r": b"\r",
    '"': b'"',
    "\\": b"\\",
}


def _decode_git_path(path: str) -> str:
    """Decode a Git C-quoted pathname, including octal-escaped UTF-8 bytes."""
    if len(path) < 2 or not (path.startswith('"') and path.endswith('"')):
        return path

    quoted = path[1:-1]
    decoded = bytearray()
    index = 0
    while index < len(quoted):
        char = quoted[index]
        if char != "\\":
            decoded.extend(char.encode("utf-8"))
            index += 1
            continue

        index += 1
        if index >= len(quoted):
            decoded.extend(b"\\")
            break
        escaped = quoted[index]
        if escaped in "01234567":
            end = index + 1
            while end < min(index + 3, len(quoted)) and quoted[end] in "01234567":
                end += 1
            decoded.append(int(quoted[index:end], 8))
            index = end
            continue
        decoded.extend(_GIT_C_ESCAPES.get(escaped, escaped.encode("utf-8")))
        index += 1

    return decoded.decode("utf-8", errors="replace")


def get_staged_hunks() -> dict[str, list[tuple[int, int]]]:
    """Return exact new-file line ranges for staged changes, keyed by path.

    Deletion-only hunks are skipped because they do not touch a line range in
    the new file. Fully deleted and binary files have no usable hunk data and
    are conservatively treated as whole-file matches by :func:`check_surfaces`.
    """
    result = subprocess.run(
        ["git", "diff", "--cached", "--unified=0"],
        capture_output=True,
        check=True,
    )

    file_hunks: dict[str, list[tuple[int, int]]] = {}
    current_file: str | None = None
    file_re = re.compile(r"^\+\+\+ (.+)$")
    hunk_re = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
    stdout = result.stdout if isinstance(result.stdout, bytes) else result.stdout.encode()

    for line in stdout.decode("utf-8", errors="replace").splitlines():
        match = file_re.match(line)
        if match:
            path = _decode_git_path(match.group(1))
            current_file = path[2:] if path.startswith("b/") else None
            if current_file is None:
                continue
            file_hunks.setdefault(current_file, [])
            continue

        match = hunk_re.match(line)
        if match and current_file is not None:
            start = int(match.group(1))
            count = int(match.group(2)) if match.group(2) is not None else 1
            if count:
                file_hunks[current_file].append((start, start + count - 1))

    return file_hunks


def _parse_line_range(range_str: str) -> tuple[str, int, int]:
    path_part, range_part = range_str.rsplit(":", 1)
    start_str, end_str = range_part.split("-")
    return path_part, int(start_str), int(end_str)


def _ranges_overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    return a_start <= b_end and b_start <= a_end


def check_surfaces(
    surfaces: list[Surface],
    staged_files: list[str],
    staged_hunks: dict[str, list[tuple[int, int]]],
) -> list[Hit]:
    """Return one hit per surface that overlaps the staged changes."""
    staged_set = set(staged_files)
    hits: list[Hit] = []

    for surface in surfaces:
        matched_files: list[str] = []
        matched_ranges: list[str] = []

        for path in surface.paths:
            if path not in staged_set:
                continue

            file_line_ranges = [
                item for item in surface.line_ranges if item.startswith(path + ":")
            ]
            if not file_line_ranges:
                if path not in matched_files:
                    matched_files.append(path)
                continue

            file_hunks = staged_hunks.get(path, [])
            if not file_hunks:
                if path not in matched_files:
                    matched_files.append(path)
                continue

            for range_str in file_line_ranges:
                _, range_start, range_end = _parse_line_range(range_str)
                for hunk_start, hunk_end in file_hunks:
                    if _ranges_overlap(hunk_start, hunk_end, range_start, range_end):
                        if path not in matched_files:
                            matched_files.append(path)
                        if range_str not in matched_ranges:
                            matched_ranges.append(range_str)
                        break

        if matched_files:
            hits.append(
                Hit(
                    surface=surface,
                    matched_files=matched_files,
                    matched_line_ranges=matched_ranges,
                )
            )

    return hits


def _format_hit(hit: Hit) -> str:
    tag = "BLOCK" if hit.blocks else "WARN"
    lines = [
        f"  [{tag}] {hit.surface.name}: {hit.surface.description}",
        f"         files: {', '.join(hit.matched_files)}",
    ]
    if hit.matched_line_ranges:
        lines.append(f"         line ranges: {', '.join(hit.matched_line_ranges)}")
    flags = [
                flag
                for flag, enabled in [
                    ("needs_human", hit.surface.needs_human),
                    ("needs_shadow_run", hit.surface.needs_shadow_run),
                    ("needs_equivalence_check", hit.surface.needs_equivalence_check),
                    ("needs_validation", hit.surface.needs_validation),
                ]
                if enabled
            ]
    if flags:
        lines.append(f"         flags: {', '.join(flags)}")
    if hit.surface.notes:
        lines.append(f"         notes: {hit.surface.notes}")
    return "\n".join(lines)


def find_repo_root() -> Path:
    """Return the current Git worktree root."""
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=True,
    )
    return Path(result.stdout.strip())


def run(
    *,
    warn_only: bool = False,
    surfaces_path: Path | None = None,
    config_path: Path | None = None,
) -> int:
    """Run the staged-surface check with resolved configuration."""
    repo_root = find_repo_root()
    config = load_config(config_path)
    config_root = config.root if config.source_path is not None else repo_root
    registry = surfaces_path or (config_root / config.hooks.surfaces)

    if not registry.exists():
        print(f"surface-check: surfaces.yaml not found at {registry}, skipping.", file=sys.stderr)
        return 0

    try:
        surfaces = load_surfaces(registry)
    except (OSError, UnicodeError, yaml.YAMLError, AttributeError, TypeError, ValueError) as exc:
        message = " ".join(str(exc).split())
        print(f"error: invalid surfaces.yaml: {message}", file=sys.stderr)
        return 2
    staged_files = get_staged_files()
    if not staged_files:
        return 0

    hits = check_surfaces(surfaces, staged_files, get_staged_hunks())
    if not hits:
        return 0

    blocking = [hit for hit in hits if hit.blocks]
    warnings = [hit for hit in hits if not hit.blocks]

    if warnings:
        print("surface-check: WARNINGS — staged files touch monitored surfaces:")
        for hit in warnings:
            print(_format_hit(hit))
        print()

    if blocking:
        verb = "WARNINGS (--warn-only)" if warn_only else "BLOCKED"
        print(f"surface-check: {verb} — staged files touch human-review surfaces:")
        for hit in blocking:
            print(_format_hit(hit))
        print()
        if not warn_only:
            print("Ensure a human reviews these surfaces before merging.")
            print("Use --warn-only to downgrade blocks to warnings.")
            return 1

    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--warn-only", action="store_true", help="Never block; always exit 0.")
    parser.add_argument("--surfaces", type=Path, default=None, help="Path to surfaces.yaml.")
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to corral.yaml (default: nearest corral.yaml above cwd)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return run(
        warn_only=args.warn_only,
        surfaces_path=args.surfaces,
        config_path=args.config,
    )


if __name__ == "__main__":
    sys.exit(main())
