"""Read-only live inventory access to CORE's PostgreSQL database."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from .config import get_settings


class CoreInventoryError(RuntimeError):
    """Raised when CARA cannot read CORE inventory."""


class CoreInventoryNotConfigured(CoreInventoryError):
    """Raised when CARA_CORE_DATABASE_URL is not set."""


@contextmanager
def _connect() -> Iterator[Any]:
    database_url = get_settings().core_database_url.strip()
    if not database_url:
        raise CoreInventoryNotConfigured("CARA_CORE_DATABASE_URL is not configured.")

    try:
        import psycopg
        from psycopg.rows import dict_row

        with psycopg.connect(database_url, row_factory=dict_row, autocommit=True) as conn:
            conn.execute("SET default_transaction_read_only = on")
            yield conn
    except CoreInventoryError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise CoreInventoryError(f"Could not read CORE inventory: {exc}") from exc


def inventory_status() -> dict[str, Any]:
    """Connection summary for the admin dashboard."""
    if not get_settings().core_database_url.strip():
        return {
            "configured": False,
            "connected": False,
            "item_count": None,
            "error": "CARA_CORE_DATABASE_URL is not configured.",
        }
    try:
        with _connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS count FROM items WHERE is_active = 1").fetchone()
        return {
            "configured": True,
            "connected": True,
            "item_count": int(row["count"] if row else 0),
            "error": "",
        }
    except CoreInventoryError as exc:
        return {
            "configured": True,
            "connected": False,
            "item_count": None,
            "error": str(exc),
        }


def search_inventory(item: str, limit: int = 10) -> list[dict[str, Any]]:
    """Search CORE inventory by SKU, name, location, or barcode."""
    query = (item or "").strip()
    if not query:
        return []
    limit = max(1, min(int(limit or 10), 50))
    like = f"%{query}%"
    prefix = f"{query}%"

    with _connect() as conn:
        rows = conn.execute(
            """
            WITH layer_totals AS (
                SELECT item_id, SUM(remaining_quantity) AS quantity
                FROM inventory_layers
                GROUP BY item_id
            ),
            primary_barcodes AS (
                SELECT item_id, MIN(barcode) AS primary_barcode
                FROM item_barcodes
                WHERE is_primary = 1
                GROUP BY item_id
            )
            SELECT
                i.id AS item_id,
                i.site,
                NULLIF(i.sku, '') AS sku,
                i.name,
                COALESCE(layer_totals.quantity, 0) AS quantity,
                COALESCE(
                    NULLIF(i.raw_data->>'Unit', ''),
                    NULLIF(i.raw_data->>'UOM', ''),
                    NULLIF(i.raw_data->>'Unit of Measure', '')
                ) AS unit,
                CONCAT_WS(' / ', NULLIF(i.loc_1, ''), NULLIF(i.loc_2, ''), NULLIF(i.loc_3, '')) AS location,
                i.updated_at,
                primary_barcodes.primary_barcode
            FROM items i
            LEFT JOIN layer_totals ON layer_totals.item_id = i.id
            LEFT JOIN primary_barcodes ON primary_barcodes.item_id = i.id
            WHERE i.is_active = 1
              AND (
                i.sku ILIKE %(like)s
                OR i.name ILIKE %(like)s
                OR i.loc_1 ILIKE %(like)s
                OR i.loc_2 ILIKE %(like)s
                OR i.loc_3 ILIKE %(like)s
                OR EXISTS (
                    SELECT 1
                    FROM item_barcodes b
                    WHERE b.item_id = i.id AND b.barcode ILIKE %(like)s
                )
              )
            ORDER BY
                CASE
                    WHEN i.sku ILIKE %(query)s THEN 0
                    WHEN EXISTS (
                        SELECT 1
                        FROM item_barcodes b
                        WHERE b.item_id = i.id AND b.barcode ILIKE %(query)s
                    ) THEN 1
                    WHEN i.sku ILIKE %(prefix)s THEN 2
                    WHEN i.name ILIKE %(prefix)s THEN 3
                    WHEN i.sku ILIKE %(like)s THEN 4
                    WHEN i.name ILIKE %(like)s THEN 5
                    ELSE 6
                END,
                i.sku,
                i.name
            LIMIT %(limit)s
            """,
            {"query": query, "like": like, "prefix": prefix, "limit": limit},
        ).fetchall()

    return [_format_row(dict(row)) for row in rows]


def _format_row(row: dict[str, Any]) -> dict[str, Any]:
    quantity = row.get("quantity")
    return {
        "item_id": row.get("item_id"),
        "site": row.get("site"),
        "sku": row.get("sku"),
        "name": row.get("name"),
        "quantity": float(quantity) if quantity is not None else 0.0,
        "unit": row.get("unit"),
        "location": row.get("location") or None,
        "updated_at": row.get("updated_at"),
    }
