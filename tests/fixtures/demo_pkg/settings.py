"""Application settings derived from the YAML config."""

from __future__ import annotations

from .loaders import load_app_config


def get_threshold() -> float:
    cfg = load_app_config()
    return cfg.get("threshold", 0.5)


def get_mode() -> str:
    cfg = load_app_config()
    return cfg["mode"]


def get_secret() -> str:
    # ``load_secrets`` is deliberately not declared in corral.yaml, so the
    # config extractor must not emit any reads_config edges for it.
    cfg = load_secrets()  # noqa: F821
    return cfg.get("token")
