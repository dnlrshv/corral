"""Cross-module transformation orchestration."""

from __future__ import annotations

from .queries import active_customer_query, rebuild_curated_orders
from .settings import minimum_total


def build_statements() -> list[str]:
    threshold = minimum_total()
    return [rebuild_curated_orders(threshold), active_customer_query()]
