"""Seatbelt containment for candidate-code test verification on native runs.

Verifiers retain workspace read/write and controller-owned verifier bundles, but never
inherit publisher or provider account stores merely because they share a macOS user.
This is a scoped file boundary, not hostile-same-UID isolation.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import containment

SENTINEL_NAME = ".corral-verifier-containment-sentinel"


@dataclass(frozen=True)
class Prepared:
    boundary: containment.Boundary
    profile: str
    evidence: dict


def _inside(path: Path, roots: tuple[Path, ...]) -> bool:
    return any(path == root or root in path.parents for root in roots)


def prepare(*, workspace: str | Path, state_dir: str | Path, artifacts: str | Path,
            task_dir: str | Path, task_id: str, attempt: str, protected_paths: tuple[str, ...] = (),
            probe_sentinels: tuple[str, ...] = ()) -> Prepared:
    """Build and prove the verifier boundary before candidate tests execute.

    ``probe_sentinels`` are controller host configuration for disposable fixture files.
    They must already be regular files under a protected root and are never task input.
    """
    root, state = Path(workspace).resolve(), Path(state_dir).resolve()
    artifact_root, output = Path(artifacts).resolve(), Path(task_dir).resolve()
    protected = tuple(Path(item).expanduser().resolve() for item in protected_paths)
    scratch_root = state / "verifier-scratch"
    scratch = scratch_root / task_id / attempt
    scratch.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)
    denied = {str(state), str(artifact_root), *(str(item) for item in protected),
              *containment.sensitive_account_stores()}
    supplied = tuple(Path(item).expanduser().resolve() for item in probe_sentinels)
    invalid = [str(item) for item in supplied
               if not item.is_file() or item.is_symlink() or not _inside(item, protected)]
    if invalid:
        raise PermissionError("verifier probe sentinels must be regular files below protected paths: "
                              + ", ".join(sorted(invalid)))
    generated = (
        containment.write_sentinel(state / "sentinels" / f"{task_id}-{attempt}.verifier-state",
                                   "controller state during verifier execution"),
        containment.write_sentinel(output / SENTINEL_NAME,
                                   "trusted verifier receipt directory"),
    )
    boundary = containment.Boundary(
        workspace=str(root), scratch=str(scratch), tmpdir=str(scratch), deny=tuple(sorted(denied)),
        sentinels=tuple(sorted({*generated, *(str(item) for item in supplied)})),
        label="seatbelt-test-verifier-boundary",
    )
    overlaps = containment.refuse_overlaps(boundary, scratch_root=str(scratch_root))
    if overlaps:
        raise PermissionError("test verifier boundary overlaps trusted state: " + "; ".join(overlaps))
    evidence = containment.require(boundary)
    return Prepared(boundary=boundary, profile=containment.build_profile(boundary), evidence=evidence)
