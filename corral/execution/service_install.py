"""Render the generic launchd service definition from explicit local paths."""
from __future__ import annotations

import argparse
import plistlib
from pathlib import Path
from xml.sax.saxutils import escape


def render(template: Path, *, label: str, executable: Path, config: Path,
           interval_seconds: int, log_dir: Path) -> bytes:
    if not label or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-_" for char in label):
        raise ValueError("launchd label contains unsupported characters")
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")
    for name, path in (("executable", executable), ("config", config), ("log_dir", log_dir)):
        if not path.is_absolute():
            raise ValueError(f"{name} must be an absolute path")
    value = template.read_text()
    replacements = {"@LABEL@": escape(label), "@CORRAL_SERVICE@": escape(str(executable)),
                    "@CONFIG@": escape(str(config)), "@INTERVAL_SECONDS@": str(interval_seconds),
                    "@LOG_DIR@": escape(str(log_dir))}
    for marker, replacement in replacements.items():
        value = value.replace(marker, replacement)
    encoded = value.encode()
    plistlib.loads(encoded)
    return encoded


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template", required=True, type=Path)
    parser.add_argument("--label", required=True)
    parser.add_argument("--executable", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--interval-seconds", required=True, type=int)
    parser.add_argument("--log-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    content = render(args.template, label=args.label, executable=args.executable,
                     config=args.config, interval_seconds=args.interval_seconds,
                     log_dir=args.log_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
