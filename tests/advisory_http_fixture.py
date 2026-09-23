"""Complete HTTP fixtures exercising the real policy assembler and review transport."""

from copy import deepcopy
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

from corral.execution.advisory import compute_advisory_intent
from corral.execution.github_advisory import GitHubAdvisoryTransport
from corral.execution.github_support import compute_canonical_wire_hash
from corral.execution.policy import fetch_policy_snapshot
from corral.execution.store import Store, record_advisory_approval

REPO, PR = "test/repo", "test/repo#101"
HEAD, BASE = "a" * 40, "b" * 40
ACTOR = "review-publisher"
REQUIRED_SOURCE_PATHS = (
    ".github/workflows/review.yml", "scripts/review_runner.py",
    "scripts/review.py", "scripts/publish_review.py",
)
POLICY_INPUTS = {"required_sources": list(REQUIRED_SOURCE_PATHS),
                 "base_ref": "main",
                 "selected_workflow": REQUIRED_SOURCE_PATHS[0],
                 "runner": {"runs_on": ["self-hosted", "example-executor"]}}


class HTTP:
    def __init__(self):
        self.actor = ACTOR
        self.pull = {
            "number": 101,
            "state": "open",
            "head": {"sha": HEAD},
            "base": {"sha": BASE, "ref": "main"},
        }
        self.rules = [
            {
                "id": 1,
                "name": "main",
                "target": "branch",
                "enforcement": "active",
                "conditions": {
                    "ref_name": {"include": ["refs/heads/main"], "exclude": []}
                },
                "rules": [],
                "bypass_actors": [],
            }
        ]
        self.reviews, self.comments, self.posts = [], {}, []
        self.after_post = lambda review: review

    def __call__(self, method, path, *, headers=None, json=None):
        route, query = urlsplit(path).path, parse_qs(urlsplit(path).query)
        if method == "POST":
            self.posts.append(deepcopy(json))
            review = {
                "id": 901,
                "state": "COMMENTED",
                "user": {"login": self.actor},
                "commit_id": json["commit_id"],
                "body": json["body"],
            }
            self.reviews.append(deepcopy(review))
            self.comments[901] = deepcopy(json.get("comments", []))
            return self.after_post(review)
        if route == "/user":
            return {"login": self.actor}
        if route.endswith("/rulesets"):
            return [{"id": r["id"]} for r in self.rules]
        if "/rulesets/" in route:
            return deepcopy(
                next(r for r in self.rules if str(r["id"]) == route.split("/")[-1])
            )
        if route.endswith("/protection"):
            raise HTTPError(path, 404, "Not Found", {}, None)
        if route.endswith("/contents/.github/workflows"):
            return [
                {
                    "name": "review.yml",
                    "path": REQUIRED_SOURCE_PATHS[0],
                    "sha": "c" * 40,
                }
            ]
        if "/contents/" in route:
            source = route.split("/contents/")[1]
            assert source in REQUIRED_SOURCE_PATHS
            return {"path": source, "sha": "c" * 40, "size": 100}
        if route.endswith("/comments"):
            values = self.comments[int(route.split("/")[-2])]
        elif route.endswith("/reviews"):
            values = self.reviews
        else:
            return deepcopy(self.pull)
        page = int(query.get("page", ["1"])[0])
        return deepcopy(values[(page - 1) * 100 : page * 100])


def environment(tmp_path, *, body="advisory", comments=None, base_ref="main"):
    http = HTTP()
    http.pull["base"]["ref"] = base_ref
    inputs = {**POLICY_INPUTS, "base_ref": base_ref}
    store = Store(tmp_path / "authority.sqlite")
    store.acquire("pr:" + PR, "corral")
    policy = fetch_policy_snapshot(REPO, BASE, base_ref=base_ref, http_client=http,
                                   policy_inputs=inputs)["enforcement_digest"]
    intent, payload = compute_advisory_intent(
        REPO,
        PR,
        HEAD,
        BASE,
        policy,
        ACTOR,
        body,
        {"comments": comments} if comments is not None else {},
    )
    store.put_once("advisory_payload", intent, payload)
    record_advisory_approval(
        store,
        repo=REPO,
        pr=PR,
        head=HEAD,
        base=BASE,
        policy=policy,
        intent=intent,
        epoch=1,
        authorized_by="test operator",
        canonical_wire_hash=compute_canonical_wire_hash(HEAD, body, comments)[0],
    )
    transport = GitHubAdvisoryTransport(
        http_client=http, store=store, bridge_actor=ACTOR,
        authorized_bridge_actors=frozenset({ACTOR}), policy_inputs=inputs
    )
    return store, http, transport, intent, payload
