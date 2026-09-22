"""Process-backed offline M1 demo. All model/usage evidence is synthetic."""
import argparse
import json
import platform
import subprocess
import sys
from pathlib import Path

from .controller import Controller
from .profiles import Profile, resolve


def fixture_profiles():
    return [Profile("strong-low", "fixture-strong", "low", "synthetic", "1", "fixture",
                    ("implementation", "review", "repair", "adjudication"),
                    ("shell", "edit", "search"), 10000),
            Profile("weak-xhigh", "fixture-weak", "xhigh", "synthetic", "1", "fixture",
                    ("implementation", "review", "repair", "adjudication"),
                    ("shell", "edit", "search"), 10000)]


def one_task(directory, host="local-fixture"):
    directory = Path(directory).resolve()
    repo = directory / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    if not (repo / ".git").exists():
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                        "commit", "--allow-empty", "-qm", "synthetic fixture"], cwd=repo, check=True)
    worker = """import json, os, pathlib, subprocess, sys, time
pathlib.Path('related.txt').write_text('6 7')
numbers = [int(x) for x in pathlib.Path('related.txt').read_text().split()]
negative = subprocess.run([sys.executable, '-c', 'assert 6 * 7 == 41'])
assert negative.returncode != 0
time.sleep(1.2)  # sparse deterministic wait, no model/status polling
result = {'answer': numbers[0] * numbers[1], 'prose': '', 'negative_experiment': 'rejected'}
pathlib.Path('result.json').write_text(json.dumps(result))
pathlib.Path(os.environ['CORRAL_USAGE_PATH']).write_text(json.dumps([
    {'id':'fixture-usage-1','scope':'session','epoch':0,'sequence':1,
     'mode':'cumulative','counters':{'input':100,'output':20,'turns':3,'retries':2}}]))
"""
    controller = Controller(directory / "controller", "synthetic-owner", {host: {
        "routes": ["fixture"], "harnesses": ["synthetic"]}},
        default_host=host, profiles=fixture_profiles())
    selected = resolve(fixture_profiles(), role="implementation", routes=["fixture"],
                       evidence={"strong-low": 9, "weak-xhigh": 2}, default="strong-low")
    expected = {"answer": 42, "prose": "", "negative_experiment": "rejected"}
    spec = {"repo": "synthetic-example", "workspace": str(repo), "command": [sys.executable, "-c", worker],
            "verify": [sys.executable, "-c", f"import json; assert json.load(open('result.json')) == {expected!r}"],
            "candidate_paths": ["result.json", "related.txt"], "selection": selected,
            "soft_thresholds": {"input": 10, "turns": 1, "retries": 1}}
    task = controller.submit("synthetic-owner", "one-task-v1", spec)
    actual = controller.run("synthetic-owner", task, execution_host=host)
    assert actual["result"]["accepted"] and actual["result"]["structured"] == expected
    assert set(actual["result"]["usage"]["soft_thresholds"]) == {"input", "turns", "retries"}
    evidence = {"scenario": "M1-one-task", "proof": "real-process/synthetic-model-and-usage",
                "revision": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
                "command": [sys.executable, "-m", "corral.execution.demo", str(directory)],
                "environment": platform.platform(), "expected": expected, "actual": actual}
    (directory / "evidence.json").write_text(json.dumps(evidence, indent=2))
    return evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory")
    parser.add_argument("--host", default="local-fixture")
    args = parser.parse_args()
    result = one_task(args.directory, args.host)
    print(json.dumps({"accepted": result["actual"]["result"]["accepted"],
                      "evidence": str(Path(args.directory) / "evidence.json")}))


if __name__ == "__main__":
    main()
