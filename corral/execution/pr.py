"""Offline PR lifecycle contracts; authenticated evidence and guarded fake merge."""
import hmac
import json

from .advisory import (
    authenticate_advisory_publisher,
    compute_advisory_intent,
    validate_candidate_and_pr,
)
from .profiles import resolve
from .publication_validation import verify_approval
from .store import canonical, digest


class FakeGitHub:
    """Explicit fake boundary: no GitHub network or provider calls.

    Limitation: Real GitHub transport adapter (e.g. POST /repos/{owner}/{repo}/pulls/{number}/reviews
    with event="COMMENT") is deliberately absent in offline sandbox. Zero network or provider calls.
    Does not merge, does not satisfy status checks, and does not alter live ownership.
    """
    def __init__(self):
        self.candidates = {}
        self.merged = {}
        self.advisories = {}  # Keyed by immutable stable intent ID: intent -> record
        self.advisory_calls = []

    def advisory(self, pr, expected, intent, payload, lose_ack=False):
        if self.candidates.get(pr) != expected:
            raise PermissionError("remote head/base candidate changed")
        record = {
            "pr": pr,
            "intent": intent,
            "candidate": expected,
            "payload": payload,
            "transport": "synthetic_comment",
            "advisory": True,
            "reconciled": False,
        }
        self.advisory_calls.append({"pr": pr, "intent": intent, "expected": expected, "lose_ack": lose_ack})
        if intent not in self.advisories:
            self.advisories[intent] = record
        if lose_ack:
            raise ConnectionError("simulated lost advisory acknowledgement")
        return dict(self.advisories[intent])

    def has_advisory(self, intent):
        return intent in self.advisories

    def merge(self, pr, expected, intent, lose_ack=False):
        if self.candidates[pr] != expected:
            raise PermissionError("remote head/base candidate changed")
        if pr in self.merged and self.merged[pr] != intent:
            raise PermissionError("already merged by different intent")
        self.merged[pr] = intent
        if lose_ack:
            raise ConnectionError("simulated lost merge acknowledgement")
        return {"pr": pr, "intent": intent, "candidate": expected, "merged": True}


class PRLifecycle:
    def __init__(self, store, github, *, publishers, owner_token, policy, profiles=()):
        self.store, self.github = store, github
        self.publishers, self.owner_token, self.policy = publishers, owner_token, policy
        self.profiles = profiles

    def owner(self, token):
        if not hmac.compare_digest(token, self.owner_token):
            raise PermissionError("owner merge/mutation authorization required")

    def publisher(self, token):
        actor = next((actor for actor, secret in self.publishers.items()
                      if hmac.compare_digest(token, secret)), None)
        if actor is None:
            raise PermissionError("unauthenticated review/check publisher")
        return actor

    def ingest_event(self, token, event):
        self.publisher(token)
        # Delivery IDs and scan aliases do not define logical PR identities.
        key = event["repo"] + "#" + str(event["number"])
        self.store.put_once("pr_event", event["delivery"], event)
        old = self.store.get("pr_history", key)
        if old is None:
            self.store.put_once("pr_history", key, {"task": event.get("task") or "adopted:" + key,
                "origin": event.get("origin", "external"), "unobserved_implementation_usage": None})
        return key

    def candidate(self, token, pr, candidate, *, owner, epoch):
        self.owner(token)
        value = {**candidate, "policy": self.policy["version"]}
        key = digest(value)
        with self.store.transaction() as db:
            row = db.execute("SELECT owner,epoch,status FROM owners WHERE resource=?", ("pr:" + pr,)).fetchone()
            if row != (owner, epoch, "active"):
                raise PermissionError("candidate writer fence is stale")
            db.execute("INSERT OR IGNORE INTO records VALUES('candidate',?,?)", (key, canonical(value)))
            db.execute("INSERT OR REPLACE INTO records VALUES('pr_candidate',?,?)", (pr, canonical(key)))
        self.github.candidates[pr] = key
        return key

    def binding(self, candidate, lens):
        current = self.store.get("candidate", candidate)
        rule = self.policy["lenses"][lens]
        value = {"policy": current["policy"], "spec": current["spec"],
                 "scope": {name: current["inputs"][name] for name in rule["scope"]}}
        if rule.get("base_bound", True):
            value["base"] = current["base"]
        if rule.get("head_bound", True):
            value["head"] = current["head"]
        return digest(value)

    def request_review(self, token, pr, lens, *, opinion=None, reason=None, selection=None):
        self.owner(token)
        if lens not in self.policy["lenses"]:
            raise PermissionError("unregistered review lens")
        if opinion and not reason:
            raise ValueError("intentional second opinion requires reason")
        candidate = self.store.get("pr_candidate", pr)
        binding = self.binding(candidate, lens)
        key = digest({"pr": pr, "lens": lens, "binding": binding, "opinion": opinion})
        old = self.store.get("review_request", key)
        if old:
            return key
        rule = self.policy["lenses"][lens]
        resolved = resolve(self.profiles, role=rule.get("role", "review"),
                           routes=self.policy["routes"], default=self.policy["default_profile"],
                           **(selection or {}))
        self.store.put_once("review_request", key, {"pr": pr, "lens": lens,
            "binding": binding, "candidate": candidate, "selection": resolved,
            "opinion": opinion, "reason": reason})
        return key

    def review(self, token, request, result):
        actor = self.publisher(token)
        req = self.store.get("review_request", request)
        if not req or actor not in self.policy["lenses"][req["lens"]]["actors"]:
            raise PermissionError("publisher not authorized for review lens")
        if result["candidate"] != req["candidate"] or result["binding"] != req["binding"]:
            raise PermissionError("review binding mismatch")
        if result["verdict"] not in ("approve", "request-changes"):
            raise ValueError("invalid structured verdict")
        self.store.put_once("review", request, {**result, "actor": actor,
                                                "usage": result.get("usage")})
        invocation = result.get("invocation")
        if invocation:
            self.store.put_once("pr_usage", invocation, {"pr": req["pr"], "usage": result.get("usage")})

    def check(self, token, pr, candidate, name, passed):
        actor = self.publisher(token)
        if actor not in self.policy["check_actors"]:
            raise PermissionError("publisher cannot attest CI")
        if not isinstance(passed, bool):
            raise ValueError("CI result must be a structured boolean")
        self.store.put_once("check", digest([pr, candidate, name]),
                            {"passed": bool(passed), "actor": actor})

    def mutation(self, token, pr, owner):
        self.owner(token)
        return self.store.acquire("pr:" + pr, owner)

    def assert_mutation(self, pr, owner, epoch):
        if self.store.ownership("pr:" + pr) != (owner, epoch, "active"):
            raise PermissionError("mutation fence is stale or draining")

    def merge(self, token, pr, owner, epoch, *, expected, lose_ack=False):
        self.owner(token)
        if self.store.ownership("pr:" + pr) != (owner, epoch, "active"):
            raise PermissionError("mutation fence is stale or draining")
        current = self.store.get("pr_candidate", pr)
        if current != expected or self.github.candidates.get(pr) != current:
            raise PermissionError("candidate freshness failure")
        if self.store.get("candidate", current).get("excluded"):
            raise PermissionError("repository risk exclusion requires separate authorization")
        requests, reviews = self.store.records("review_request"), self.store.records("review")
        for lens in self.policy["lenses"]:
            applicable = [reviews[key] for key, req in requests.items()
                          if req["pr"] == pr and req["lens"] == lens
                          and req["binding"] == self.binding(current, lens) and key in reviews]
            if not applicable or any(r["verdict"] != "approve" or r.get("blocking") for r in applicable):
                raise PermissionError("required applicable review missing or blocking")
        for name in self.policy["checks"]:
            result = self.store.get("check", digest([pr, current, name]))
            if not result or not result["passed"]:
                raise PermissionError("required exact-candidate CI missing")
        intent = digest({"pr": pr, "candidate": current, "owner": owner, "epoch": epoch})
        self.store.owned_operation("pr:" + pr, owner, epoch, "merge_intent", intent,
                                   {"pr": pr, "candidate": current})
        receipt = self.store.get("merge_receipt", intent)
        if receipt:
            return receipt
        # Unknown external outcome is reconciled before retrying the side effect.
        if self.github.merged.get(pr) == intent:
            receipt = {"pr": pr, "intent": intent, "candidate": current, "merged": True, "reconciled": True}
        else:
            receipt = self.github.merge(pr, current, intent, lose_ack)
        self.store.put_once("merge_receipt", intent, receipt)
        return receipt

    def advisory(self, token, pr, owner, epoch, *, expected, body=None, payload=None,
                 publisher_token=None, review_request=None, lose_ack=False):
        """Bind full PR advisory comment intent, verify provenance, and record synthetic transport receipt.

        Minimal local preparation binds FULL repo/PR/head/base/policy/publisher/payload.
        Synthetic COMMENT transport only; does not merge, satisfy checks, or alter ownership.
        Dedups duplicate delivery, refuses stale candidates, reconciles lost ACKs.
        """
        self.owner(token)
        repo, head, base = validate_candidate_and_pr(self.store, self.policy, pr, expected)
        if self.store.ownership("pr:" + pr) != (owner, epoch, "active"):
            raise PermissionError("mutation fence is stale or draining")
        current = self.store.get("pr_candidate", pr)
        if current != expected or self.github.candidates.get(pr) != current:
            raise PermissionError("candidate freshness failure")

        publisher, provenance, effective_body, effective_extra = authenticate_advisory_publisher(
            self, pr, current, body, payload,
            publisher_token=publisher_token, review_request=review_request
        )
        intent, full_payload = compute_advisory_intent(
            repo, pr, head, base, self.policy["version"], publisher, effective_body, effective_extra
        )

        if getattr(self.github, "transport_name", "synthetic_comment") != "synthetic_comment":
            # A real transport publishes only an intent whose stored approval binds this
            # exact payload, owner epoch, publisher and live policy; check it before any
            # intent is recorded so a refused approval leaves nothing unresolved.
            verify_approval(self.github, pr, intent, full_payload)

        self.store.owned_operation(
            "pr:" + pr, owner, epoch, "advisory_intent", intent,
            {"pr": pr, "candidate": current, "head": head, "base": base, "publisher": publisher}
        )
        self.store.put_once("advisory_payload", intent, full_payload)

        t_name = getattr(self.github, "transport_name", "synthetic_comment")
        if t_name != "synthetic_comment":
            return self.github.advisory(pr, current, intent, full_payload, lose_ack)
        receipt = self.store.get("advisory_receipt", intent)
        if receipt and (t_name == "synthetic_comment" or (
            receipt.get("transport") == t_name and receipt.get("advisory") is True
            and isinstance(receipt.get("review_id"), int) and not isinstance(receipt.get("review_id"), bool)
            and receipt.get("review_id") > 0 and receipt.get("intent") == intent and receipt.get("candidate") == current
        )):
            return receipt

        if self.github.has_advisory(intent):
            if t_name == "synthetic_comment":
                receipt = {"pr": pr, "intent": intent, "candidate": current, "repo": repo, "head": head, "base": base, "publisher": publisher, "transport": t_name, "advisory": True, "reconciled": True}
            else:
                stored = self.store.get("advisory_receipt", intent)
                receipt = dict(stored) if stored and stored.get("transport") == t_name and stored.get("review_id") else self.github.advisory(pr, current, intent, full_payload, lose_ack)
                receipt["reconciled"] = True
        else:
            receipt = self.github.advisory(pr, current, intent, full_payload, lose_ack)
            receipt.update({"repo": repo, "head": head, "base": base, "publisher": publisher})
            receipt.setdefault("transport", t_name)

        existing = self.store.get("advisory_receipt", intent)
        if existing and existing.get("transport") == "synthetic_comment" and t_name != "synthetic_comment":
            self.store.replace("advisory_receipt", intent, receipt)
        else:
            self.store.put_once("advisory_receipt", intent, receipt)
        return receipt

    def reconcile_advisory(self, token, pr, owner, epoch, *, intent,
                           candidate=None, body=None, publisher_token=None):
        """Explicit reconciliation path for an outstanding advisory publication intent.

        Allows reconciling stale candidate intents after lost ACK without submitting
        anything to transport, ensuring stale refusal does not make unknown outcomes
        irreconcilable. Reconciles using authenticated stored publisher/candidate/body.
        """
        self.owner(token)
        if self.store.ownership("pr:" + pr) != (owner, epoch, "active"):
            raise PermissionError("mutation fence is stale or draining")

        op = self.store.get("advisory_intent", intent)
        if not op or op.get("pr") != pr:
            raise ValueError("unknown or mismatched advisory intent for PR")

        stored_payload = self.store.get("advisory_payload", intent)
        if not stored_payload:
            raise ValueError("missing advisory payload for intent")

        stored_cand = op.get("candidate")
        if candidate is not None and candidate != stored_cand:
            raise ValueError("candidate mismatch during advisory reconciliation")
        if not self.store.get("candidate", stored_cand):
            raise ValueError(f"stored candidate record '{stored_cand}' not found")

        if body is not None and body != stored_payload.get("body"):
            raise ValueError("body mismatch during advisory reconciliation")

        stored_publisher = stored_payload.get("publisher")
        if publisher_token:
            actor = self.publisher(publisher_token)
            if actor != stored_publisher:
                raise PermissionError("publisher mismatch during advisory reconciliation")
        elif stored_publisher not in self.publishers:
            raise PermissionError("unauthenticated publisher in stored advisory intent")

        receipt = self.store.get("advisory_receipt", intent)
        if receipt and getattr(self.github, "transport_name", "synthetic_comment") == "synthetic_comment":
            return receipt

        current_cand = self.store.get("pr_candidate", pr)
        is_stale = bool(current_cand != stored_cand)

        if hasattr(self.github, "reconcile"):
            receipt = self.github.reconcile(pr, intent, stored_payload, is_stale=is_stale)
        else:
            delivered = bool(self.github.has_advisory(intent))
            receipt = {
                "pr": pr, "intent": intent, "candidate": stored_cand,
                "repo": stored_payload["repo"], "head": stored_payload["head"],
                "base": stored_payload["base"], "publisher": stored_publisher,
                "transport": getattr(self.github, "transport_name", "synthetic_comment"),
                "advisory": delivered, "reconciled": True, "stale_reconciliation": is_stale,
            }
            if not delivered:
                receipt["delivered"] = False

        if receipt.get("reconciled") and getattr(self.github, "transport_name", "synthetic_comment") == "synthetic_comment":
            self.store.put_once("advisory_receipt", intent, receipt)
        return receipt

    def repair(self, token, pr, cause, *, owner, epoch):
        self.owner(token)
        self.assert_mutation(pr, owner, epoch)
        if cause not in ("conflict", "spec-concern", "blocking-review"):
            raise ValueError("semantic repair cause required")
        selection = resolve(self.profiles, role="repair", routes=self.policy["routes"],
                            default=self.policy["default_profile"])
        key = digest([pr, self.store.get("pr_candidate", pr), cause])
        self.store.put_once("repair", key, {"pr": pr, "cause": cause, "selection": selection,
                                           "next": "registered writer must produce new candidate"})
        return key

    def transfer(self, token, pr, old_owner, epoch, new_owner, *, stopped):
        self.owner(token)
        with self.store.transaction() as db:
            ownership = db.execute("SELECT owner,epoch,status FROM owners WHERE resource=?", ("pr:" + pr,)).fetchone()
            if not ownership or ownership[:2] != (old_owner, epoch) or ownership[2] not in ("active", "draining"):
                raise PermissionError("stale transfer fence")
            for key, raw in db.execute("SELECT key,value FROM records WHERE kind='merge_intent'").fetchall():
                if json.loads(raw)["pr"] == pr and not db.execute(
                        "SELECT 1 FROM records WHERE kind='merge_receipt' AND key=?", (key,)).fetchone():
                    return {"state": "uncertain", "new_owner": None, "reason": "external merge intent unresolved"}
            for key, raw in db.execute("SELECT key,value FROM records WHERE kind='repair_execution'").fetchall():
                if json.loads(raw)["pr"] == pr and not db.execute(
                        "SELECT 1 FROM records WHERE kind='repair_complete' AND key=?", (key,)).fetchone():
                    return {"state": "uncertain", "new_owner": None, "reason": "repair writer unresolved"}
            for key, raw in db.execute("SELECT key,value FROM records WHERE kind='advisory_intent'").fetchall():
                # A delivered receipt or an authenticated proof of absence settles the intent.
                if json.loads(raw)["pr"] == pr and not db.execute(
                        "SELECT 1 FROM records WHERE kind='advisory_receipt' AND key=?", (key,)).fetchone() and not db.execute(
                        "SELECT 1 FROM publication_intents WHERE intent=? AND status='absent'", (key,)).fetchone():
                    return {"state": "uncertain", "new_owner": None, "reason": "advisory publication intent unresolved"}
            # Check common cohort leases and intents
            lease_row = db.execute("SELECT status FROM leases WHERE resource=?", ("pr:" + pr,)).fetchone()
            if lease_row and lease_row[0] in ("in_flight", "ambiguous", "active"):
                return {"state": "uncertain", "new_owner": None, "reason": f"lease_{lease_row[0]}"}
            int_row = db.execute(
                "SELECT intent, status FROM publication_intents WHERE resource=? AND status IN ('pending', 'ambiguous')", ("pr:" + pr,)
            ).fetchone()
            if int_row:
                return {"state": "uncertain", "new_owner": None, "reason": f"intent_{int_row[1]}"}
            if not stopped:
                db.execute("UPDATE owners SET status='draining' WHERE resource=?", ("pr:" + pr,))
                return {"state": "draining", "new_owner": None}
            db.execute("UPDATE owners SET owner=?,epoch=?,status='active' WHERE resource=?",
                       (new_owner, epoch + 1, "pr:" + pr))
            return {"state": "active", "owner": new_owner, "epoch": epoch + 1}

    def execute_repair(self, token, pr, cause, *, owner, epoch, controller, controller_token, spec):
        """Run qualified offline repair, then create a newly bound fake candidate."""
        key = self.repair(token, pr, cause, owner=owner, epoch=epoch)
        self.store.owned_operation("pr:" + pr, owner, epoch, "repair_execution", key, {"pr": pr})
        repair = self.store.get("repair", key)
        task_spec = {**spec, "role": "repair", "selection": repair["selection"], "pr": pr}
        task = controller.submit(controller_token, "pr-repair:" + key, task_spec)
        self.store.put_once("repair_task", key, {"task": task})
        result = controller.run(controller_token, task, execution_host=task_spec.get("host", controller.default_host))
        if not result["result"] or not result["result"]["accepted"]:
            if result["state"].get("status") == "completed":
                self.store.put_once("repair_complete", key, {"accepted": False})
            return {"repair": key, "task": task, "accepted": False}
        old = self.store.get("candidate", self.store.get("pr_candidate", pr))
        content = result["result"]["receipt"]["candidate"]
        new = self.candidate(token, pr, {**old, "head": "fake-content:" + content,
                                         "inputs": {**old["inputs"], "code": content}}, owner=owner, epoch=epoch)
        self.store.put_once("pr_usage", result["state"]["attempt"], {
            "pr": pr, "usage": result["result"]["usage"]})
        self.store.put_once("repair_complete", key, {"accepted": True, "candidate": new})
        return {"repair": key, "task": task, "accepted": True, "candidate": new}
