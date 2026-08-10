"""Emit an additive reminder when an agent edits a declared surface.

This command implements a Claude Code ``PreToolUse`` hook. It reads a JSON
tool payload from stdin and prints a reminder when ``tool_input.file_path``
matches a path in the surfaces registry. It always exits successfully and
never blocks the requested edit.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import yaml

from corral.config import Config, find_config_file, load_config


def _load_surfaces(surfaces_path: Path) -> dict:
    if not surfaces_path.exists():
        return {}
    data = yaml.safe_load(surfaces_path.read_text(encoding="utf-8"))
    return data.get("surfaces", {}) if data else {}


def _match_surfaces(rel_path: str, surfaces: dict) -> list[tuple[str, dict]]:
    matches = []
    for name, surface in surfaces.items():
        for surface_path in surface.get("paths", []):
            if rel_path == surface_path:
                matches.append((name, surface))
                break
    return matches


def _rel_path(file_path: str, project_root: Path) -> str:
    try:
        return str(Path(file_path).relative_to(project_root))
    except ValueError:
        return file_path


def _format_reminder(matches: list[tuple[str, dict]]) -> str:
    parts = [
        "[surface-reminder] Editing a registered high-risk surface — "
        "proceed with extra care."
    ]
    for name, surface in matches:
        parts.append(f"\n  Surface:     {name}")
        if description := surface.get("description"):
            parts.append(f"  Description: {description}")
        flags = [
            label
            for label, key in (
                ("needs_human", "needs_human"),
                ("needs_shadow_run", "needs_shadow_run"),
                ("needs_equivalence_check", "needs_equivalence_check"),
                ("needs_validation", "needs_validation"),
            )
            if surface.get(key)
        ]
        if flags:
            parts.append(f"  Gates:       {', '.join(flags)}")
        if notes := surface.get("notes"):
            parts.append(f"  Notes:       {notes}")
    return "\n".join(parts)


def _project_root() -> Path:
    env_value = os.environ.get("CLAUDE_PROJECT_DIR")
    if env_value:
        return Path(env_value)
    return Path.cwd()


def _config_for_project(project_root: Path, config_path: Path | None) -> Config:
    if config_path is not None:
        return load_config(config_path)
    found = find_config_file(project_root)
    return load_config(found) if found is not None else Config()


def run(
    *,
    config_path: Path | None = None,
    surfaces_path: Path | None = None,
) -> int:
    """Read one hook payload from stdin and emit any matching reminder."""
    try:
        payload = json.loads(sys.stdin.read())
        file_path = payload.get("tool_input", {}).get("file_path", "")
        if not file_path:
            return 0

        project_root = _project_root()
        config = _config_for_project(project_root, config_path)
        config_root = config.root if config.source_path is not None else project_root
        registry = surfaces_path or (config_root / config.hooks.surfaces)
        relative_path = _rel_path(file_path, project_root)
        matches = _match_surfaces(relative_path, _load_surfaces(registry))
        if matches:
            print(_format_reminder(matches))
    except Exception:
        # Pre-tool reminders are deliberately fail-open: malformed payloads,
        # missing files, and configuration errors must not block an edit.
        pass
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to corral.yaml (default: nearest file above the project root)",
    )
    parser.add_argument("--surfaces", type=Path, default=None, help="Path to surfaces.yaml.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return run(config_path=args.config, surfaces_path=args.surfaces)


if __name__ == "__main__":
    sys.exit(main())
