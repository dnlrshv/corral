"""Executable fake GitHub lifecycle with a real process-backed semantic repair."""
import argparse
import json
import platform
import subprocess
import sys
from pathlib import Path

from .controller import Controller
from .demo import fixture_profiles
from .legacy import import_review
from .pr import FakeGitHub, PRLifecycle
from .store import Store


def run(directory):
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    repo = directory / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=f@example.invalid",
                    "commit", "--allow-empty", "-qm", "fixture"], cwd=repo, check=True)
    profiles = fixture_profiles()
    policy = {"version": "fixture-policy-v1", "routes": ["fixture"], "default_profile": "strong-low",
              "checks": ["lint", "test"], "check_actors": ["ci"], "lenses": {
                  "code": {"scope": ["code"], "actors": ["code-reviewer"]},
                  "spec": {"scope": ["spec"], "actors": ["spec-reviewer"], "head_bound": False}}}
    store = Store(directory / "pr.sqlite")
    github = FakeGitHub()
    lifecycle = PRLifecycle(store, github, publishers={"ci": "ci", "code-reviewer": "code",
        "spec-reviewer": "spec"}, owner_token="owner", policy=policy, profiles=profiles)
    pr = lifecycle.ingest_event("ci", {"repo": "fake/repo", "number": 1,
        "delivery": "event1", "origin": "manual"})
    epoch = lifecycle.mutation("owner", pr, "legacy")
    initial = lifecycle.candidate("owner", pr, {"head": "fake-h1", "base": "fake-b1", "spec": "s1",
        "inputs": {"code": "broken", "spec": "s1"}}, owner="legacy", epoch=epoch)
    controller = Controller(directory / "controller", "controller", {"fixture": {
        "routes": ["fixture"], "harnesses": ["synthetic"]}}, default_host="fixture", profiles=profiles)
    repair = lifecycle.execute_repair("owner", pr, "conflict", owner="legacy", epoch=epoch,
        controller=controller, controller_token="controller", spec={"repo": "fake/repo", "workspace": str(repo),
        "candidate_paths": ["fixed.py", "result.json"], "command": [sys.executable, "-c",
        "open('fixed.py','w').write('answer = 42\\n'); open('result.json','w').write('{\"fixed\":true}')"],
        "verify": [sys.executable, "-c", "from fixed import answer; assert answer == 42"]})
    assert repair["accepted"] and repair["candidate"] != initial
    current = repair["candidate"]
    reviews = []
    for lens, token, actor in (("code", "code", "code-reviewer"), ("spec", "spec", "spec-reviewer")):
        key = lifecycle.request_review("owner", pr, lens)
        candidate = store.get("candidate", current)
        envelope = {"workflow_run": {"id": lens + "-run", "path": "fixture-review.yml",
            "conclusion": "success", "head_sha": candidate["head"], "base_sha": candidate["base"],
            "policy": candidate["policy"]}, "review": {"actor": actor, "run_id": lens + "-run", "verdict": "approve"},
            "invocation": lens + "-invocation", "usage": None}
        imported = import_review(lifecycle, token, key, envelope,
                                 {"actors": [actor], "workflow_path": "fixture-review.yml"})
        import_review(lifecycle, token, key, envelope, {"actors": [actor], "workflow_path": "fixture-review.yml"})
        reviews.append(imported)
    for check in policy["checks"]:
        lifecycle.check("ci", pr, current, check, True)
    transfer = lifecycle.transfer("owner", pr, "legacy", epoch, "corral", stopped=True)
    try:
        lifecycle.merge("owner", pr, "corral", transfer["epoch"], expected=current, lose_ack=True)
    except ConnectionError:
        pass
    receipt = lifecycle.merge("owner", pr, "corral", transfer["epoch"], expected=current)
    assert receipt["merged"] and receipt["reconciled"] and len(github.merged) == 1
    rollback = lifecycle.transfer("owner", pr, "corral", transfer["epoch"], "legacy", stopped=True)
    evidence = {"scenario": "fake-PR-with-real-repair-process", "environment": platform.platform(),
        "revision": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "command": [sys.executable, "-m", "corral.execution.pr_demo", str(directory)],
        "expected": "one repaired candidate, two required reviews, one reconciled merge, one rollback owner",
        "actual": {"repair": repair, "reviews": reviews, "merge": receipt, "rollback": rollback},
        "paid_model_calls": 0, "inference": "synthetic fixture outputs only", "pr_usage": store.records("pr_usage")}
    (directory / "evidence.json").write_text(json.dumps(evidence, indent=2))
    return evidence


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory")
    args = parser.parse_args()
    evidence = run(args.directory)
    print(json.dumps({"merged": evidence["actual"]["merge"]["merged"], "paid_calls": 0}))
