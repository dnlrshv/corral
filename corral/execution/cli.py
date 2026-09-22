"""One-shot authenticated transport endpoint; detached execution outlives clients."""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from . import continuation
from .controller import Controller
from .scheduler import ready
from corral.redaction import redact_text, safe_config_diagnostic


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--show-config", action="store_true",
                        help="Print safely redacted configuration for diagnostics")
    parser.add_argument("--execute")
    parser.add_argument("--execute-wave")
    parser.add_argument("--epoch", type=int)
    parser.add_argument("--runner-id")
    args = parser.parse_args(argv)
    config = json.loads(args.config.read_text())
    if args.show_config:
        print(json.dumps(safe_config_diagnostic(config), indent=2))
        return
    controller = Controller(config["state"], config["token"], config["hosts"],
                            default_host=config["default_host"], profiles=config.get("profiles", []))
    token = config["token"]
    if args.execute:
        result = controller.run(token, args.execute, execution_host=config["execution_host"])
    elif args.execute_wave:
        from .wave import WaveRunner
        runner = WaveRunner(controller, token)
        try:
            result = runner.run(args.execute_wave, execution_host=config["execution_host"])
        except BaseException:
            if args.epoch is not None and args.runner_id:
                controller.store.transition_owner(f"wave_runner:{args.execute_wave}", args.runner_id, args.epoch, "uncertain")
            raise
        else:
            if args.epoch is not None and args.runner_id:
                controller.store.transition_owner(f"wave_runner:{args.execute_wave}", args.runner_id, args.epoch, "released")
    else:
        request = json.load(sys.stdin)
        action = request.pop("action")
        if action == "submit":
            result = {"task": controller.submit(token, request["request_id"], request["spec"])}
        elif action in ("dispatch", "dispatch-wave"):
            requests = controller.store.records("request")
            scope = [request["task_id"]] if action == "dispatch" else request["task_ids"]
            if not scope or any(task not in requests for task in scope):
                raise ValueError("dispatch requires an explicit existing task scope")
            states = controller.store.records("state")
            # A dependency is satisfied by the task's CURRENT result, so a continued task whose
            # latest generation was rejected does not keep admitting its dependents.
            completed = [task for task in requests
                         if (continuation.current_result(controller.store, task) or {}).get("accepted")]
            host_id = config["execution_host"]
            host = {"cpu": os.cpu_count() or 1, "memory_mb": 0,
                    "interactive_boost_seconds": 10, "routes": ["deterministic"],
                    **config["hosts"][host_id]}
            host["routes"] = [*host.get("routes", []), "deterministic"]
            tasks = [{**controller.context(key), "id": key,
                      "paused": controller.context(key).get("pause_dispatch", False),
                      "submitted": controller.store.get("initial", key)["submitted"],
                      "route": (value["selection"].get("profile") or {}).get("route", "deterministic")}
                     for key, value in requests.items() if key in scope and value["host"] == host_id
                     and (key not in states or continuation.pending_generation(controller.store, key))
                     and not controller.store.get("cancel", key)]
            running = [requests[k] for k, value in states.items()
                       if value["status"] in ("running", "dispatching", "uncertain") and value["host"] == host_id]
            admitted = ready(tasks, completed, running, host, time.time())
            for task in admitted:
                with (Path(config["state"]) / "dispatch.log").open("ab") as log:
                    subprocess.Popen([sys.executable, "-m", "corral.execution.cli", "--config",
                                      str(args.config.resolve()), "--execute", task],
                                     stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                     start_new_session=True)
            result = {"admitted": admitted, "dispatch": "capacity/dependency-ready; reconcile status"}
        elif action == "status":
            result = controller.status(token, request["task_id"])
        elif action == "steer":
            controller.steer(token, request["task_id"], request["amendment_id"], request["amendment"])
            result = controller.status(token, request["task_id"])
        elif action == "continue":
            # Explicit post-terminal continuation of the SAME task identity: checkpoints the
            # terminal attempt and schedules one new generation. It never dispatches by itself.
            result = controller.continue_task(token, request["task_id"], request["continuation_id"],
                                              request["continuation"])
        elif action == "cancel":
            result = controller.cancel(token, request["task_id"])
        elif action == "reconcile":
            from .reconciliation_api import reconcile_local

            if "observation" in request:
                raise PermissionError("CLI reconciliation does not accept caller-provided observations")
            result = reconcile_local(controller, token, request["task_id"])
        elif action == "snapshot":
            result = controller.snapshot(token, request["task_id"], request["paths"])
        elif action == "fetch-artifact":
            result = controller.fetch_artifact(token, request["task_id"], request["path"], request.get("generation"))
        elif action == "transfer":
            result = controller.transfer(token, request["task_id"], request["transfer_id"],
                                         request["incoming"], request["expected"])
        elif action in ("run-wave", "step-wave", "submit-wave", "wave-status", "reconcile-wave"):
            from .wave import WaveRunner, WAVE_STATE_KIND, WAVE_KIND

            runner = WaveRunner(controller, token)
            if action == "wave-status":
                state = controller.store.get(WAVE_STATE_KIND, request["wave_id"])
                wave_record = controller.store.get(WAVE_KIND, request["wave_id"])
                with controller.store.transaction() as db:
                    owner_row = db.execute("SELECT owner,epoch,status FROM owners WHERE resource=?", (f"wave_runner:{request['wave_id']}",)).fetchone()
                runner_status = {"owner": owner_row[0], "epoch": owner_row[1], "status": owner_row[2]} if owner_row else None
                result = {"wave_id": request["wave_id"], "state": state, "record": wave_record, "runner": runner_status}
            elif action == "reconcile-wave":
                with controller.store.transaction() as db:
                    owner_row = db.execute("SELECT owner,epoch,status FROM owners WHERE resource=?", (f"wave_runner:{request['wave_id']}",)).fetchone()
                reconciled = False
                reason = "no active owner"
                if owner_row and owner_row[2] in ("uncertain", "active"):
                    owner_id = owner_row[0]
                    pid_record = controller.store.get("wave_runner_pid", request["wave_id"])
                    if pid_record and pid_record.get("owner") == owner_id:
                        try:
                            os.kill(pid_record["pid"], 0)
                            reason = "process is still alive"
                        except ProcessLookupError:
                            controller.store.transition_owner(f"wave_runner:{request['wave_id']}", owner_id, owner_row[1], "released")
                            reconciled = True
                            reason = "proven dead"
                        except Exception as e:
                            reason = f"cannot verify process: {e}"
                            controller.store.transition_owner(f"wave_runner:{request['wave_id']}", owner_id, owner_row[1], "uncertain")
                    else:
                        reason = "unobservable identity"
                        controller.store.transition_owner(f"wave_runner:{request['wave_id']}", owner_id, owner_row[1], "uncertain")
                result = {"wave_id": request["wave_id"], "reconciled": reconciled, "reason": reason}
            elif action == "submit-wave":
                result = runner.submit_wave(request["wave_id"], request["tasks"], request.get("handoffs"))
            elif action == "step-wave":
                result = runner.step(request["wave_id"], execution_host=config["execution_host"])
            elif action == "run-wave":
                if "tasks" in request:
                    runner.submit_wave(request["wave_id"], request["tasks"], request.get("handoffs"))
                import uuid
                runner_id = f"wave_runner_{uuid.uuid4().hex[:8]}"
                try:
                    epoch = controller.store.acquire(f"wave_runner:{request['wave_id']}", runner_id)
                except PermissionError:
                    result = {"wave_id": request["wave_id"], "dispatch": "already-running-or-uncertain"}
                else:
                    with (Path(config["state"]) / "wave_dispatch.log").open("ab") as log:
                        proc = subprocess.Popen([sys.executable, "-m", "corral.execution.cli", "--config",
                                          str(args.config.resolve()), "--execute-wave", request["wave_id"],
                                          "--epoch", str(epoch), "--runner-id", runner_id],
                                         stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                         start_new_session=True)
                        controller.store.replace("wave_runner_pid", request["wave_id"], {"pid": proc.pid, "owner": runner_id})
                    result = {"wave_id": request["wave_id"], "dispatch": "launched", "runner_id": runner_id}
        else:
            raise ValueError("unsupported action")
    print(json.dumps(result))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        err = "".join(traceback.format_exception(type(e), e, e.__traceback__))
        sys.stderr.write(redact_text(err, marker="[REDACTED]"))
        sys.exit(1)
