"""Shared fixtures for post-terminal continuation regressions.

The stage 1 / stage 2 pair mirrors the real duration-parser pilot: an accepted ``h``/``m``
parser is amended into a ``d``/``h``/``m`` parser with descending-order and duplicate
rejection, judged by a DIFFERENT controller-owned verifier script under the SAME host
verifier root. Nothing here is monkeypatched into Corral.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from corral.execution.controller import Controller

STAGE1_SOURCE = '''import re

_PATTERN = re.compile(r"^(?:(\\d+)h)?(?:(\\d+)m)?$")
_MINUTES = {"h": 60, "m": 1}


def parse_duration(text):
    """Stage 1 contract: hours and minutes only, in descending order."""
    match = _PATTERN.match(text.strip())
    if not match or not any(match.groups()):
        raise ValueError(f"invalid duration: {text!r}")
    hours, minutes = (int(value) if value else 0 for value in match.groups())
    return hours * _MINUTES["h"] + minutes * _MINUTES["m"]
'''

STAGE2_SOURCE = '''import re

_TOKEN = re.compile(r"(\\d+)([dhm])")
_MINUTES = {"d": 1440, "h": 60, "m": 1}
_RANK = {"d": 0, "h": 1, "m": 2}


def parse_duration(text):
    """Stage 2 contract: days added, units must descend d/h/m, no duplicates."""
    value = text.strip()
    if not value:
        raise ValueError("invalid duration: empty")
    tokens = _TOKEN.findall(value)
    if not tokens or "".join(a + u for a, u in tokens) != value.replace(" ", ""):
        raise ValueError(f"invalid duration: {text!r}")
    units = [unit for _amount, unit in tokens]
    if len(set(units)) != len(units):
        raise ValueError(f"duplicate unit in duration: {text!r}")
    if [_RANK[unit] for unit in units] != sorted(_RANK[unit] for unit in units):
        raise ValueError(f"units must descend d/h/m: {text!r}")
    return sum(int(amount) * _MINUTES[unit] for amount, unit in tokens)
'''

STAGE1_OBJECTIVE = "Implement parse_duration for h/m durations, returning total minutes."
STAGE2_OBJECTIVE = ("Amend parse_duration to also accept days. Units must appear in descending "
                    "order d/h/m; duplicates and wrong order are errors.")

#: Controller-owned verifiers. Each is a separate script under the same host verifier root and
#: neither imports from the worker cwd.
VERIFY_STAGE1 = '''"""Controller-owned stage 1 verifier; never imports from the worker cwd."""
import sys
from pathlib import Path

source = Path(sys.argv[1]).read_text()
namespace = {}
exec(compile(source, sys.argv[1], "exec"), namespace)
parse = namespace["parse_duration"]
assert parse("2h") == 120, "hours"
assert parse("45m") == 45, "minutes"
assert parse("1h30m") == 90, "descending h then m"
for invalid in ("", "abc", "-1h", "1.5h", "1d", "1h2h"):
    try:
        parse(invalid)
    except ValueError:
        continue
    raise AssertionError(f"stage 1 must reject {invalid!r}")
'''

VERIFY_STAGE2 = '''"""Controller-owned stage 2 verifier: days, strict order, duplicate rejection."""
import sys
from pathlib import Path

source = Path(sys.argv[1]).read_text()
namespace = {}
exec(compile(source, sys.argv[1], "exec"), namespace)
parse = namespace["parse_duration"]
assert parse("1d") == 1440, "days"
assert parse("1d2h3m") == 1563, "descending d/h/m"
assert parse("2h") == 120 and parse("1h30m") == 90, "stage 1 behaviour is preserved"
for invalid in ("1h2h", "1d1d", "2h1d", "1m1d", "abc", "", "-1d", "1.5d", "1x"):
    try:
        parse(invalid)
    except ValueError:
        continue
    except Exception as error:
        raise AssertionError(f"stage 2 must reject {invalid!r} with ValueError, got {error!r}")
    raise AssertionError(f"stage 2 must reject {invalid!r}")
'''


def _git_init(directory: Path) -> None:
    subprocess.run(["git", "init", "-q", str(directory)], check=True)
    subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=f@example.invalid",
                    "commit", "--allow-empty", "-qm", "fixture"], cwd=directory, check=True)


def write_verifier(roots: Path, name: str, body: str) -> Path:
    """Author a controller-owned verifier inside the host-declared verifier root."""
    path = Path(roots) / name
    path.write_text(body)
    return path


def verify_argv(roots: Path, name: str, candidate: str = "duration.py") -> list[str]:
    return [sys.executable, str(Path(roots) / name), candidate]


def deterministic_env(tmp_path: Path) -> dict:
    """Local controller with a real external verifier root; no native route, no sandbox."""
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    _git_init(workspace)
    (workspace / "duration.py").write_text("def parse_duration(text):\n    return 0\n")
    roots = tmp_path / "verifiers"
    roots.mkdir(parents=True, exist_ok=True)
    write_verifier(roots, "verify_stage1.py", VERIFY_STAGE1)
    host = {"routes": ["fixture"], "harnesses": ["synthetic"], "cpu": 4, "memory_mb": 1024,
            "verifier_roots": [str(roots)]}
    controller = Controller(tmp_path / "state", "owner", {"fixture": host},
                            default_host="fixture", profiles=[])
    return {"controller": controller, "workspace": workspace, "roots": roots, "host": host,
            "state": tmp_path / "state", "token": "owner", "host_id": "fixture"}


STAGE1_STRUCTURED = {"function": "parse_duration", "units": ["h", "m"]}
STAGE2_STRUCTURED = {"function": "parse_duration", "units": ["d", "h", "m"],
                     "errors": ["duplicate-unit", "descending-order"]}
#: Objective marker -> (candidate source, structured completion) for the real worker below.
DEFAULT_CHOICES = (("days", STAGE2_SOURCE, STAGE2_STRUCTURED),
                   ("h/m", STAGE1_SOURCE, STAGE1_STRUCTURED))


def worker_code(choices=DEFAULT_CHOICES, *, sleep_seconds: float = 0.0, usage=None) -> str:
    """Real worker code that reads its objective from the controller-written context.

    The command itself is inherited execution authority, so a continuation can only change
    what the worker does through the amended objective -- exactly the contract under test.

    ``usage`` maps a generation to the counters that generation reports through the
    controller-owned usage channel, so per-invocation attribution and cumulative/delta
    semantics can be checked on the deterministic path too. The command is inherited, so the
    worker can only tell the generations apart from the context the controller wrote.
    """
    payload = json.dumps([{"marker": marker, "source": source, "structured": structured}
                          for marker, source, structured in choices])
    lines = ["import json, os, time",
             "from pathlib import Path",
             f"time.sleep({sleep_seconds})",
             f"choices = json.loads({payload!r})",
             "context = json.loads(Path(os.environ['CORRAL_CONTEXT_PATH']).read_text())",
             "objective = context.get('objective') or ''",
             "selected = next((c for c in choices if c['marker'] in objective), choices[-1])",
             "Path('duration.py').write_text(selected['source'])",
             "Path('result.json').write_text(json.dumps(selected['structured']))"]
    if usage is not None:
        lines += [f"usage_table = json.loads({json.dumps(usage)!r})",
                  "entry = usage_table.get(str(context.get('generation') or 1))",
                  "if entry:",
                  "    event = {'id': 'generation-counter', 'scope': 'session', 'epoch': 0,",
                  "               'sequence': 1, 'mode': entry['mode'],",
                  "               'counters': entry['counters']}",
                  "    Path(os.environ['CORRAL_USAGE_PATH']).write_text(json.dumps([event]))"]
    return "\n".join(lines) + "\n"


def deterministic_spec(env: dict, *, verifier: str = "verify_stage1.py", choices=DEFAULT_CHOICES,
                       sleep_seconds: float = 0.0, usage=None, **overrides) -> dict:
    spec = {"repo": "duration-parser", "workspace": str(env["workspace"]),
            "candidate_paths": ["duration.py"], "verifier_paths": [], "external_verifier": True,
            "objective": STAGE1_OBJECTIVE,
            "command": [sys.executable, "-c",
                        worker_code(choices, sleep_seconds=sleep_seconds, usage=usage)],
            "verify": verify_argv(env["roots"], verifier)}
    spec.update(overrides)
    return spec


def stage2_continuation(env: dict, *, continuation_id: str = "stage2-days",
                        objective: str = STAGE2_OBJECTIVE, verifier: str = "verify_stage2.py",
                        **overrides) -> dict:
    """A legitimate stage 2 continuation payload: new objective + a host-rooted verifier."""
    payload = {"objective": objective, "verify": verify_argv(env["roots"], verifier),
               "external_verifier": True, "candidate_paths": ["duration.py"]}
    payload.update(overrides)
    return payload


def dispatch_stage2(env: dict, *, verifier: str = "verify_stage2.py") -> None:
    """Author the stage 2 verifier in the same controller-owned root, before it is selected."""
    write_verifier(env["roots"], verifier, VERIFY_STAGE2)


def run_stage1(env: dict, request_id: str = "duration-parser", **spec_overrides) -> tuple[str, dict]:
    controller = env["controller"]
    task = controller.submit("owner", request_id, deterministic_spec(env, **spec_overrides))
    return task, controller.run("owner", task, execution_host=env["host_id"])


def restart(env: dict, profiles=()) -> Controller:
    """A fresh controller process over the same durable state (restart / lost-ack path)."""
    return Controller(env["state"], "owner", {"fixture": env["host"]}, default_host="fixture",
                      profiles=list(profiles))
