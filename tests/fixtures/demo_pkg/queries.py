"""SQL statements used by the demo pipeline."""

ORDERS_QUERY = "SELECT id, total FROM orders WHERE total > 0"


def archive_orders() -> str:
    """Return the statement that moves rows into the archive table."""
    return "INSERT INTO orders_archive SELECT * FROM orders"


def _row_filter() -> str:
    return "total > 0"
