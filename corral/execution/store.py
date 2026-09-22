"""Single-controller transactional state, request deduplication and fencing."""
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import uuid
from contextlib import contextmanager



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

    def allocate(self, task, host, cpu, memory_mb, capacity):
        with self.transaction() as db:
            rows = db.execute("SELECT key,value FROM records WHERE kind='allocation'").fetchall()
            allocations = {k: json.loads(v) for k, v in rows}
            if task in allocations and allocations[task]["active"]:
                return False
            active = [v for v in allocations.values() if v["active"] and v["host"] == host]
            if cpu <= 0 or memory_mb < 0:
                raise ValueError("invalid resource request")
            if sum(v["cpu"] for v in active) + cpu > capacity["cpu"] or sum(v["memory_mb"] for v in active) + memory_mb > capacity["memory_mb"]:
                raise PermissionError("registered host capacity unavailable")
            db.execute("INSERT OR REPLACE INTO records VALUES('allocation',?,?)", (task, canonical({
                "host": host, "cpu": cpu, "memory_mb": memory_mb, "active": True})))
            return True

    def release_allocation(self, task):
        value = self.get("allocation", task)
        if value:
            self.replace("allocation", task, {**value, "active": False})

    def settle_cancelled(self, *, resource, owner, epoch, task, generation, result, audit, expected_state, state):
        from .recovery_store import settle_cancelled

        return settle_cancelled(self, resource=resource, owner=owner, epoch=epoch, task=task,
                                generation=generation, result=result, audit=audit,
                                expected_state=expected_state, state=state)

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

    def acquire(self, resource, owner):
        with self.transaction() as db:
            row = db.execute("SELECT owner,epoch,status FROM owners WHERE resource=?",
                             (resource,)).fetchone()
            if row and row[2] != "released":
                if row[0] == owner and row[2] == "active":
                    return row[1]
                raise PermissionError("resource still owned or outcome uncertain")
            epoch = row[1] + 1 if row else 1
            db.execute("INSERT OR REPLACE INTO owners VALUES(?,?,?,?)",
                       (resource, owner, epoch, "active"))
            return epoch

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
        with self.transaction() as db:
            existing = db.execute(
                "SELECT status, payload FROM publication_intents WHERE intent=?", (intent,)
            ).fetchone()
            if existing:
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

            payload_str = canonical(payload) if isinstance(payload, (dict, list)) else str(payload)
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
        """Explicit safe operator recovery ONLY if no pending or ambiguous intent exists."""
        if not authorized_by:
            raise PermissionError("explicit operator authorization required for lease recovery")
        with self.transaction() as db:
            pending = db.execute(
                "SELECT intent FROM publication_intents WHERE resource=? AND status IN ('pending', 'ambiguous')",
                (resource,),
            ).fetchone()
            if pending:
                raise PermissionError(f"cannot recover lease for '{resource}': unresolved intent '{pending[0]}' exists")
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
    policy: str = "v1",
    epoch: int | None = None,
    canonical_wire_hash: str | None = None,
    policy_snapshot: dict | None = None,
    publisher: str | None = None,
) -> dict:
    """Register explicit candidate-bound approval for an advisory publication intent."""
    if not pr or "#" not in pr:
        raise ValueError("Explicit repository PR identity required, format: <repo>#<number>")
    if not head or len(head) != 40:
        raise ValueError("Valid 40-character commit head SHA required")
    if not base or len(base) != 40:
        raise ValueError("Valid 40-character commit base SHA required")
    if not intent:
        raise ValueError("Stable publication intent ID required")

    if epoch is None:
        ownership = store.ownership("pr:" + pr) if hasattr(store, "ownership") else None
        if ownership:
            epoch = ownership[1] if ownership[0] == "corral" else ownership[1] + 1
        else:
            epoch = 1

    stored_payload = store.get("advisory_payload", intent) or {}
    approval = {
        "publisher": publisher or stored_payload.get("publisher"),
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
