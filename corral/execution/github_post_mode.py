"""Durable descriptions of GitHub writes withheld by host policy."""
from __future__ import annotations


def validate(mode: str) -> str:
    if mode not in ("dry-run", "comment"):
        raise ValueError("github_post_mode must be dry-run or comment")
    return mode


def record(store, intent: str, *, method: str, endpoint: str, body: dict) -> dict:
    value = {"status": "dry-run", "intent": intent, "method": method,
             "endpoint": endpoint, "body": body}
    store.put_once("github_dry_run", intent, value)
    return value
