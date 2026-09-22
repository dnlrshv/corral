"""One-shot authenticated endpoint over the authoritative Corral service Store."""
from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path

from .service import Service


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args(argv)
    request = json.load(__import__("sys").stdin)
    action = request.pop("action")
    service = Service(args.config)
    if action == "submit":
        result = service.submit(**request)
    elif action == "status":
        result = service.status(request["event_id"])
    elif action == "tick":
        result = service.tick(request.get("now"))
    elif action == "amend":
        result = service.amend(request["event_id"], request["amendment_id"],
                               request["objective"])
    elif action == "pause":
        result = service.pause(request["event_id"], request["amendment_id"],
                               request.get("paused", True))
    elif action == "cancel":
        result = service.cancel(request["event_id"])
    elif action == "continue":
        result = service.continue_event(request["event_id"], request["continuation_id"],
                                        request["objective"])
    elif action == "reconcile":
        service.tick(request.get("now"))
        result = service.status(request["event_id"])
    elif action == "wave":
        from .client import Client
        result = Client({"python": __import__("sys").executable,
                         "controller_config": str(service.controller_path),
                         "source": str(Path(__file__).parents[2]),
                         "transport": "local"}).call(
                             "run-wave", wave_id=request["wave_id"],
                             tasks=request["tasks"], handoffs=request.get("handoffs"))
    elif action == "fetch":
        event = service.store.get("service_event", request["event_id"])
        if not event or not event.get("task_id"):
            raise KeyError(request["event_id"])
        result = service.controller.fetch_artifact(
            service.token, event["task_id"], request["relative_path"])
        base64.b64decode(result["data"], validate=True)
    else:
        raise ValueError("unsupported service action")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
