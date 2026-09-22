import base64
import hashlib
import json
import subprocess
import time
from pathlib import Path
from typing import Any

from . import continuation
from .scheduler import digest
from .workspace import apply_manifest, manifest, safe_path

HANDOFF_KIND = "artifact_handoff"
HANDOFF_INTENT_KIND = "handoff_intent"
BLOCKED_KIND = "task_blocked"

def execute_handoff(controller, token: str, spec: dict[str, Any]) -> dict[str, Any]:
    """Transfer and bind a verified artifact to a consumer workspace.

    1. Resolve immutable accepted producer receipt
    2. Locate artifact from immutable accepted candidate storage or verified workspace
    3. Check consumer initial state protection
    4. Acquire exclusive workspace lock for atomic versioned preparation transition
    5. Check idempotency: already successfully bound with same digest
    6. Apply and optionally commit
    """
    from .wave import handoff_key
    if hasattr(spec, "as_dict"):
        spec = spec.as_dict()
    elif not isinstance(spec, dict):
        spec = dict(spec)

    producer_task = spec["producer"]
    producer_path_rel = spec["producer_path"]
    consumer_task = spec["consumer"]
    consumer_path_rel = spec["consumer_path"]
    key = handoff_key(producer_task, producer_path_rel, consumer_task, consumer_path_rel)

    # 1. Resolve immutable accepted producer receipt
    producer_result = continuation.current_result(controller.store, producer_task)
    if not producer_result or not producer_result.get("accepted"):
        blocker = {
            "status": "blocked",
            "reason": f"producer task {producer_task} does not have an accepted result",
            "key": key, "producer": producer_task, "consumer": consumer_task,
        }
        controller.store.replace(BLOCKED_KIND, consumer_task, blocker)
        return blocker

    producer_gen = producer_result.get("generation", 1)
    producer_spec = controller.context(producer_task)

    if producer_path_rel not in producer_spec.get("candidate_paths", []):
        blocker = {
            "status": "blocked",
            "reason": f"artifact {producer_path_rel} is not a declared candidate path for producer {producer_task}",
            "key": key, "producer": producer_task, "consumer": consumer_task,
        }
        controller.store.replace(BLOCKED_KIND, consumer_task, blocker)
        return blocker

    # 2. Locate artifact from immutable accepted candidate storage or verified workspace
    art_dir = continuation.artifact_dir(controller.artifacts, producer_task, producer_gen)
    manifest_file = art_dir / "candidate_manifest.json"
    if not manifest_file.is_file():
        blocker = {
            "status": "blocked",
            "reason": f"producer candidate manifest missing: {manifest_file}",
            "key": key, "producer": producer_task, "consumer": consumer_task,
        }
        controller.store.replace(BLOCKED_KIND, consumer_task, blocker)
        return blocker

    try:
        cand_manifest = json.loads(manifest_file.read_text())
        from .store import digest as compute_digest
        recomputed = compute_digest({"base": cand_manifest.get("base"), "files": cand_manifest.get("files")})
        if recomputed != cand_manifest.get("digest"):
            raise ValueError("candidate_manifest.json internal digest mismatch")
        if recomputed != producer_result.get("receipt", {}).get("candidate_post"):
            raise ValueError("manifest digest does not match accepted receipt")
    except Exception as e:
        blocker = {
            "status": "blocked",
            "reason": f"producer candidate manifest is corrupt or unverified: {e}",
            "key": key, "producer": producer_task, "consumer": consumer_task,
        }
        controller.store.replace(BLOCKED_KIND, consumer_task, blocker)
        return blocker

    file_entry = cand_manifest.get("files", {}).get(producer_path_rel)
    if not file_entry:
        blocker = {
            "status": "blocked",
            "reason": f"producer artifact missing from accepted manifest: {producer_path_rel}",
            "key": key, "producer": producer_task, "consumer": consumer_task,
        }
        controller.store.replace(BLOCKED_KIND, consumer_task, blocker)
        return blocker

    verified_digest = file_entry["digest"]

    archived_file = art_dir / "accepted_candidates" / producer_path_rel
    workspace_file = safe_path(producer_spec["workspace"], producer_path_rel)

    if archived_file.is_file():
        data = archived_file.read_bytes()
        file_mode = archived_file.stat().st_mode & 0o777
    elif workspace_file.is_file():
        data = workspace_file.read_bytes()
        file_mode = workspace_file.stat().st_mode & 0o777
    else:
        blocker = {
            "status": "blocked",
            "reason": f"producer artifact missing: {producer_path_rel}",
            "key": key, "producer": producer_task, "consumer": consumer_task,
        }
        controller.store.replace(BLOCKED_KIND, consumer_task, blocker)
        return blocker

    artifact_digest = hashlib.sha256(data).hexdigest()
    if artifact_digest != verified_digest or (spec.get("expected_digest") and artifact_digest != spec["expected_digest"]):
        blocker = {
            "status": "blocked",
            "reason": f"artifact digest mismatch for {producer_path_rel}: expected {spec.get('expected_digest') or verified_digest}, got {artifact_digest}",
            "key": key, "producer": producer_task, "consumer": consumer_task,
            "actual_digest": artifact_digest,
        }
        controller.store.replace(BLOCKED_KIND, consumer_task, blocker)
        return blocker

    consumer_spec = controller.context(consumer_task)
    consumer_root = Path(consumer_spec["workspace"]).resolve()
    safe_path(consumer_root, consumer_path_rel)

    # Check idempotency: already successfully bound with same digest
    existing = controller.store.get(HANDOFF_KIND, key)
    if existing and existing.get("status") == "bound":
        if existing.get("artifact_digest") == artifact_digest:
            dest = safe_path(consumer_root, consumer_path_rel)
            if dest.is_file() and hashlib.sha256(dest.read_bytes()).hexdigest() == artifact_digest:
                return existing

    # 3. Check consumer initial state protection
    current_manifest = manifest(consumer_root, [consumer_path_rel])
    initial_manifest = spec.get("consumer_initial_manifest")
    if initial_manifest:
        allowed_bases = {initial_manifest["base"]}
        for existing in controller.store.records(HANDOFF_KIND).values():
            if existing.get("consumer") == consumer_task and existing.get("status") == "bound":
                if "consumer_base" in existing:
                    allowed_bases.add(existing["consumer_base"])

        file_intact = current_manifest.get("files", {}).get(consumer_path_rel) == initial_manifest.get("files", {}).get(consumer_path_rel)
        if current_manifest["base"] not in allowed_bases or not file_intact:
            blocker = {
                "status": "blocked",
                "reason": f"consumer destination {consumer_path_rel} changed since wave submission; refuse intervening overwrite",
                "key": key, "producer": producer_task, "consumer": consumer_task,
            }
            controller.store.replace(BLOCKED_KIND, consumer_task, blocker)
            return blocker

    # 4. Acquire exclusive workspace lock for atomic versioned preparation transition
    lock_resource = "workspace:" + str(consumer_root)
    owner = "wave-handoff:" + key
    epoch = controller.store.acquire(lock_resource, owner)

    try:
        # Check git clean staging state: no unrelated files staged
        if (consumer_root / ".git").exists():
            staged = subprocess.check_output(
                ["git", "diff", "--cached", "--name-only"], cwd=consumer_root, text=True
            ).strip().splitlines()
            if staged:
                raise PermissionError(f"unrelated staged changes in consumer workspace: {staged}")

        intent = {"epoch": epoch, "status": "transferring", "key": key, "digest": artifact_digest}
        controller.store.replace(HANDOFF_INTENT_KIND, key, intent)

        # Apply manifest safely
        file_entry = {"digest": artifact_digest, "data": base64.b64encode(data).decode(), "mode": file_mode}
        incoming_manifest = {"base": current_manifest["base"], "files": {consumer_path_rel: file_entry}}
        incoming_manifest["digest"] = digest({"base": incoming_manifest["base"], "files": incoming_manifest["files"]})
        apply_manifest(consumer_root, incoming_manifest, current_manifest)

        # Versioned preparation git commit
        consumer_base = current_manifest["base"]
        if spec.get("commit_binding", True) and (consumer_root / ".git").exists():
            subprocess.run(["git", "add", "--", consumer_path_rel], cwd=consumer_root, check=True, capture_output=True)
            staged = subprocess.check_output(["git", "diff", "--cached", "--name-only"], cwd=consumer_root, text=True).strip().splitlines()
            if set(staged) != {consumer_path_rel}:
                subprocess.run(["git", "reset", "HEAD"], cwd=consumer_root, capture_output=True)
                raise PermissionError("git staging contaminated during handoff")
            subprocess.run(
                ["git", "commit", "-m", f"Bind accepted {producer_path_rel} from {producer_task[:12]}"],
                cwd=consumer_root, check=True, capture_output=True,
            )
            consumer_base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=consumer_root, text=True).strip()

        record = {
            "status": "bound", "key": key, "producer": producer_task,
            "producer_generation": producer_gen, "producer_path": producer_path_rel,
            "artifact_digest": artifact_digest, "consumer": consumer_task,
            "consumer_path": consumer_path_rel, "consumer_base": consumer_base,
            "timestamp": time.time(),
        }
        controller.store.replace(HANDOFF_KIND, key, record)
        controller.store.replace(HANDOFF_INTENT_KIND, key, {"status": "completed", "record": record})
        controller.store.replace(BLOCKED_KIND, consumer_task, {"status": "unblocked", "task": consumer_task})
        controller.store.transition_owner(lock_resource, owner, epoch, "released")
        return record
    except BaseException:
        controller.store.transition_owner(lock_resource, owner, epoch, "uncertain")
        raise
