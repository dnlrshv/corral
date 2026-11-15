"""Plain, reasoned pointer templates shared by coding agents."""
from __future__ import annotations

import os
from pathlib import Path

POINTER_IMPORT = '''@{canonical_rel}

The shared agent instructions for this repository are in {canonical}, imported above. Keep repository rules in {canonical} so that every coding agent reads the same text, and keep only agent-specific settings in this file.
'''
POINTER_REFERENCE = '''The shared agent instructions for this repository are in [{canonical}]({canonical_rel}). Read that file before you change anything, because it holds the repository's workflow, test and review rules for every coding agent. Keep repository rules in {canonical} and keep only agent-specific settings in this file.
'''


def render_pointer(spec, canonical: str, root: Path) -> str:
    relative = os.path.relpath(root / canonical, (root / spec.path).parent).replace(os.sep, '/')
    template = POINTER_IMPORT if spec.style == 'import' else POINTER_REFERENCE
    return template.format(canonical=canonical, canonical_rel=relative)
