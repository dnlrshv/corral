"""Coherent helper routines for GitHub advisory transport, HTTP dispatch, and receipt persistence."""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> Any:
        raise PermissionError(
            f"HTTP redirect prohibited to protect credentials (attempted redirect to {newurl})"
        )


def compute_canonical_wire_hash(
    head: str, body: str, comments: list[Any] | None = None
) -> tuple[str, dict[str, Any]]:
    """Compute canonical sha256 hash and wire dictionary for review comments."""
    wire_payload: dict[str, Any] = {
        "commit_id": head,
        "event": "COMMENT",
        "body": body,
    }
    if comments:
        wire_payload["comments"] = comments
    wire_bytes = json.dumps(wire_payload, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(wire_bytes).hexdigest(), wire_payload


COMMENT_FIELDS = (
    "path",
    "body",
    "line",
    "side",
    "start_line",
    "start_side",
    "position",
    "subject_type",
)


def comments_match(remote: list[Any], expected: list[Any]) -> bool:
    """Compare full wire semantics; API metadata does not grant provenance."""

    def normalize(comment: Any) -> str:
        if (
            not isinstance(comment, dict)
            or not isinstance(comment.get("path"), str)
            or not isinstance(comment.get("body"), str)
        ):
            raise ValueError("malformed review comment")
        fields = {k: comment[k] for k in COMMENT_FIELDS if comment.get(k) is not None}
        if fields.get("line") is not None:
            fields.pop(
                "position", None
            )  # Derived API metadata for modern line comments.
            if fields.get("subject_type", "line") == "line":
                fields.pop("subject_type", None)
        return json.dumps(fields, sort_keys=True)

    return sorted(map(normalize, remote)) == sorted(map(normalize, expected))


def read_pages(request_fn: Callable[..., Any], path: str) -> list[dict[str, Any]]:
    items = []
    page = 1
    while True:
        response = request_fn("GET", f"{path}?per_page=100&page={page}")
        if not isinstance(response, list) or any(
            not isinstance(item, dict) for item in response
        ):
            raise ValueError("malformed paginated GitHub response")
        items.extend(response)
        if len(response) < 100:
            return items
        page += 1


def validate_review_receipt(
    request_fn: Callable[..., Any],
    repo: str,
    number: int,
    review: Any,
    head: str,
    body: str,
    comments: list[Any] | None,
    actor: str,
) -> dict[str, Any]:
    if not isinstance(review, dict):
        raise ValueError("malformed review receipt")
    review_id = review.get("id")
    if type(review_id) is not int or review_id <= 0:
        raise ValueError("invalid review receipt id")
    if (
        review.get("state") != "COMMENTED"
        or review.get("commit_id") != head
        or review.get("body") != body
        or not isinstance(review.get("user"), dict)
        or review["user"].get("login") != actor
    ):
        raise ValueError("review receipt does not match authenticated wire payload")
    remote_comments = read_pages(
        request_fn, f"/repos/{repo}/pulls/{number}/reviews/{review_id}/comments"
    )
    if not comments_match(remote_comments, comments or []):
        raise ValueError("review receipt comments differ from wire payload")
    return review


def parse_pr_identity(pr: str) -> tuple[str, int]:
    """Parse repository PR identity string formatted as <repo>#<number>."""
    parts = pr.split("#", 1) if isinstance(pr, str) and "#" in pr else ["", ""]
    if not parts[0] or not parts[1].isdigit():
        raise ValueError(
            f"Explicit PR repository identity required, format: <repo>#<number>, got: {pr!r}"
        )
    return parts[0], int(parts[1])


def verify_remote_candidate(
    request_fn: Callable[..., Any],
    repo: str,
    number: int,
    expected_head: str,
    expected_base: str,
) -> dict[str, Any]:
    """Fetch remote PR and verify candidate head/base have not drifted."""
    pull = request_fn("GET", f"/repos/{repo}/pulls/{number}")
    pull = pull if isinstance(pull, dict) else json.loads(pull)
    remote_head, remote_base = (
        (pull.get("head") or {}).get("sha"),
        (pull.get("base") or {}).get("sha"),
    )
    if pull.get("state") != "open":
        raise PermissionError(f"remote PR #{number} is closed or merged")
    if remote_head != expected_head or remote_base != expected_base:
        raise PermissionError(
            f"remote head/base candidate changed: remote ({remote_head[:10] if remote_head else 'None'}, "
            f"{remote_base[:10] if remote_base else 'None'}) vs expected ({expected_head[:10]}, {expected_base[:10]})"
        )
    return pull


def has_intent_provenance(
    store: Any,
    intent: str,
    resource: str,
    head: str,
    base: str,
    payload: dict[str, Any] | None = None,
) -> bool:
    """Only immutable attempted payloads establish prior base/policy provenance."""
    if store is None or payload is None:
        return False
    from .publication_validation import validate_payload

    try:
        validate_payload(resource[3:], intent, payload)
    except (ValueError, PermissionError, KeyError, TypeError):
        return False
    with store.transaction() as db:
        row = db.execute(
            "SELECT resource,head_sha,base_sha,payload FROM publication_intents WHERE intent=?",
            (intent,),
        ).fetchone()
    return bool(
        row and row[:3] == (resource, head, base) and json.loads(row[3]) == payload
    )


def find_remote_matching_review(
    request_fn: Callable[..., Any],
    repo: str,
    number: int,
    head: str,
    expected_body: str,
    expected_comments: list[Any] | None = None,
    bridge_actor: str | None = None,
) -> dict[str, Any] | None:
    for review in read_pages(request_fn, f"/repos/{repo}/pulls/{number}/reviews"):
        # GitHub reports ``"body": null`` for reviews that carry only line comments.
        if "body" in review and review["body"] is None:
            review = {**review, "body": ""}
        # Incomplete rows cannot prove absence and therefore cannot permit a POST.
        if (
            type(review.get("id")) is not int
            or review["id"] <= 0
            or not isinstance(review.get("user"), dict)
            or not isinstance(review["user"].get("login"), str)
            or not isinstance(review.get("commit_id"), str)
            or not isinstance(review.get("body"), str)
            or not isinstance(review.get("state"), str)
        ):
            raise ValueError("incomplete remote review metadata")
        if (
            review["commit_id"] != head
            or review["body"] != expected_body
            or review["user"]["login"] != bridge_actor
        ):
            continue
        # A changed state/comment set is a conflicting publication, not permission to repost.
        return validate_review_receipt(
            request_fn,
            repo,
            number,
            review,
            head,
            expected_body,
            expected_comments,
            bridge_actor,
        )
    return None


def persist_delivery_receipt(
    store: Any,
    intent: str,
    resource: str,
    receipt: dict[str, Any],
    *,
    attempt_id: str | None = None,
    clear_lease: bool = False,
) -> None:
    from .publication_store import record_outcome

    record_outcome(
        store,
        intent,
        resource,
        "delivered",
        review_id=receipt["review_id"],
        attempt_id=attempt_id,
        receipt=receipt,
    )


class GitHubHTTPError(RuntimeError):
    """Keep HTTP status separate from arbitrary untrusted response body text."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def execute_github_request(
    opener: urllib.request.OpenerDirector,
    http_client: Callable[..., Any] | None,
    headers: dict[str, str],
    allow_network: bool,
    timeout: float,
    method: str,
    path: str,
    json_data: dict[str, Any] | None = None,
    sanitize_fn: Callable[[str], str] | None = None,
) -> Any:
    """Safe HTTP boundary dispatching strictly to https://api.github.com without retries for POST."""
    _clean = sanitize_fn or (lambda m: m)
    if http_client is not None:
        return http_client(method, path, headers=headers, json=json_data)
    if not allow_network:
        raise PermissionError(
            "live network calls prohibited; fake HTTP or saved events handler required"
        )

    url = (
        path
        if path.startswith(("http://", "https://"))
        else f"https://api.github.com{path if path.startswith('/') else '/' + path}"
    )
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.netloc != "api.github.com":
        raise PermissionError(
            "HTTP requests strictly restricted to https://api.github.com"
        )

    data_bytes = (
        json.dumps(json_data).encode("utf-8") if json_data is not None else None
    )
    req = urllib.request.Request(url, data=data_bytes, headers=headers, method=method)
    try:
        with opener.open(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as err:
        err_body = err.read().decode("utf-8", errors="replace")
        raise GitHubHTTPError(
            err.code, _clean(f"GitHub API {err.code}: {err.reason} - {err_body}")
        ) from None
    except Exception as exc:
        raise RuntimeError(_clean(f"GitHub HTTP transport error: {exc}")) from None
