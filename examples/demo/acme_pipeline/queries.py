"""SQL text owned by the synthetic order pipeline."""

from __future__ import annotations


def rebuild_curated_orders(minimum: int) -> str:
    """Return SQL that refreshes a derived table from raw orders."""
    return (
        "INSERT INTO curated_orders "
        "SELECT order_id, customer_id, total FROM raw_orders "
        f"WHERE total >= {minimum}"
    )


def active_customer_query() -> str:
    return "SELECT DISTINCT customer_id FROM curated_orders"
