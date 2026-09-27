"""Optional host scheduling and child-process policy."""
from __future__ import annotations

import platform
import shutil
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def validate_windows(value: object) -> list[dict]:
    if not isinstance(value, list):
        raise ValueError("blackout_windows must be a list")
    for window in value:
        if not isinstance(window, dict) or set(window) != {"days", "start", "end", "timezone", "policy"}:
            raise ValueError("blackout window requires days, start, end, timezone and policy")
        days = window["days"]
        if (not isinstance(days, list) or not days or any(
                not isinstance(day, str) or day not in _DAYS for day in days)
                or len(days) != len(set(days))):
            raise ValueError("blackout days must list unique weekday names")
        for field in ("start", "end"):
            clock = window[field]
            if (not isinstance(clock, str) or len(clock) != 5 or clock[2] != ":"
                    or not clock[:2].isdigit() or not clock[3:].isdigit()
                    or int(clock[:2]) > 23 or int(clock[3:]) > 59):
                raise ValueError(f"blackout {field} must be HH:MM")
        if window["start"] == window["end"]:
            raise ValueError("blackout start and end must differ")
        if not isinstance(window["timezone"], str):
            raise ValueError("blackout timezone must be an IANA zone name")
        try:
            ZoneInfo(window["timezone"])
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("blackout timezone must be an IANA zone name") from exc
        if window["policy"] != "finish":
            raise ValueError("blackout policy must be finish; hold is not supported")
    return value


def _inside(window: dict, instant: datetime) -> bool:
    local = instant.astimezone(ZoneInfo(window["timezone"]))
    minute = local.hour * 60 + local.minute
    start = int(window["start"][:2]) * 60 + int(window["start"][3:])
    end = int(window["end"][:2]) * 60 + int(window["end"][3:])
    today = _DAYS[local.weekday()]
    previous = _DAYS[(local.weekday() - 1) % 7]
    if end > start:
        return today in window["days"] and start <= minute < end
    return (today in window["days"] and minute >= start
            or previous in window["days"] and minute < end)


def blackout(windows: list[dict], now: float) -> dict:
    instant = datetime.fromtimestamp(now, timezone.utc)
    for window in windows:
        if not _inside(window, instant):
            continue
        # Walk real UTC minutes: nonexistent spring minutes and repeated autumn minutes
        # therefore retain the correct local-time semantics at both DST transitions.
        boundary = instant.replace(second=0, microsecond=0) + timedelta(minutes=1)
        for _ in range(8 * 24 * 60):
            if not any(_inside(candidate, boundary) for candidate in windows):
                return {"active": True, "window": window, "until": boundary.isoformat()}
            boundary += timedelta(minutes=1)
        return {"active": True, "window": window, "until": None}
    return {"active": False, "window": None, "until": None}


def validate_priority(value: object) -> dict:
    if not isinstance(value, dict) or set(value) - {"nice", "low_priority_io"}:
        raise ValueError("process_priority must contain only nice and low_priority_io")
    nice = value.get("nice", 0)
    io = value.get("low_priority_io", False)
    if type(nice) is not int or not 0 <= nice <= 19 or type(io) is not bool:
        raise ValueError("process_priority needs nice 0..19 and boolean low_priority_io")
    if nice and not shutil.which("nice"):
        raise ValueError("process_priority nice executable is unavailable")
    if io:
        tool = "taskpolicy" if platform.system() == "Darwin" else "ionice" if platform.system() == "Linux" else None
        if tool is None or not shutil.which(tool):
            raise ValueError("process_priority low_priority_io tool is unavailable")
    return {"nice": nice, "low_priority_io": io}


def priority_command(command: list[str], value: dict | None) -> list[str]:
    if value is None:
        return list(command)
    policy = validate_priority(value)
    result = list(command)
    if policy["nice"]:
        result = [shutil.which("nice"), "-n", str(policy["nice"]), *result]
    if policy["low_priority_io"]:
        tool = "taskpolicy" if platform.system() == "Darwin" else "ionice"
        args = ["-b"] if tool == "taskpolicy" else ["-c3"]
        result = [shutil.which(tool), *args, *result]
    return result
