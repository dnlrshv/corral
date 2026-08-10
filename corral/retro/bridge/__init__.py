"""File-backed evidence bridge for the weekly retrospective.

Splits the source ``memory_bridge`` module along its natural seams:

- :mod:`corral.retro.bridge.discovery` resolves configured evidence roots.
- :mod:`corral.retro.bridge.security` redacts credentials and enforces the
  outbound gate on every record field.
- :mod:`corral.retro.bridge.readers` loads sanitized records from memory
  corpora and structured run artifacts, and merges/renders them for mining.
"""

from corral.retro.bridge.readers import (
    load_bridge_evidence,
    load_memory_corpus,
    load_run_artifacts,
    merge_bridge_groups,
    render_bridge_evidence,
    render_group_evidence,
)
from corral.retro.bridge.security import UnsafeBridgeRecordError, assert_safe_record, sanitize_text

__all__ = [
    "UnsafeBridgeRecordError",
    "assert_safe_record",
    "load_bridge_evidence",
    "load_memory_corpus",
    "load_run_artifacts",
    "merge_bridge_groups",
    "render_bridge_evidence",
    "render_group_evidence",
    "sanitize_text",
]
