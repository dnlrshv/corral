"""Authenticated controller API and exact-candidate deterministic acceptance."""
import hmac
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path

from . import completion, containment, continuation, native, routes, verifier, workspace_contract
from .adapter import source_root
from .atomic_io import write_json
from .process import Process, boundary_for
from .profiles import Profile, STANDARD_NATIVE_PROFILES, resolve
from .store import Store, canonical, digest
from .usage import Spool
from .workspace import apply_manifest, manifest, safe_path


class Controller:
    def __init__(self, state, token, hosts, *, default_host, profiles=()):
        self.store = Store(Path(state) / "controller.sqlite")
        self.artifacts = Path(state) / "artifacts"
        self.artifacts.mkdir(parents=True, exist_ok=True)
        self.token, self.hosts, self.default_host = token, hosts, default_host
        registered = {p.id: (p if isinstance(p, Profile) else Profile(**p)) for p in STANDARD_NATIVE_PROFILES}
        for p in profiles:
            prof = p if isinstance(p, Profile) else Profile(**p)
            registered[prof.id] = prof
        self.profiles = list(registered.values())


    def authorize(self, token):
        if not hmac.compare_digest(token, self.token):
            raise PermissionError("authenticated client authority required")

    def submit(self, token, request_id, spec):
        self.authorize(token)
        spec = json.loads(json.dumps(spec))
        if spec.get("native_resume") or spec.get("resume") or spec.get("session_resume"):
            raise PermissionError("native session resume is unsupported; fresh checkpoint-based generations are supported")
        spec.setdefault("host", self.default_host)
        spec.setdefault("mode", "interactive")
        spec.setdefault("endpoint", "local")
        if spec["host"] not in self.hosts or spec["endpoint"] != "local":
            raise PermissionError("host/endpoint not authorized by this offline controller")
        host = self.hosts[spec["host"]]
        # Resolve profile server-side to ensure trusted digest match
        profile_id = spec.get("profile_id")
        model = spec.get("model")
        effort = spec.get("effort")
        if profile_id or model:
            requested_id = profile_id or f"{model}-{effort or 'medium'}"
            eligible = next((p for p in self.profiles if p.id == requested_id), None)
            if eligible is None:
                raise PermissionError("profile is not registered by controller")
            selected = resolve(self.profiles, role=spec.get("role", "implementation"),
                               routes=host.get("routes", []), tools=spec.get("tools", []),
                               context=spec.get("context", 0), model=eligible.model,
                               effort=eligible.effort, default=eligible.id)
            if eligible.harness not in host.get("harnesses", []):
                raise PermissionError("harness unsupported on executor")
            if eligible.harness != "synthetic":
                declared = routes.declared_routes(host).get(eligible.route)
                if declared is None:
                    raise PermissionError(
                        f"profile {eligible.id} route {eligible.route!r} is not declared by this host")
                routes.authorize(declared, eligible, host_routes=tuple(host.get("routes", [])))
                if spec.get("command"):
                    raise PermissionError(
                        "native profile must run through the trusted adapter; explicit command refused")
            selected["original_request"] = {"id": requested_id}
            selected["source_reason"] = spec.get("source_reason")
            selected["source_confidence"] = spec.get("source_confidence")
            spec["selection"] = selected
        elif spec.get("selection", {}).get("profile"):
            # Backwards compatibility for old precise format
            requested = spec["selection"]["profile"]
            eligible = next((p for p in self.profiles if p.id == requested["id"]), None)
            if eligible is None:
                raise PermissionError("profile is not registered by controller")
            selected = resolve(self.profiles, role=spec.get("role", "implementation"),
                               routes=host.get("routes", []), tools=spec.get("tools", []),
                               context=spec.get("context", 0), model=eligible.model,
                               effort=eligible.effort, default=eligible.id)
            if digest(requested) != digest(selected["profile"]):
                raise PermissionError("profile declaration does not match trusted registry")
            if eligible.harness not in host.get("harnesses", []):
                raise PermissionError("harness unsupported on executor")
            if eligible.harness != "synthetic":
                declared = routes.declared_routes(host).get(eligible.route)
                if declared is None:
                    raise PermissionError(
                        f"profile {eligible.id} route {eligible.route!r} is not declared by this host")
                routes.authorize(declared, eligible, host_routes=tuple(host.get("routes", [])))
                if spec.get("command"):
                    raise PermissionError(
                        "native profile must run through the trusted adapter; explicit command refused")
            selected["original_request"] = spec["selection"].get("requested")
            selected["source_reason"] = spec["selection"].get("reason")
            selected["source_confidence"] = spec["selection"].get("confidence")
            spec["selection"] = selected
        else:
            spec["selection"] = resolve([], role="deterministic", routes=[], deterministic=True)
        spec.setdefault("verifier_paths", [])
        candidate_set = set(spec.get("candidate_paths", []))
        verifier_set = set(spec.get("verifier_paths", []))
        if candidate_set & verifier_set:
            raise PermissionError("candidate_paths must not overlap with verifier_paths")
        for name in spec["candidate_paths"] + spec["verifier_paths"] + [
            spec.get("result_file", "result.json"),
            spec.get("usage_file", "usage.json"),
        ]:
            safe_path(spec["workspace"], name)
        if spec.get("role", "implementation") not in ("implementation", "review", "repair", "adjudication", "deterministic"):
            raise ValueError("unknown role")
        task_id = digest({"request": request_id, "repo": spec["repo"]})
        self.store.put_once("request", task_id, spec)
        with self.store.transaction() as db:
            db.execute("INSERT OR IGNORE INTO records VALUES('initial',?,?)",
                       (task_id, canonical({"status": "submitted", "submitted": time.time()})))
        return task_id

    def steer(self, token, task_id, amendment_id, amendment):
        self.authorize(token)
        if not self.store.get("request", task_id):
            raise KeyError(task_id)
        if set(amendment) - {"objective", "mode", "pause_dispatch", "stop_monitoring"}:
            raise PermissionError("steering cannot grant authority or replace execution")
        if amendment.get("mode", "interactive") not in ("interactive", "wave"):
            raise ValueError("invalid mode")
        self.store.append_ordered("amendment", task_id + ":" + amendment_id, amendment)

    def continue_task(self, token, task_id, continuation_id, amendment):
        """Checkpoint a terminal attempt and schedule exactly one later generation."""
        return continuation.schedule(self, token, task_id, continuation_id, amendment)

    def context(self, task_id):
        result = dict(self.store.get("request", task_id))
        amendments = [v for k, v in self.store.records("amendment").items() if k.startswith(task_id + ":")]
        for item in sorted(amendments, key=lambda value: value["sequence"]):
            result.update(item["value"])
        return result

    def status(self, token, task_id):
        self.authorize(token)
        return {"task": task_id, "spec": self.store.get("request", task_id),
                "state": self.store.get("state", task_id) or self.store.get("initial", task_id),
                "amendments": {k: v for k, v in self.store.records("amendment").items()
                               if k.startswith(task_id + ":")},
                # Current result plus the immutable per-generation history and lineage, so a
                # continued task is auditable through the client instead of by reading files.
                "result": continuation.current_result(self.store, task_id),
                "results": {str(k): v for k, v in
                            continuation.results(self.store, task_id).items()},
                "lineage": continuation.lineage(self, task_id)}

    def cancel(self, token, task_id):
        self.authorize(token)
        self.store.put_once("cancel", task_id, {"requested": True})
        return {"status": "cancel-requested", "ownership_released": False}

    def snapshot(self, token, task_id, paths):
        self.authorize(token)
        spec = self.store.get("request", task_id)
        if spec is None:
            raise KeyError(task_id)
        if self.hosts[spec["host"]].get("executor"):
            from .remote import workspace_call

            return workspace_call(self, task_id, "snapshot", paths=paths)
        provenance = workspace_contract.preflight(spec, spec["workspace"])
        return manifest(spec["workspace"], paths, provenance)

    def fetch_artifact(self, token, task_id, path, generation=None):
        self.authorize(token)
        spec = self.store.get("request", task_id)
        if spec is None:
            raise KeyError(task_id)

        if self.hosts[spec["host"]].get("executor"):
            from .remote import workspace_call
            return workspace_call(self, task_id, "fetch-artifact", path=path, generation=generation)

        found_results = continuation.results(self.store, task_id)
        if generation is None:
            result = found_results[max(found_results)] if found_results else None
            if not result:
                raise ValueError("No result for task")
            generation = result.get("generation", 1)
        else:
            result = found_results.get(generation)
            if not result:
                raise ValueError(f"No result for task {task_id} generation {generation}")

        if not result.get("accepted"):
            raise ValueError(f"Result for generation {generation} was not accepted")

        gen_spec = continuation.effective_spec(self, task_id, generation)

        if path not in gen_spec.get("candidate_paths", []):
            raise ValueError(f"artifact {path} is not in candidate_paths for generation {generation}")

        art_dir = continuation.artifact_dir(self.artifacts, task_id, generation)
        manifest_file = art_dir / "candidate_manifest.json"
        if not manifest_file.is_file():
            raise FileNotFoundError("candidate_manifest.json missing")

        cand_manifest = json.loads(manifest_file.read_text())

        from .store import digest as compute_digest
        recomputed = compute_digest({"base": cand_manifest["base"], "files": cand_manifest["files"]})
        if recomputed != cand_manifest.get("digest"):
            raise ValueError("candidate_manifest.json internal digest mismatch")
        if recomputed != result.get("receipt", {}).get("candidate_post"):
            raise ValueError("candidate_manifest.json digest does not match accepted receipt")

        file_entry = cand_manifest.get("files", {}).get(path)
        if not file_entry:
            raise FileNotFoundError("artifact missing from manifest files")

        archived_file = art_dir / "accepted_candidates" / path
        if not archived_file.is_file():
            raise FileNotFoundError("artifact missing from archive")

        data = archived_file.read_bytes()
        import hashlib
        actual_digest = hashlib.sha256(data).hexdigest()
        if actual_digest != file_entry["digest"]:
            raise ValueError("artifact data corruption in archive")

        import base64
        return {
            "data": base64.b64encode(data).decode(),
            "mode": file_entry.get("mode"),
            "digest": actual_digest,
            "manifest_digest": cand_manifest.get("digest")
        }

    def transfer(self, token, task_id, transfer_id, incoming, expected):
        self.authorize(token)
        spec = self.store.get("request", task_id)
        if spec is None:
            raise KeyError(task_id)
        if self.hosts[spec["host"]].get("executor"):
            from .remote import workspace_call

            return workspace_call(self, task_id, "transfer", transfer_id=transfer_id,
                                  incoming=incoming, expected=expected)
        if spec.get("workspace_kind", "checkout") != "checkout":
            raise PermissionError("transfer requires a real checkout workspace")
        key = task_id + ":" + transfer_id
        request = {"incoming": incoming["digest"], "expected": expected["digest"]}
        self.store.put_once("transfer_request", key, request)
        saved = self.store.get("transfer_receipt", key)
        if saved:
            return saved
        workspace = str(Path(spec["workspace"]).resolve())
        owner = "transfer:" + key
        epoch = self.store.acquire("workspace:" + workspace, owner)
        if self.store.get("transfer_started", key):
            raise PermissionError("interrupted transfer requires reconciliation")
        self.store.put_once("transfer_started", key, {"epoch": epoch})
        try:
            result = apply_manifest(workspace, incoming, expected)
        except BaseException:
            self.store.transition_owner("workspace:" + workspace, owner, epoch, "uncertain")
            raise
        self.store.put_once("transfer_receipt", key, result)
        self.store.transition_owner("workspace:" + workspace, owner, epoch, "released")
        return result

    def run(self, token, task_id, *, execution_host):
        self.authorize(token)
        request = self.store.get("request", task_id)
        if request is None:
            raise KeyError(task_id)
        if request["host"] != execution_host:
            raise PermissionError("wrong execution host; no fallback")
        if self.hosts[execution_host].get("executor"):
            from .remote import run_remote

            return run_remote(self, token, task_id, self.hosts[execution_host])
        existing = self.store.get("state", task_id)
        # Any dispatched-but-unfinished work must be reconciled, never relaunched. The only
        # authorized reason to dispatch a task that already has state is the one explicitly
        # scheduled continuation generation of the same aggregate task identity.
        generation = continuation.pending_generation(self.store, task_id) or 1
        if existing and generation == 1:
            return self.status(token, task_id)
        spec = continuation.effective_spec(self, task_id, generation)
        if spec.get("native_resume") or spec.get("resume") or spec.get("session_resume"):
            raise PermissionError("native session resume is unsupported; fresh checkpoint-based generations are supported")
        dispatch_objective = spec.get("objective")
        for dependency in spec.get("dependencies", []):
            result = continuation.current_result(self.store, dependency)
            if not result or not result["accepted"]:
                raise PermissionError("dependency is not accepted")
            if continuation.pending_generation(self.store, dependency) is not None:
                raise PermissionError("dependency has a pending continuation")
        if self.store.get("cancel", task_id):
            return {**self.status(token, task_id), "dispatch": "cancelled-before-start"}
        if self.context(task_id).get("pause_dispatch"):
            raise PermissionError("dispatch paused")
        workspace = str(Path(spec["workspace"]).resolve())
        workspace_provenance = workspace_contract.preflight(spec, workspace)
        resource = "workspace:" + workspace
        epoch = self.store.acquire(resource, task_id)
        capacity = self.hosts[execution_host]
        if not self.store.allocate(task_id, execution_host, spec.get("cpu", 1),
                                   spec.get("memory_mb", 0), {
                                       "cpu": capacity.get("cpu", os.cpu_count() or 1),
                                       "memory_mb": capacity.get("memory_mb", 0)}):
            return self.status(token, task_id)
        attempt = str(uuid.uuid4())
        state = {"status": "dispatching", "attempt": attempt, "epoch": epoch,
                 "host": execution_host, "profile": spec.get("selection"),
                 "process_status": None, "endpoint": "local", "generation": generation,
                 "workspace_provenance": workspace_provenance}
        # Serialized claim prevents concurrent identical clients starting two workers, once per
        # generation: a repeated dispatch of the same generation finds the claim and returns.
        try:
            self.store.put_once("claim", continuation.claim_key(task_id, generation),
                                {"attempt": attempt, "generation": generation})
        except ValueError:
            return self.status(token, task_id)
        self.store.replace("state", task_id, state)
        self.store.put_once("invocation", attempt, {"task": task_id, "generation": generation,
            "role": spec.get("role", "implementation"), "selection": spec["selection"],
            "observed": None,
            "usage": "unknown-until-native-events"})
        # A continuation generation gets its own artifact directory and native scratch, so no
        # stale file from the accepted prior attempt can masquerade as this invocation's result.
        output = continuation.artifact_dir(self.artifacts, task_id, generation)
        output.mkdir(parents=True, exist_ok=True)
        spool = Spool(output / "usage-spool.sqlite")
        telemetry_errors = []
        context_path = output / "context.json"
        usage_path = output / "native-usage.json"
        checkpoint = continuation.checkpoint_for(self.store, task_id, generation)
        if checkpoint:
            (output / "continuation-checkpoint.json").write_text(
                json.dumps(checkpoint, indent=2, sort_keys=True))
        def update_context_and_usage():
            amendments = [{"amendment_id": key.split(":", 1)[1], "sequence": value["sequence"],
                           "value": value["value"]}
                          for key, value in sorted(self.store.records("amendment").items())
                          if key.startswith(task_id + ":")]
            payload = {**self.context(task_id), "task": task_id, "generation": generation,
                       "attempt": attempt, "amendment_history": amendments,
                       "workspace_provenance": workspace_provenance}
            if checkpoint:
                payload["prior_generation"] = checkpoint
                payload["prompt_extras"] = "\n\n".join(
                    part for part in (spec.get("prompt_extras"),
                                      continuation.checkpoint_text(checkpoint)) if part)
            write_json(context_path, payload)
            usage_file = usage_path
            if usage_file.is_file():
                try:
                    events = json.loads(usage_file.read_text())
                    if not isinstance(events, list):
                        raise ValueError("native usage must be an event list")
                    for event in events:
                        spool.append({**event, "invocation": attempt, "task": task_id})
                except (ValueError, TypeError, KeyError, OSError, sqlite3.Error) as error:
                    telemetry_errors.append(type(error).__name__)
        host = self.hosts[execution_host]
        verifier_roots = tuple(str(item) for item in (host.get("verifier_roots") or ()))
        protected_paths = [str(self.store.path.parent.resolve()), str(self.artifacts.resolve()),
                           *verifier_roots, *(host.get("protected_paths") or [])]
        # A refusal before any worker process exists must not strand ownership as
        # active: nothing ran, so the workspace is released and the refusal recorded.
        launched = False
        try:
            policy = verifier.policy(spec, workspace, host=host, worker_writable=(workspace,))
            verifier_paths = list(policy.verifier_paths)
            pre_verifier_manifest = verifier.bound_bundle(policy, workspace) or None
            # Persisted so reconciliation can prove integrity after a controller restart.
            state["verifier_bundle"] = pre_verifier_manifest

            selection = spec.get("selection") or {}
            declared_profile = selection.get("profile") or {}
            harness = declared_profile.get("harness")
            native_run = bool(harness) and harness != "synthetic"
            run_env = {"CORRAL_CONTEXT_PATH": str(context_path), "CORRAL_USAGE_PATH": str(usage_path)}
            run_cwd, seatbelt, label, native_evidence = workspace, None, None, None
            if native_run:
                profile = next((item for item in self.profiles if item.id == declared_profile.get("id")), None)
                if profile is None:
                    raise PermissionError("native selection is not a controller-registered profile")
                prepared = native.prepare(spec=spec, host=host, profile=profile, task_dir=output,
                                          workspace=workspace, state_dir=self.store.path.parent,
                                          artifacts=self.artifacts, source_root=source_root(),
                                          task_id=continuation.scratch_id(task_id, generation),
                                          verifier_roots=verifier_roots,
                                          usage_path=usage_path, context_path=context_path)
                command, run_cwd, native_evidence = prepared.command, prepared.run_cwd, prepared.evidence
                run_env.update(prepared.env)
                # The adapter is trusted controller-side code: it runs outside the worker boundary
                # and places the harness inside it. Sandboxing the adapter instead would give the
                # worker the task directory and leave the harness unconstrained.
                label = "trusted-adapter-outside-worker-boundary"
            else:
                command = spec.get("command")
                if not command:
                    raise PermissionError("deterministic dispatch requires an explicit command")
                if spec.get("use_sandbox"):
                    boundary = boundary_for(workspace, protected_paths, verifier_paths, task_dir=output)
                    seatbelt = containment.build_profile(boundary)

            update_context_and_usage()
            with (output / "stdout").open("wb") as out, (output / "stderr").open("wb") as err:
                child = Process(command, run_cwd, out, err, env=run_env,
                                seatbelt_profile=seatbelt, containment_label=label)
                launched = True
                # The label the process object actually applied, never an inferred claim.
                state.update(status="running", pid=child.child.pid, pgid=child.pgid,
                             worker_identity=child.worker_identity, containment=child.containment)
                self.store.replace("state", task_id, state)
                while child.child.poll() is None:
                    update_context_and_usage()
                    if self.store.get("cancel", task_id):
                        state["cancellation"] = child.cancel()
                        break
                    time.sleep(0.05)
                code = child.child.poll()
                group_alive = child.running_group()
            # Structured output is preserved independently of prose or process exit.
            structured, adapter_result = completion.structured(output, workspace, spec, native_run)
            if adapter_result is None and native_run:
                telemetry_errors.append("AdapterResultMissing")
            (output / "structured.json").write_text(json.dumps(structured, indent=2))
            update_context_and_usage()
            usage = completion.publish_usage(spool, self.store, output=output, spec=spec,
                                             attempt=attempt, telemetry_errors=telemetry_errors)

            if group_alive:
                raise RuntimeError("parent exited with live descendants; ownership uncertain")
            record = verifier.execute(policy, workspace, candidate_paths=spec["candidate_paths"],
                                      task=task_id, attempt=attempt,
                                      pre_verifier_manifest=pre_verifier_manifest,
                                      workspace_provenance=workspace_provenance)
            receipt = record.payload
            receipt["native"] = native_evidence
            if native_evidence:
                # Passed through verbatim: the receipt must carry the probe's honest scope
                # limits (not_contained / isolation_claim), not a narrower restatement.
                receipt["containment"] = native_evidence["containment"]
            (output / "verify.stdout").write_bytes(record.stdout)
            (output / "verify.stderr").write_bytes(record.stderr)
            (output / "receipt.json").write_text(json.dumps(receipt, indent=2))
            observed = (adapter_result or {}).get("identity_observed") or None
            if observed:
                # Observed identity comes only from harness-reported fields, never from success.
                self.store.replace("invocation", attempt,
                                   {**self.store.get("invocation", attempt), "observed": observed})
            requested_identity = (adapter_result or {}).get("identity_requested") or {}
            identity_mismatch = sorted(
                key for key in ("model", "effort", "route", "provider", "account_ref")
                if native_run and observed and observed.get(key) is not None
                and str(observed[key]) != str(requested_identity.get(key)))
            accepted = (code == 0 and receipt["exit_code"] == 0 and receipt["unchanged"]
                        and receipt["verifier_intact"] is not False and receipt["policy_ok"]
                        and not identity_mismatch
                        and not self.store.get("cancel", task_id))
            if native_run:
                accepted = bool(accepted and adapter_result
                                and adapter_result.get("status") == "completed")
            current_obj = self.context(task_id).get("objective")
            amendment_pending = bool(current_obj != dispatch_objective)

            if accepted:
                cand_manifest = manifest(workspace, spec.get("candidate_paths", []), workspace_provenance)
                if cand_manifest["digest"] != receipt.get("candidate_post"):
                    # Worker mutated files after verification!
                    accepted = False
                else:
                    cand_dir = output / "accepted_candidates"
                    cand_dir.mkdir(parents=True, exist_ok=True)
                    (output / "candidate_manifest.json").write_text(json.dumps(cand_manifest, indent=2))
                    for c_path in spec.get("candidate_paths", []):
                        src_file = safe_path(workspace, c_path)
                        if src_file.is_file():
                            dst_file = safe_path(cand_dir, c_path)
                            dst_file.parent.mkdir(parents=True, exist_ok=True)
                            dst_file.write_bytes(src_file.read_bytes())

            result = {"structured": structured, "accepted": accepted, "receipt": receipt,
                      "identity_mismatch": identity_mismatch,
                      "artifact_directory": str(output), "endpoint": "local", "usage": usage,
                      "selection": spec["selection"], "observed": observed or "unknown",
                      "generation": generation,
                      "native": native_evidence,
                      "adapter_errors": (adapter_result or {}).get("errors") or [],
                      "adapter_warnings": (adapter_result or {}).get("warnings") or [],
                      "amendment_pending": amendment_pending,
                      "dispatch_objective": dispatch_objective,
                      "current_objective": current_obj}
            # The current attempt is recorded exactly as it executed against its old objective.
            continuation.record_result(self.store, task_id, generation, result)

            state.update(status="completed", process_status=code,
                         amended_objective_pending=amendment_pending)
            self.store.replace("state", task_id, state)

            self.store.transition_owner(resource, task_id, epoch,
                                        "uncertain" if self.store.get("cancel", task_id) else "released")

            # If it succeeded but an amendment is pending, automatically schedule the next generation.
            if accepted and amendment_pending and not self.store.get("cancel", task_id):
                auto_id = f"auto-amend-g{generation+1}-{uuid.uuid4().hex[:8]}"
                # The amendment dict only needs to carry the new objective; bindings are inherited
                self.continue_task(token, task_id, auto_id, {"objective": current_obj})
            if not self.store.get("cancel", task_id):
                self.store.release_allocation(task_id)
        except BaseException as error:
            # Post-launch failures keep ownership uncertain: a worker may have touched the
            # workspace and its descendants may be unknown. A pre-launch refusal proved no
            # process was created, so the workspace is truthfully released for reconciliation.
            state.update(status="uncertain" if launched else "refused-before-launch",
                         error=type(error).__name__)
            self.store.replace("state", task_id, state)
            self.store.transition_owner(resource, task_id, epoch,
                                        "uncertain" if launched else "released")
            if not launched:
                self.store.release_allocation(task_id)
            raise
        return self.status(token, task_id)
