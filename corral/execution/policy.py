"""Authenticated live GitHub policy snapshot capture, canonical normalization, and verification."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .policy_schema import validate_file, validate_protection, validate_ruleset
from .policy_inputs import normalize as normalize_policy_inputs

REQUIRED_SOURCE_PATHS = ()  # Compatibility name; sources are repository-owned inputs.

MANDATORY_SNAPSHOT_KEYS = (
    "schema_version",
    "repo",
    "base_sha",
    "enforcement_contents",
    "enforcement_digest",
)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Strictly refuse HTTP redirects to prevent credential exposure."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        raise RuntimeError("HTTP redirects prohibited on authenticated policy endpoints")


def canonical_json(obj: Any) -> str:
    """Return deterministically sorted and formatted JSON representation."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _strip_dynamic_ruleset_metadata(r: dict[str, Any]) -> dict[str, Any]:
    """Strip timestamps, node IDs, URLs, and caller permissions from ruleset."""
    rules = r.get("rules") or []
    sorted_rules = (
        sorted(rules, key=lambda item: (item.get("type", ""), canonical_json(item.get("parameters", {}))))
        if isinstance(rules, list)
        else rules
    )

    bypass = r.get("bypass_actors") or []
    sorted_bypass = (
        sorted(bypass, key=lambda b: (b.get("actor_type", ""), b.get("actor_id", 0), b.get("bypass_mode", "")))
        if isinstance(bypass, list)
        else bypass
    )

    return {
        "id": r.get("id"),
        "name": r.get("name"),
        "target": r.get("target"),
        "enforcement": r.get("enforcement"),
        "conditions": r.get("conditions"),
        "rules": sorted_rules,
        "bypass_actors": sorted_bypass,
    }


def _strip_dynamic_protection_metadata(p: dict[str, Any] | None) -> dict[str, Any] | None:
    """Strip URLs and transient fields from classic protection dict."""
    if not p or not isinstance(p, dict):
        return None
    cleaned = dict(p)
    cleaned.pop("url", None)
    cleaned.pop("_links", None)
    return cleaned


def compute_policy_digest(enforcement: dict[str, Any]) -> str:
    """Compute stable SHA-256 digest of normalized enforcement contents."""
    canonical_repr = canonical_json(enforcement)
    return hashlib.sha256(canonical_repr.encode("utf-8")).hexdigest()


def _get_auth_token(token: Optional[str] = None) -> Optional[str]:
    """Retrieve token without printing or logging token secrets."""
    if token:
        return token
    env_token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if env_token:
        return env_token
    token_file = os.environ.get("GITHUB_TOKEN_FILE")
    if token_file:
        tf_path = Path(token_file)
        if not tf_path.exists():
            raise FileNotFoundError(f"Token file does not exist: {token_file}")
        if tf_path.is_symlink():
            resolved = tf_path.resolve()
            if not resolved.exists():
                raise FileNotFoundError(f"Token file symlink target does not exist: {resolved}")
            st_target = resolved.stat()
            if hasattr(os, "getuid") and st_target.st_uid != os.getuid():
                raise PermissionError(f"Token file symlink target not owned by current user: {resolved}")
        st = tf_path.stat()
        if hasattr(os, "getuid") and st.st_uid != os.getuid():
            raise PermissionError(f"Token file not owned by current user: {token_file}")
        mode = stat.S_IMODE(st.st_mode)
        if mode & 0o077 != 0:
            raise PermissionError(f"Insecure permissions on token file {token_file}: {oct(mode)}; must be 0600 or 0400")
        return tf_path.read_text(encoding="utf-8").strip()
    try:
        res = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=False)
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
    except Exception:
        pass
    return None


def fetch_policy_snapshot(
    repo: str,
    base_sha: str,
    *,
    base_ref: str = "main",
    http_client: Any = None,
    token: Optional[str] = None,
    policy_inputs: dict | None = None,
) -> dict[str, Any]:
    """Fetch live authenticated GitHub repository policy snapshot (GET only)."""
    inputs = normalize_policy_inputs(policy_inputs)
    if http_client is not None:
        return _fetch_via_http_client(repo, base_sha, base_ref, http_client, inputs)

    auth_token = _get_auth_token(token)
    if not auth_token:
        raise RuntimeError("GitHub policy snapshot unavailable: authenticated token required")

    opener = urllib.request.build_opener(_NoRedirectHandler())
    urllib.request.install_opener(opener)

    headers = {
        "Authorization": f"Bearer {auth_token}",
        "User-Agent": "corral-advisory-policy",
        "Accept": "application/vnd.github+json",
    }

    def _sanitize(msg: str) -> str:
        return msg.replace(auth_token, "[REDACTED_TOKEN]") if auth_token in msg else msg

    def _http_get(path: str) -> tuple[int, Any]:
        url = f"https://api.github.com{path}"
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != "api.github.com":
            raise PermissionError(f"HTTP requests strictly restricted to https://api.github.com, got: {url}")
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                final_url = resp.geturl() if hasattr(resp, "geturl") else url
                if final_url:
                    p_final = urllib.parse.urlsplit(final_url)
                    if p_final.scheme != "https" or p_final.netloc != "api.github.com":
                        raise RuntimeError("HTTP redirects prohibited on authenticated policy endpoints")
                body = resp.read().decode("utf-8")
                return getattr(resp, "status", 200), json.loads(body) if body else {}
        except urllib.error.HTTPError as err:
            return err.code, None
        except Exception as exc:
            if "redirect" in str(exc).lower():
                raise RuntimeError("HTTP redirects prohibited on authenticated policy endpoints") from None
            raise RuntimeError(f"GitHub policy snapshot request failed: {_sanitize(str(exc))}") from None

    return _assemble_snapshot(repo, base_sha, base_ref, _http_get, inputs)


def _fetch_via_http_client(repo: str, base_sha: str, base_ref: str, client: Any, inputs: dict) -> dict[str, Any]:
    """Adapt mock or caller-supplied HTTP client to GET-only snapshot assembler."""
    def _client_get(path: str) -> tuple[int, Any]:
        clean_path = path.split("#")[0]
        try:
            return 200, client("GET", clean_path)
        except Exception as exc:
            status = exc.code if isinstance(exc, urllib.error.HTTPError) else getattr(exc, "status", None)
            if type(status) is int:
                return status, None
            raise

    return _assemble_snapshot(repo, base_sha, base_ref, _client_get, inputs)


def _assemble_snapshot(
    repo: str,
    base_sha: str,
    base_ref: str,
    get_fn: Any,
    inputs: dict,
) -> dict[str, Any]:
    """Assemble normalized policy snapshot with complete fail-closed verification."""
    # 1. Paginated Rulesets API
    rulesets: list[dict[str, Any]] = []
    page = 1
    while True:
        s_list, p_items = get_fn(f"/repos/{repo}/rulesets?per_page=100&page={page}&includes_parents=true#/rulesets")
        if s_list in (401, 403):
            raise PermissionError(f"Insufficient permissions to list rulesets for '{repo}' (status {s_list})")
        if s_list != 200 or not isinstance(p_items, list):
            raise RuntimeError(f"Failed to query repository rulesets for '{repo}' (status {s_list})")
        for r in p_items:
            rid = r.get("id") if isinstance(r, dict) else None
            if type(rid) is not int or rid <= 0:
                raise ValueError("Malformed ruleset listing entry")
            s_detail, detail = get_fn(f"/repos/{repo}/rulesets/{rid}")
            if s_detail in (401, 403):
                raise PermissionError(f"Insufficient permissions to read ruleset detail {rid} on '{repo}' (status {s_detail})")
            if s_detail != 200 or not isinstance(detail, dict):
                raise RuntimeError(f"Failed to fetch ruleset detail {rid} on '{repo}' (status {s_detail})")
            validate_ruleset(detail, rid)
            rulesets.append(_strip_dynamic_ruleset_metadata(detail))
        if len(p_items) < 100:
            break
        page += 1
    rulesets.sort(key=lambda item: int(item.get("id") or 0))

    # 2. Classic Protection (only genuine 404 is absence)
    s_prot, prot = get_fn(f"/repos/{repo}/branches/{base_ref}/protection")
    if s_prot == 200 and isinstance(prot, dict):
        validate_protection(prot)
        classic_protection = _strip_dynamic_protection_metadata(prot)
    elif s_prot == 404:
        classic_protection = None
    elif s_prot in (401, 403):
        raise PermissionError(f"Insufficient permissions to read branch protection on {base_ref}: {s_prot}")
    else:
        raise RuntimeError(f"Failed to read branch protection on {base_ref}: {s_prot}")

    # 3. Workflows directory listing
    s_wf, wf_list = get_fn(f"/repos/{repo}/contents/.github/workflows?ref={base_sha}")
    workflows: list[dict[str, Any]] = []
    if s_wf == 200 and isinstance(wf_list, list):
        for w in wf_list:
            validate_file(w, workflow=True)
            workflows.append({"name": w["name"], "path": w["path"], "sha": w["sha"], "blob_sha": w["sha"]})
    elif s_wf == 404:
        workflows = []
    elif s_wf in (401, 403):
        raise PermissionError(f"Insufficient permissions to read workflows on {base_sha}: {s_wf}")
    else:
        raise RuntimeError(f"Failed to read workflows on {base_sha}: {s_wf}")
    workflows.sort(key=lambda item: item["path"])

    # 4. Explicit GET for required source paths at baseSHA
    required_sources: list[dict[str, Any]] = []
    for src_path in inputs["required_sources"]:
        s_src, src_data = get_fn(f"/repos/{repo}/contents/{src_path}?ref={base_sha}")
        if s_src == 200 and isinstance(src_data, dict):
            validate_file(src_data, expected_path=src_path)
            required_sources.append({
                "path": src_path,
                "sha": src_data.get("sha"),
                "size": src_data.get("size"),
            })
        elif s_src in (401, 403):
            raise PermissionError(f"Insufficient permissions to read required source '{src_path}': {s_src}")
        elif s_src == 404:
            raise RuntimeError(f"Required policy source '{src_path}' not found at base SHA {base_sha} (404)")
        else:
            raise RuntimeError(f"Failed to fetch required policy source '{src_path}' (status {s_src})")
    required_sources.sort(key=lambda item: item["path"])

    selected_wf = next((s for s in required_sources if s["path"] == inputs["selected_workflow"]), None)
    runner_revisions = inputs["runner"]

    enforcement_contents = {
        "base_sha": base_sha,
        "classic_protection": classic_protection,
        "repo": repo,
        "required_sources": required_sources,
        "rulesets": rulesets,
        "runner": runner_revisions,
        "runner_revisions": runner_revisions,
        "selected_workflow": selected_wf,
        "workflows": workflows,
    }
    digest = compute_policy_digest(enforcement_contents)

    return {
        "schema_version": "1.0",
        "repo": repo,
        "base_sha": base_sha,
        "base_ref": base_ref,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "rulesets": rulesets,
        "classic_protection": classic_protection,
        "workflows": workflows,
        "required_sources": required_sources,
        "selected_workflow": selected_wf,
        "runner": runner_revisions,
        "runner_revisions": runner_revisions,
        "enforcement_contents": enforcement_contents,
        "enforcement_digest": digest,
    }


def load_policy_snapshot(path: Path | str) -> dict[str, Any]:
    """Load policy snapshot from file and verify complete schema and digest integrity."""
    p = Path(path).resolve()
    if not p.exists():
        raise FileNotFoundError(f"Policy snapshot file not found: {p}")
    raw = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"Policy snapshot at '{p}' must be a JSON dictionary")

    for k in MANDATORY_SNAPSHOT_KEYS:
        if k not in raw:
            raise ValueError(f"Missing required field '{k}' in policy snapshot")

    enforcement = raw["enforcement_contents"]
    if not isinstance(enforcement, dict):
        raise ValueError("Policy snapshot 'enforcement_contents' must be a dictionary")
    if enforcement.get("repo") != raw["repo"]:
        raise ValueError(f"Policy snapshot repo mismatch: {enforcement.get('repo')} != {raw['repo']}")
    if enforcement.get("base_sha") != raw["base_sha"]:
        raise ValueError(f"Policy snapshot base_sha mismatch: {enforcement.get('base_sha')} != {raw['base_sha']}")

    computed = compute_policy_digest(enforcement)
    if computed != raw.get("enforcement_digest"):
        raise ValueError(f"Policy snapshot corrupted: digest mismatch ({computed} != {raw.get('enforcement_digest')})")
    return raw


def save_policy_snapshot(snapshot: dict[str, Any], path: Path | str) -> None:
    """Save policy snapshot to path with atomic overwrite."""
    p = Path(path).resolve()
    p.parent.mkdir(parents=True, exist_ok=True)
    temp = p.with_suffix(".tmp")
    temp.write_text(json.dumps(snapshot, indent=2, sort_keys=True), encoding="utf-8")
    temp.replace(p)
