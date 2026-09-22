"""Joint profile resolution from explicit eligibility and declared evidence."""
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Profile:
    id: str
    model: str
    effort: str
    harness: str
    version: str
    route: str
    roles: tuple[str, ...]
    tools: tuple[str, ...]
    context: int
    speed: str = "normal"
    mapping_version: str = "1"
    portable_intent: str | None = None
    in_session_change: bool = False
    provider: str | None = None
    family: str | None = None
    account_ref: str | None = None
    subscription_product: str | None = None
    quota_pool: str | None = None


# Only routes/models actually observed on this host are registered: a profile needs an
# evidenced harness version, model label and context window (an unevidenced model such as
# gemini-3.1-pro-high is deliberately absent rather than declared with a borrowed number). Each entry binds a
# harness binary version, a provider, an account reference and the context window that the
# harness itself reported; a placeholder model or an unobserved route is not a profile.
# Evidence: agy 1.2.3 `agy models` label `gemini-3.8-flash-high` with an observed
# max_context_window of 235849 tokens (M4_EVIDENCE/route-diagnosis-metadata.json), and
# codex-cli 0.144.1 `thread_settings_applied` model=qwen3.8-max effort=high over the
# Alibaba Baba Token Plan endpoint with model_context_window 258400
# (M4_EVIDENCE/qwen-command-map-native-metadata.json).
STANDARD_NATIVE_PROFILES = (
    Profile(
        id="gemini-3.8-flash-high",
        model="gemini-3.8-flash",
        effort="high",
        harness="agy",
        version="1.2.3",
        route="native-antigravity-agy",
        roles=("implementation", "repair", "adjudication"),
        tools=("read", "search", "edit", "shell", "test"),
        context=235849,
        speed="normal",
        provider="google",
        family="gemini",
        account_ref="antigravity-signed-in",
        subscription_product="antigravity-subscription",
        quota_pool="antigravity-subscription",
    ),
    Profile(
        id="qwen3.8-max-high-codex-baba",
        model="qwen3.8-max",
        effort="high",
        harness="codex",
        version="0.144.1",
        route="native-codex-baba",
        roles=("implementation", "repair", "adjudication"),
        tools=("read", "search", "edit", "shell", "test"),
        context=258400,
        speed="normal",
        provider="alibaba",
        family="qwen",
        account_ref="baba-token-plan",
        quota_pool="baba-token-plan",
    ),
)


def resolve(profiles, *, role, routes, tools=(), context=0, model=None,
            effort=None, evidence=None, default=None, deterministic=False,
            demand=None, portable_intent=None):
    """A pinned unsupported dimension is an error, never a clamped setting."""
    if deterministic:
        return {"profile": None, "reason": "deterministic", "inference_calls": 0}
    eligible = [p for p in profiles if p.route in routes and role in p.roles
                and set(tools) <= set(p.tools) and p.context >= context
                and (model is None or p.model == model)
                and (effort is None or p.effort == effort)
                and (portable_intent is None or p.portable_intent == portable_intent)]
    if not eligible:
        raise PermissionError("no supported authorized profile for requested dimensions")
    ranked = [p for p in eligible if p.id in (evidence or {})]
    if ranked:
        chosen = max(ranked, key=lambda p: (evidence[p.id], p.id))
        reason, confidence = "declared task evidence", "fixture-or-observed-as-supplied"
    else:
        chosen = next((p for p in eligible if p.id == default), None)
        if chosen is None:
            if len(eligible) != 1:
                raise ValueError("declare an eligible default when evidence is sparse")
            chosen = eligible[0]
        reason, confidence = "declared eligible default", "low"
    return {"requested": {"model": model, "effort": effort},
            "profile": asdict(chosen), "observed": None, "reason": reason,
            "confidence": confidence, "inference_calls": 0, "demand": demand,
            "portable_intent": portable_intent}


def observe(selection, actual):
    """Unknown observation is telemetry; a known route mismatch is authorization."""
    resolved = selection["profile"]
    if actual.get("route") not in (None, resolved["route"]):
        raise PermissionError("observed execution route is unauthorized")
    for key in ("provider", "account_ref", "subscription_product", "quota_pool"):
        if actual.get(key) is not None and actual[key] != resolved.get(key):
            raise PermissionError("observed billing/provider identity is not authorized")
    return {**selection, "observed": dict(actual), "mismatches": [
        key for key in ("model", "effort", "speed", "harness", "version")
        if actual.get(key) is not None and actual[key] != resolved.get(key)]}
