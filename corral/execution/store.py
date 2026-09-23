"""Single-controller transactional state, request deduplication and fencing."""
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


def lease_holder_alive(pid, acquired_at: float | None = None) -> bool:
    """Whether a lease's local holder process still exists.

    A pid that names no process holds nothing, and neither does an exited holder
    that its parent has not reaped yet (a zombie) or a process that started after
    the lease was acquired: its pid was reused. An unobservable process fails closed.
    """
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        pass  # The process exists but belongs to another user.
    # Read the clock before ``ps``: a slow ``ps`` must not age a live holder into a
    # process that started after its lease.
    now = time.time()
    observed = _process_state(pid)
    if observed is None:
        return True
    state, elapsed = observed
    if state.startswith("Z"):
        return False
    # ``ps`` reports whole seconds; allow that rounding before calling the pid reused.
    # An unreported age fails closed.
    return acquired_at is None or elapsed is None or elapsed + 2 >= now - acquired_at


def _process_state(pid: int) -> tuple[str, int | None] | None:
    """The ``ps`` state code of ``pid`` and its elapsed seconds when ``ps`` reports them.

    None means the process state is unobservable.
    """
    try:
        raw = subprocess.run(["ps", "-o", "stat=", "-o", "etime=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=10,
                             check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    if not raw:
        return None
    state, *etime = raw.split(None, 1)
    match = re.fullmatch(r"(?:(?:(\d+)-)?(\d+):)?(\d+):(\d+)", "".join(etime))
    if not match:
        return state, None
    days, hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return state, ((days * 24 + hours) * 60 + minutes) * 60 + seconds


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.transaction() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS records(kind TEXT, key TEXT, value TEXT,
                    PRIMARY KEY(kind,key));
                CREATE TABLE IF NOT EXISTS owners(resource TEXT PRIMARY KEY,
                    owner TEXT, epoch INTEGER, status TEXT);
                CREATE TABLE IF NOT EXISTS leases (
                    resource TEXT PRIMARY KEY, owner TEXT NOT NULL, epoch INTEGER NOT NULL,
                    holder_pid INTEGER NOT NULL, attempt_id TEXT NOT NULL, head_sha TEXT NOT NULL, acquired_at REAL NOT NULL, status TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS publication_intents (
                    intent TEXT PRIMARY KEY, resource TEXT NOT NULL, owner TEXT NOT NULL, epoch INTEGER NOT NULL,
                    head_sha TEXT NOT NULL, base_sha TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL,
                    error TEXT, review_id INTEGER, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    attempt_id TEXT NOT NULL DEFAULT ''
                );
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(publication_intents)")}
            if "attempt_id" not in columns:
                db.execute("ALTER TABLE publication_intents ADD COLUMN attempt_id TEXT NOT NULL DEFAULT ''")

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def put_once(self, kind, key, value):
        raw = canonical(value)
        with self.transaction() as db:
            old = db.execute("SELECT value FROM records WHERE kind=? AND key=?",
                             (kind, key)).fetchone()
            if old:
                if old[0] != raw:
                    raise ValueError("conflicting idempotency identity")
                return False
            db.execute("INSERT INTO records VALUES(?,?,?)", (kind, key, raw))
            return True

    def get(self, kind, key):
        with self.transaction() as db:
            row = db.execute("SELECT value FROM records WHERE kind=? AND key=?",
                             (kind, key)).fetchone()
        return json.loads(row[0]) if row else None

    def records(self, kind):
        with self.transaction() as db:
            rows = db.execute("SELECT key,value FROM records WHERE kind=? ORDER BY key",
                              (kind,)).fetchall()
        return {k: json.loads(v) for k, v in rows}

    def replace(self, kind, key, value):
        with self.transaction() as db:
            db.execute("INSERT OR REPLACE INTO records VALUES(?,?,?)",
                       (kind, key, canonical(value)))

    def append_ordered(self, kind, key, value):
        with self.transaction() as db:
            old = db.execute("SELECT value FROM records WHERE kind=? AND key=?", (kind, key)).fetchone()
            if old:
                if json.loads(old[0])["value"] != value:
                    raise ValueError("conflicting amendment identity")
                return
            seq = db.execute("SELECT COUNT(*) FROM records WHERE kind=?", (kind,)).fetchone()[0] + 1
            db.execute("INSERT INTO records VALUES(?,?,?)", (kind, key, canonical({"sequence": seq, "value": value})))

    def allocate(self, task, host, cpu, memory_mb, capacity, *, reservation=None, db=None):
        """Allocate registered host capacity to one task; the store is the only capacity authority.

        Service admission passes ``reservation`` (its event id) so capacity is held from the
        moment the event is claimed; the controller dispatch that runs the task adopts that
        reservation instead of counting the task twice. Returns False, writing nothing, while
        the task already holds a live allocation; a refusal never leaves a partial record.
        """
        if db is None:
            with self.transaction() as opened:
                return self.allocate(task, host, cpu, memory_mb, capacity,
                                     reservation=reservation, db=opened)
        rows = db.execute("SELECT key,value FROM records WHERE kind='allocation'").fetchall()
        allocations = {k: json.loads(v) for k, v in rows}
        own = allocations.get(task)
        if own and own["active"]:
            held = own.get("reservation")
            if held is None or reservation not in (None, held):
                return False
        active = [v for k, v in allocations.items() if k != task and v["active"] and v["host"] == host]
        if cpu <= 0 or memory_mb < 0:
            raise ValueError("invalid resource request")
        if sum(v["cpu"] for v in active) + cpu > capacity["cpu"] or sum(v["memory_mb"] for v in active) + memory_mb > capacity["memory_mb"]:
            raise PermissionError("registered host capacity unavailable")
        value = {"host": host, "cpu": cpu, "memory_mb": memory_mb, "active": True}
        if reservation is not None:
            value["reservation"] = reservation
        db.execute("INSERT OR REPLACE INTO records VALUES('allocation',?,?)", (task, canonical(value)))
        return True

    def active_allocations(self, host):
        return [value for value in self.records("allocation").values()
                if value.get("active") and value.get("host") == host]

    def release_allocation(self, task):
        value = self.get("allocation", task)
        if value:
            self.replace("allocation", task, {**value, "active": False})

    def release_reservation(self, task, reservation):
        """Release a service admission reservation that no controller dispatch adopted."""
        with self.transaction() as db:
            row = db.execute("SELECT value FROM records WHERE kind='allocation' AND key=?",
                             (task,)).fetchone()
            value = json.loads(row[0]) if row else None
            if not value or not value["active"] or value.get("reservation") != reservation:
                return False
            db.execute("INSERT OR REPLACE INTO records VALUES('allocation',?,?)",
                       (task, canonical({**value, "active": False})))
            return True

    def begin_dispatch(self, *, resource, task, claim_key, claim, host, cpu, memory_mb, capacity,
                       state, invocation):
        """Make this call the one dispatcher of a task generation, or write nothing at all.

        Claim, host allocation, workspace ownership, dispatching state and invocation commit
        in one transaction, so every refusal (generation already claimed, a live dispatcher of
        the same task, busy workspace, invalid or unavailable capacity) leaves no residue to
        strand. Returns ``(epoch, newly_acquired)``, or None when another dispatch owns it.
        """
        with self.transaction() as db:
            if db.execute("SELECT 1 FROM records WHERE kind='claim' AND key=?", (claim_key,)).fetchone():
                return None
            if not self.allocate(task, host, cpu, memory_mb, capacity, db=db):
                return None
            epoch, newly_acquired = self._acquire(db, resource, task)
            db.execute("INSERT INTO records VALUES('claim',?,?)", (claim_key, canonical(claim)))
            db.execute("INSERT OR REPLACE INTO records VALUES('state',?,?)",
                       (task, canonical({**state, "epoch": epoch})))
            db.execute("INSERT INTO records VALUES('invocation',?,?)",
                       (claim["attempt"], canonical(invocation)))
            return epoch, newly_acquired

    def finish_attempt(self, **kwargs):
        from .recovery_store import finish_attempt

        return finish_attempt(self, **kwargs)

    def mark_worker_launch(self, **kwargs):
        from .recovery_store import mark_worker_launch

        return mark_worker_launch(self, **kwargs)

    def settle_cancelled(self, *, resource, owner, epoch, task, generation, result, audit, expected_state, state,
                         owner_from=("uncertain",)):
        from .recovery_store import settle_cancelled

        return settle_cancelled(self, resource=resource, owner=owner, epoch=epoch, task=task,
                                generation=generation, result=result, audit=audit,
                                expected_state=expected_state, state=state, owner_from=owner_from)

    def owned_operation(self, resource, owner, epoch, kind, key, value):
        with self.transaction() as db:
            row = db.execute("SELECT owner,epoch,status FROM owners WHERE resource=?", (resource,)).fetchone()
            if row != (owner, epoch, "active"):
                raise PermissionError("stale operation fence")
            old = db.execute("SELECT value FROM records WHERE kind=? AND key=?", (kind, key)).fetchone()
            raw = canonical(value)
            if old and old[0] != raw:
                raise ValueError("conflicting operation identity")
            if not old:
                db.execute("INSERT INTO records VALUES(?,?,?)", (kind, key, raw))
                return True
            return False

    def owned_once(self, resource, owner, epoch, kind, key, identity, first):
        """Persist ``{**first, **identity}`` once under an owner fence; return the stored record.

        ``first`` carries fields fixed by the first write, such as a first-build time; an
        ``identity`` field of the same name takes precedence over it. A retry
        with the same ``identity`` gets the stored record back unchanged, so values derived from
        it stay reproducible; a different identity under the same key is refused.
        """
        expected = json.loads(canonical(identity))
        with self.transaction() as db:
            row = db.execute("SELECT owner,epoch,status FROM owners WHERE resource=?", (resource,)).fetchone()
            if row != (owner, epoch, "active"):
                raise PermissionError("stale operation fence")
            old = db.execute("SELECT value FROM records WHERE kind=? AND key=?", (kind, key)).fetchone()
            if old:
                stored = json.loads(old[0])
                if {name: stored.get(name) for name in expected} != expected:
                    raise ValueError("conflicting operation identity")
                return stored
            raw = canonical({**first, **identity})
            db.execute("INSERT INTO records VALUES(?,?,?)", (kind, key, raw))
            return json.loads(raw)

    def acquire(self, resource, owner):
        with self.transaction() as db:
            return self._acquire(db, resource, owner)[0]

    @staticmethod
    def _acquire(db, resource, owner):
        """Return ``(epoch, newly_acquired)``; re-entry by the active owner acquires nothing."""
        row = db.execute("SELECT owner,epoch,status FROM owners WHERE resource=?",
                         (resource,)).fetchone()
        if row and row[2] != "released":
            if row[0] == owner and row[2] == "active":
                return row[1], False
            raise PermissionError("resource still owned or outcome uncertain")
        epoch = row[1] + 1 if row else 1
        db.execute("INSERT OR REPLACE INTO owners VALUES(?,?,?,?)",
                   (resource, owner, epoch, "active"))
        return epoch, True

    def ownership(self, resource):
        with self.transaction() as db:
            return db.execute("SELECT owner,epoch,status FROM owners WHERE resource=?",
                              (resource,)).fetchone()

    def transition_owner(self, resource, owner, epoch, status):
        if status not in ("released", "uncertain", "active", "draining"):
            raise ValueError("invalid ownership state")
        with self.transaction() as db:
            result = db.execute("UPDATE owners SET status=? WHERE resource=? AND owner=? AND epoch=?",
                                (status, resource, owner, epoch))
            if not result.rowcount:
                raise PermissionError("stale ownership fence")
    def acquire_lease(self, resource: str, owner: str, head_sha: str, pid: int, attempt_id: str | None = None):
        if not attempt_id:
            attempt_id = str(uuid.uuid4())
        with self.transaction() as db:
            row = db.execute("SELECT owner, epoch, status FROM owners WHERE resource=?", (resource,)).fetchone()
            if not row:
                return False, "unmanaged_cohort", {}, 0
            owner_name, epoch, status = row
            rec = {
                "resource": resource, "owner": owner_name,
                "epoch": epoch, "status": status,
            }
            if owner_name != owner or status != "active":
                return False, f"cohort_{status if owner_name == owner else 'owned_by_' + owner_name}", rec, epoch

            lease_row = db.execute("SELECT holder_pid, acquired_at, status FROM leases WHERE resource=?", (resource,)).fetchone()
            if lease_row:
                _l_pid, _l_acquired, l_status = lease_row
                if l_status in ("in_flight", "active"):
                    return False, "concurrent_lease_active", rec, epoch
                elif l_status == "ambiguous":
                    return False, "ambiguous_lease_blocking", rec, epoch

            int_row = db.execute(
                "SELECT intent, status FROM publication_intents "
                "WHERE resource=? AND status IN ('pending', 'ambiguous')", (resource,)
            ).fetchone()
            if int_row:
                return False, f"unresolved_{int_row[1]}_intent", rec, epoch

            db.execute("DELETE FROM leases WHERE resource=?", (resource,))
            db.execute(
                "INSERT INTO leases VALUES (?, ?, ?, ?, ?, ?, ?, 'in_flight')",
                (resource, owner, epoch, pid, attempt_id, head_sha, time.time()),
            )
            return True, "acquired", rec, epoch

    def can_publish_cas(self, resource: str, owner: str, epoch: int, attempt_id: str, head_sha: str) -> bool:
        with self.transaction() as db:
            row = db.execute("SELECT owner, epoch, status FROM owners WHERE resource=?", (resource,)).fetchone()
            if not row or row[0] != owner or row[1] != epoch or row[2] != "active":
                return False
            lease_row = db.execute("SELECT attempt_id, head_sha, status FROM leases WHERE resource=?", (resource,)).fetchone()
            if not lease_row:
                return False
            l_attempt, l_sha, l_status = lease_row
            return l_attempt == attempt_id and l_sha == head_sha and l_status == "in_flight"

    def record_intent_pending(
        self, intent: str, resource: str, owner: str, epoch: int, head_sha: str, base_sha: str,
        payload: dict, attempt_id: str
    ) -> None:
        if not attempt_id:
            raise ValueError("attempt_id is required for record_intent_pending")
        now = time.time()
        payload_str = canonical(payload) if isinstance(payload, (dict, list)) else str(payload)
        with self.transaction() as db:
            existing = db.execute(
                "SELECT status, payload, resource, head_sha, base_sha FROM publication_intents WHERE intent=?",
                (intent,),
            ).fetchone()
            # Only an attempt proven absent by authenticated readback may be attempted
            # again, and only with the identical immutable payload and candidate.
            if existing and existing != ("absent", payload_str, resource, head_sha, base_sha):
                raise PermissionError(
                    f"intent '{intent}' already recorded with status '{existing[0]}'; re-posting prohibited"
                )
            owner_row = db.execute(
                "SELECT owner, epoch, status FROM owners WHERE resource=?", (resource,)
            ).fetchone()
            if owner_row is None:
                raise PermissionError(f"unmanaged resource '{resource}' cannot record intent")
            if owner_row[0] != owner or owner_row[1] != epoch or owner_row[2] != "active":
                raise PermissionError(
                    f"stale owner fence for '{resource}': expected ({owner}, {epoch}, 'active'), got {owner_row}"
                )

            lease_row = db.execute(
                "SELECT attempt_id, head_sha, status FROM leases WHERE resource=?", (resource,)
            ).fetchone()
            if lease_row is None:
                raise PermissionError(f"missing lease for '{resource}'")
            l_attempt, l_head, l_status = lease_row
            if l_status != "in_flight":
                raise PermissionError(f"lease for '{resource}' is not in_flight (status={l_status})")
            if l_head != head_sha:
                raise PermissionError(f"lease head mismatch for '{resource}': expected {head_sha}, got {l_head}")
            if l_attempt != attempt_id:
                raise PermissionError(f"lease attempt_id mismatch for '{resource}': expected {attempt_id}, got {l_attempt}")

            if existing:
                db.execute(
                    "UPDATE publication_intents SET owner=?,epoch=?,status='pending',error=NULL,"
                    "review_id=NULL,updated_at=?,attempt_id=? WHERE intent=? AND status='absent'",
                    (owner, epoch, now, attempt_id, intent),
                )
                return
            db.execute(
                "INSERT INTO publication_intents VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, NULL, NULL, ?, ?, ?)",
                (intent, resource, owner, epoch, head_sha, base_sha, payload_str, now, now, attempt_id),
            )

    def record_intent_outcome(
        self, intent: str, resource: str, status: str, error: str = None, review_id: int = None,
        attempt_id: str = None
    ) -> None:
        from .publication_store import record_outcome
        record_outcome(self, intent, resource, status, error, review_id, attempt_id)

    def release_lease(self, resource: str, attempt_id: str) -> None:
        with self.transaction() as db:
            lease_row = db.execute("SELECT attempt_id, status FROM leases WHERE resource=?", (resource,)).fetchone()
            if not lease_row or lease_row[0] != attempt_id or lease_row[1] in ("ambiguous", "completed"):
                return
            ambig = db.execute(
                "SELECT 1 FROM publication_intents WHERE resource=? AND status IN ('pending', 'ambiguous')", (resource,)
            ).fetchone()
            if ambig:
                db.execute("UPDATE leases SET status='ambiguous' WHERE resource=?", (resource,))
                return
            db.execute("DELETE FROM leases WHERE resource=? AND attempt_id=?", (resource, attempt_id))

    def recover_lease_operator(self, resource: str, authorized_by: str) -> bool:
        """Explicit operator recovery of a lease whose holder is gone.

        Refused while any pending or ambiguous intent exists (authenticated
        reconciliation settles those) and while an in-flight holder is still alive.
        """
        if not authorized_by:
            raise PermissionError("explicit operator authorization required for lease recovery")
        with self.transaction() as db:
            pending = db.execute(
                "SELECT intent FROM publication_intents WHERE resource=? AND status IN ('pending', 'ambiguous')",
                (resource,),
            ).fetchone()
            if pending:
                raise PermissionError(f"cannot recover lease for '{resource}': unresolved intent '{pending[0]}' exists")
            lease = db.execute(
                "SELECT holder_pid, status, acquired_at FROM leases WHERE resource=?", (resource,)
            ).fetchone()
            if lease and lease[1] in ("in_flight", "active") and lease_holder_alive(lease[0], lease[2]):
                raise PermissionError(f"cannot recover lease for '{resource}': holder process {lease[0]} is alive")
            db.execute("DELETE FROM leases WHERE resource=?", (resource,))
            return True

def record_advisory_approval(
    store,
    *,
    repo: str,
    pr: str,
    head: str,
    base: str,
    intent: str,
    authorized_by: str,
    policy: str,
    epoch: int | None = None,
    canonical_wire_hash: str,
    policy_snapshot: dict | None = None,
    publisher: str | None = None,
) -> dict:
    """Register explicit candidate-bound approval for an advisory publication intent.

    The approval slot is write-once, so this refuses every approval that
    ``publication_validation.verify_approval`` would refuse: the intent must be the
    canonical hash of the stored payload, and every binding, the canonical wire hash
    and any policy snapshot must match that payload. A bound snapshot is persisted
    with the approval as the evidence of the approved policy.
    """
    from .policy import compute_policy_digest
    from .publication_validation import require_approval_provenance, validate_payload

    if not isinstance(intent, str) or not re.fullmatch("[0-9a-f]{64}", intent):
        raise ValueError("intent must be a canonical SHA256")
    if not isinstance(canonical_wire_hash, str) or not re.fullmatch(
        "[0-9a-f]{64}", canonical_wire_hash
    ):
        raise ValueError("canonical wire hash required")
    require_approval_provenance(authorized_by)
    payload = store.get("advisory_payload", intent)
    if payload is None:
        raise PermissionError("approval requires the stored advisory payload for this intent")
    wire_hash, _wire = validate_payload(pr, intent, payload)
    if publisher is None:
        publisher = payload["publisher"]
    bindings = {"repo": repo, "pr": pr, "head": head, "base": base,
                "policy": policy, "publisher": publisher}
    if any(payload[key] != value for key, value in bindings.items()):
        raise PermissionError("approval bindings differ from the stored advisory payload")
    if canonical_wire_hash != wire_hash:
        raise PermissionError("approval canonical wire hash does not match the stored payload")
    if policy_snapshot is not None:
        enforcement = (policy_snapshot.get("enforcement_contents")
                       if isinstance(policy_snapshot, dict) else None)
        if (not isinstance(enforcement, dict)
                or policy_snapshot.get("repo") != repo
                or policy_snapshot.get("base_sha") != base
                or policy_snapshot.get("enforcement_digest") != policy
                or compute_policy_digest(enforcement) != policy):
            raise PermissionError("policy snapshot does not bind the approved policy digest")

    if epoch is None:
        ownership = store.ownership("pr:" + pr) if hasattr(store, "ownership") else None
        if ownership:
            epoch = ownership[1] if ownership[0] == "corral" else ownership[1] + 1
        else:
            epoch = 1
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch <= 0:
        raise ValueError("approval epoch must be a positive integer")

    approval = {
        "publisher": publisher,
        "repo": repo,
        "pr": pr,
        "head": head,
        "base": base,
        "policy": policy,
        "intent": intent,
        "epoch": epoch,
        "canonical_wire_hash": canonical_wire_hash,
        "authorized": True,
        "authorized_by": authorized_by,
    }
    if policy_snapshot is not None:
        approval["policy_snapshot"] = policy_snapshot
    store.put_once("advisory_approval", intent, approval)
    return approval

def export_cohort_control(
    store, output_path: Path, *, pr_filter: list = None
) -> dict:
    """Export active cohort ownership fences for consumption by legacy runners."""
    cohorts = {}
    with store.transaction() as db:
        rows = db.execute("SELECT resource, owner, epoch, status FROM owners").fetchall()

    for resource, owner, epoch, status in rows:
        if not resource.startswith("pr:"):
            continue
        pr_name = resource[3:]
        if pr_filter and pr_name not in pr_filter:
            continue
        cohorts[pr_name] = {
            "owner": owner,
            "epoch": epoch,
            "status": status,
        }

    export_data = {
        "version": 1,
        "cohorts": cohorts,
    }
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_suffix(".tmp")
    temp_path.write_text(json.dumps(export_data, indent=2), encoding="utf-8")
    temp_path.replace(output_path)
    return export_data
