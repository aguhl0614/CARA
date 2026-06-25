"""BigCommerce orders connector (read-only).

Pulls orders incrementally from the BigCommerce **v2** Orders API (orders live under
v2, not v3) and normalizes them to the cache's Order contract. Cursor = the max
`date_modified` seen so far, so each run only fetches orders changed since last time
(catches post-creation edits, req #2).

creds: {"store_hash": "...", "access_token": "..."}  — an API account with the
Orders (read-only) scope.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import httpx

_TIMEOUT = httpx.Timeout(30.0)
_PAGE_SIZE = 250


def _base_url(store_hash: str) -> str:
    return f"https://api.bigcommerce.com/stores/{store_hash}/v2"


def _headers(token: str) -> dict:
    return {
        "X-Auth-Token": token,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _parse_dt(value):
    """BigCommerce v2 returns RFC-2822, e.g. 'Tue, 23 Jun 2026 14:00:00 +0000'."""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%a, %d %b %Y %H:%M:%S %z")
    except (ValueError, TypeError):
        pass
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _customer_name(order: dict) -> str | None:
    ba = order.get("billing_address") or {}
    company = (ba.get("company") or "").strip()
    if company:
        return company
    name = " ".join(p for p in [ba.get("first_name"), ba.get("last_name")] if p).strip()
    return name or None


def _to_float(value):
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _min_order_id(creds: dict):
    v = creds.get("min_order_id")
    try:
        return int(str(v)) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _order_id_int(order: dict):
    try:
        return int(order.get("id"))
    except (TypeError, ValueError):
        return None


def _normalize(order: dict) -> dict:
    oid = str(order.get("id"))
    return {
        "source": "bigcommerce",
        "external_id": oid,
        "number": oid,  # the BigCommerce order id is the order number
        "customer": _customer_name(order),
        "status": order.get("status"),  # e.g. "Awaiting Fulfillment"
        "total": _to_float(order.get("total_inc_tax")),
        "currency": order.get("currency_code"),
        "order_date": _parse_dt(order.get("date_created")),
        "source_updated_at": _parse_dt(order.get("date_modified")),
        "raw": {
            **{
                k: order.get(k)
                for k in (
                    "id", "status", "status_id", "date_created", "date_modified",
                    "total_inc_tax", "currency_code", "items_total", "payment_status",
                )
            },
            "billing": {
                "name": _customer_name(order),
                "company": (order.get("billing_address") or {}).get("company"),
                "email": (order.get("billing_address") or {}).get("email"),
                "phone": (order.get("billing_address") or {}).get("phone"),
            },
            "customer_message": order.get("customer_message"),
            "line_items": [],  # filled in by sync() via the order's products endpoint
        },
    }


def _get_with_retry(client: httpx.Client, url: str, params: dict) -> httpx.Response:
    for _ in range(5):
        resp = client.get(url, params=params)
        if resp.status_code == 429:
            reset_ms = int(resp.headers.get("X-Rate-Limit-Time-Reset-Ms", "30000"))
            time.sleep(min(reset_ms / 1000.0, 60))
            continue
        return resp
    return resp


def _fetch_products(client: httpx.Client, base: str, order_id) -> list[dict]:
    """BigCommerce v2 keeps line items on a separate endpoint: /orders/{id}/products.
    Includes each line's product options (customizations — engraving text, sizes, etc.) so
    order details are complete."""
    if not order_id:
        return []
    resp = _get_with_retry(client, f"{base}/orders/{order_id}/products", {"limit": 250})
    if resp.status_code == 204:
        return []
    resp.raise_for_status()
    items = []
    for p in resp.json() or []:
        options = [
            {"name": o.get("display_name"), "value": o.get("display_value")}
            for o in (p.get("product_options") or [])
        ]
        items.append(
            {
                "item": p.get("name"),
                "sku": p.get("sku"),
                "product_id": p.get("product_id"),
                "qty": p.get("quantity"),
                "unit_price": _to_float(p.get("price_inc_tax")) or _to_float(p.get("base_price")),
                "amount": _to_float(p.get("total_inc_tax")),
                "options": options,
            }
        )
    return items


def sync(creds: dict, cursor: str | None) -> dict:
    store_hash = creds.get("store_hash")
    token = creds.get("access_token")
    if not store_hash or not token:
        raise ValueError("BigCommerce requires store_hash and access_token")

    base = _base_url(store_hash)
    url = f"{base}/orders"
    min_id = _min_order_id(creds)
    base_params = {"sort": "date_modified:asc", "limit": _PAGE_SIZE}
    if cursor:
        base_params["min_date_modified"] = cursor
    if min_id is not None:
        base_params["min_id"] = min_id  # BigCommerce v2: only orders with id >= min_id

    orders: list[dict] = []
    max_modified = cursor
    page = 1
    with httpx.Client(timeout=_TIMEOUT, headers=_headers(token)) as client:
        while True:
            resp = _get_with_retry(client, url, dict(base_params, page=page))
            if resp.status_code == 204:
                break  # v2 returns 204 when there are no (more) orders
            resp.raise_for_status()
            batch = resp.json() or []
            for o in batch:
                if min_id is not None and (_order_id_int(o) or 0) < min_id:
                    continue
                rec = _normalize(o)
                rec["raw"]["line_items"] = _fetch_products(client, base, o.get("id"))
                orders.append(rec)
                dt = rec.get("source_updated_at")
                if dt is not None:
                    iso = dt.astimezone(timezone.utc).isoformat()
                    if max_modified is None or iso > max_modified:
                        max_modified = iso
            if len(batch) < _PAGE_SIZE:
                break
            page += 1

    return {"orders": orders, "cursor": max_modified}
