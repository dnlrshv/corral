"""Legacy Gemini review runner adapter for selected-cohort authority fencing."""

from __future__ import annotations

import contextlib
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional

from .store import Store


def _find_config_path(
    state_dir: Optional[Path] = None, cohort_control_path: Optional[Path] = None
) -> Optional[Path]:
    target_path = cohort_control_path or (
        Path(os.environ["GEMINI_REVIEW_COHORT_CONTROL"])
        if "GEMINI_REVIEW_COHORT_CONTROL" in os.environ
        else None
    )
    if target_path is not None:
        p = Path(target_path).resolve()
        if not p.exists():
            raise FileNotFoundError(f"Cohort control configuration at '{p}' not found")
        return p
    if state_dir is not None:
        default_db = state_dir / "cohort_authority.db"
        if default_db.exists():
            return default_db
        default_json = state_dir / "cohort_control.json"
        if default_json.exists():
            return default_json
    return None


def get_selected_cohort_set(config_path: Path) -> tuple[set[str], Path]:
    """Parse configuration, validate full repo#PR identities, and return (selected_set, db_file)."""
    p = Path(config_path).resolve()
    if not p.exists():
        raise FileNotFoundError(f"Cohort control configuration at '{p}' not found")
    if p.suffix in (".db", ".sqlite"):
        store = Store(p)
        with store.transaction() as db:
            rows = db.execute("SELECT resource FROM owners WHERE resource LIKE 'pr:%'").fetchall()
            selected = {r[0][3:] for r in rows if len(r[0]) > 3}
        return selected, p

    try:
        content = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"Malformed cohort control configuration at '{p}': {exc}") from exc
    if not isinstance(content, dict):
        raise ValueError(f"Cohort control at '{p}' must be a JSON dictionary")

    selected: set[str] = set()
    cohorts = content.get("cohorts") or {}
    if not isinstance(cohorts, dict):
        raise ValueError("cohorts must be a mapping")
    for k in cohorts:
        if not isinstance(k, str) or "#" not in k or not k.split("#")[0] or not k.split("#")[1].isdigit():
            raise ValueError(f"invalid cohort identifier: '{k}' must be full repo#PR identity")
        selected.add(k)

    fenced = content.get("fenced_prs") or []
    if not isinstance(fenced, list):
        raise ValueError("fenced_prs must be a list")
    for f_pr in fenced:
        if not isinstance(f_pr, str) or "#" not in f_pr or not f_pr.split("#")[0] or not f_pr.split("#")[1].isdigit():
            raise ValueError(f"invalid fenced PR identifier: '{f_pr}' must be full repo#PR identity")
        selected.add(f_pr)

    db_file = Path(content["authority_db"]).resolve() if "authority_db" in content else p.parent / (p.stem + "_authority.db")
    return selected, db_file


def bootstrap_cohort_store(
    cohort_control_path: Path, authority_db_path: Optional[Path] = None
) -> Store:
    """Explicitly bootstrap authority store from configuration."""
    cfg = Path(cohort_control_path).resolve()
    _selected, default_db = get_selected_cohort_set(cfg)
    content = json.loads(cfg.read_text(encoding="utf-8"))
    db_file = Path(authority_db_path or default_db).resolve()
    store = Store(db_file)
    with store.transaction() as db:
        for k, v in (content.get("cohorts") or {}).items():
            if isinstance(v, dict):
                db.execute(
                    "INSERT OR IGNORE INTO owners (resource, owner, epoch, status) VALUES (?, ?, ?, ?)",
                    ("pr:" + k, v.get("owner", "legacy"), v.get("epoch", 1), v.get("status", "active")),
                )
        for f_pr in (content.get("fenced_prs") or []):
            db.execute(
                "INSERT OR IGNORE INTO owners (resource, owner, epoch, status) VALUES (?, 'legacy', 1, 'fenced')",
                ("pr:" + f_pr,),
            )
    return store


def resolve_cohort_store(
    state_dir: Optional[Path] = None, cohort_control_path: Optional[Path] = None
) -> Optional[Store]:
    """Resolve active Store strictly without mutations, failing closed if missing."""
    cfg = _find_config_path(state_dir, cohort_control_path)
    if cfg is None:
        return None
    _selected, db_file = get_selected_cohort_set(cfg)
    if not db_file.exists():
        return None
    return Store(db_file)


def check_cohort_admission(
    *, repo: str, pr: int, state_dir: Optional[Path] = None, cohort_control_path: Optional[Path] = None
) -> tuple[bool, str, dict[str, Any]]:
    """Determine whether the legacy Gemini review runner has ownership authority for repo#pr."""
    cfg = _find_config_path(state_dir, cohort_control_path)
    if cfg is None:
        return False, "missing_cohort_config", {}

    try:
        selected_set, db_file = get_selected_cohort_set(cfg)
    except Exception as exc:
        return False, f"malformed_cohort_config: {exc}", {}

    pr_identity = f"{repo}#{pr}"
    if pr_identity not in selected_set:
        return True, "legacy_unmanaged", {
            "resource": f"pr:{pr_identity}",
            "owner": "legacy",
            "epoch": 0,
            "status": "unmanaged",
        }

    if not db_file.exists():
        return False, "missing_authority_db", {}

    store = Store(db_file)
    resource = f"pr:{pr_identity}"
    with store.transaction() as db:
        row = db.execute("SELECT owner, epoch, status FROM owners WHERE resource=?", (resource,)).fetchone()
        if not row:
            return False, "unmanaged_cohort", {}
        owner, epoch, status = row
        rec = {"resource": resource, "owner": owner, "epoch": epoch, "status": status}
        if status == "fenced":
            return False, "cohort_fenced", rec
        if owner == "legacy" and status == "active":
            return True, "active_legacy", rec
        return False, f"cohort_{status if owner == 'legacy' else 'owned_by_' + owner}", rec


@dataclass
class PRLease:
    repo: str
    pr: int
    head_sha: str
    store: Optional[Store]
    acquired: bool
    owner_record: dict[str, Any]
    epoch: int
    pid: int
    attempt_id: str
    has_ambiguous: bool = False

    def can_publish(self) -> bool:
        if not self.acquired or self.store is None:
            return self.acquired
        return self.store.can_publish_cas(
            resource=f"pr:{self.repo}#{self.pr}",
            owner=self.owner_record.get("owner", "legacy"),
            epoch=self.epoch,
            attempt_id=self.attempt_id,
            head_sha=self.head_sha,
        )

    def record_pending(self, intent: str, base_sha: str, payload: dict[str, Any]) -> None:
        if self.store is not None:
            self.store.record_intent_pending(
                intent=intent,
                resource=f"pr:{self.repo}#{self.pr}",
                owner=self.owner_record.get("owner", "legacy"),
                epoch=self.epoch,
                head_sha=self.head_sha,
                base_sha=base_sha,
                payload=payload,
                attempt_id=self.attempt_id,
            )

    def record_delivered(self, intent: str, review_id: Optional[int] = None) -> None:
        if self.store is not None:
            self.store.record_intent_outcome(
                intent=intent,
                resource=f"pr:{self.repo}#{self.pr}",
                status="delivered",
                review_id=review_id,
                attempt_id=self.attempt_id,
            )

    def record_ambiguous(self, intent: str, error: str) -> None:
        self.has_ambiguous = True
        if self.store is not None:
            self.store.record_intent_outcome(
                intent=intent,
                resource=f"pr:{self.repo}#{self.pr}",
                status="ambiguous",
                error=error,
                attempt_id=self.attempt_id,
            )


@contextlib.contextmanager
def pr_lease(
    state_dir: Optional[Path], repo: str, pr: int, head_sha: str, *, cohort_control_path: Optional[Path] = None
) -> Iterator[PRLease]:
    resource = f"pr:{repo}#{pr}"
    pid = os.getpid()
    attempt_id = str(uuid.uuid4())

    try:
        cfg = _find_config_path(state_dir, cohort_control_path)
    except Exception:
        cfg = None

    if cfg is None:
        yield PRLease(
            repo=repo, pr=pr, head_sha=head_sha, store=None, acquired=False,
            owner_record={"status": "missing_cohort_config"}, epoch=0, pid=pid, attempt_id=attempt_id
        )
        return

    try:
        selected_set, db_file = get_selected_cohort_set(cfg)
    except Exception as exc:
        yield PRLease(
            repo=repo, pr=pr, head_sha=head_sha, store=None, acquired=False,
            owner_record={"status": f"malformed_cohort_config: {exc}"}, epoch=0, pid=pid, attempt_id=attempt_id
        )
        return

    pr_identity = f"{repo}#{pr}"
    if pr_identity not in selected_set:
        yield PRLease(
            repo=repo, pr=pr, head_sha=head_sha, store=None, acquired=True,
            owner_record={"status": "legacy_unmanaged"}, epoch=0, pid=pid, attempt_id=attempt_id
        )
        return

    allowed, reason, rec = check_cohort_admission(
        repo=repo, pr=pr, state_dir=state_dir, cohort_control_path=cohort_control_path
    )
    if not allowed or not db_file.exists():
        yield PRLease(
            repo=repo, pr=pr, head_sha=head_sha, store=None, acquired=False,
            owner_record=rec or {"status": reason}, epoch=rec.get("epoch", 0) if rec else 0,
            pid=pid, attempt_id=attempt_id
        )
        return

    store = Store(db_file)
    acquired, _reason, rec, epoch = store.acquire_lease(resource, "legacy", head_sha, pid, attempt_id)
    lease = PRLease(
        repo=repo, pr=pr, head_sha=head_sha, store=store, acquired=acquired,
        owner_record=rec, epoch=epoch, pid=pid, attempt_id=attempt_id
    )
    try:
        yield lease
    finally:
        if store is not None and lease.acquired and not lease.has_ambiguous:
            store.release_lease(resource, attempt_id)
