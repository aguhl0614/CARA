"""QuickBooks Online connector (read-only).

Pulls QuickBooks transactions (Estimates by default; set creds["entities"] to a comma list
to also include Invoices / Sales Receipts) and normalizes them to the Order contract
(source='quickbooks'). An optional creds["min_doc_number"] limits the estimate backfill to
that estimate number and up (translated to a TxnDate cutoff for an efficient server-side
filter, then enforced exactly client-side). DocNumber is the human order number, which links
to Monday jobs (an estimate's DocNumber = the job's leading number).

Sync strategy:
- **Initial backfill** (no cursor, or cursor older than CDC's 30-day window): paginated
  `SELECT ... ORDERBY Metadata.LastUpdatedTime` per entity.
- **Incremental** (cursor within 30 days): the **Change Data Capture (CDC)** operation —
  one call returns everything changed across all entities since the cursor.

Auth handling:
- Token endpoint resolved from Intuit's OpenID **discovery document** (per environment),
  with a documented-endpoint fallback.
- Access token refreshed each run and on a mid-run 401; rotated refresh tokens are PERSISTED.
- Transient errors (429 / 5xx / network) retried with exponential backoff; hard auth errors
  (invalid_grant / expired refresh token) propagate so the admin is prompted to reconnect.

creds: {client_id, client_secret, refresh_token, realm_id, environment}
Note: QBO query language uses 'Metadata.LastUpdatedTime'; the JSON response uses 'MetaData'.
"""

from __future__ import annotations

import base64
import logging
import time
from datetime import datetime, timedelta, timezone

import httpx

log = logging.getLogger("cara.quickbooks")

_DISCOVERY = {
    "production": "https://developer.api.intuit.com/.well-known/openid_configuration",
    "sandbox": "https://developer.api.intuit.com/.well-known/openid_sandbox_configuration",
}
_TOKEN_FALLBACK = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
_PROD = "https://quickbooks.api.intuit.com"
_SANDBOX = "https://sandbox-quickbooks.api.intuit.com"
_MINOR = "73"
_PAGE = 100
_ENTITIES = ("Estimate",)  # default; override per connection via creds["entities"]
_TIMEOUT = httpx.Timeout(40.0)
_RETRY_STATUS = {429, 500, 502, 503, 504}
_CDC_WINDOW = timedelta(days=29)  # CDC changedSince must be < 30 days old

_discovery_cache: dict[str, str] = {}


def _env(creds: dict) -> str:
    return "sandbox" if (creds.get("environment") or "production").lower().startswith("sand") else "production"


def _api_base(creds: dict) -> str:
    return _SANDBOX if _env(creds) == "sandbox" else _PROD


def _token_endpoint(creds: dict) -> str:
    """Resolve the token endpoint from Intuit's discovery document (cached per env)."""
    env = _env(creds)
    if env in _discovery_cache:
        return _discovery_cache[env]
    endpoint = _TOKEN_FALLBACK
    try:
        r = httpx.get(_DISCOVERY[env], timeout=_TIMEOUT)
        r.raise_for_status()
        endpoint = r.json().get("token_endpoint") or _TOKEN_FALLBACK
    except (httpx.HTTPError, ValueError):
        endpoint = _TOKEN_FALLBACK
    _discovery_cache[env] = endpoint
    return endpoint


def _backoff(attempt: int) -> None:
    time.sleep(min(2 ** attempt, 30))


def _refresh_access_token(creds: dict) -> str:
    auth = base64.b64encode(f"{creds['client_id']}:{creds['client_secret']}".encode()).decode()
    endpoint = _token_endpoint(creds)
    headers = {
        "Authorization": f"Basic {auth}",
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    data = {"grant_type": "refresh_token", "refresh_token": creds["refresh_token"]}

    last_exc = None
    for attempt in range(4):
        try:
            resp = httpx.post(endpoint, headers=headers, data=data, timeout=_TIMEOUT)
        except httpx.RequestError as e:
            last_exc = e
            _backoff(attempt)
            continue
        tid = resp.headers.get("intuit_tid")
        if resp.is_error:
            log.warning("QBO token refresh -> HTTP %s intuit_tid=%s body=%s",
                        resp.status_code, tid, resp.text[:300])
        if resp.status_code in _RETRY_STATUS:
            last_exc = httpx.HTTPStatusError("retryable", request=resp.request, response=resp)
            _backoff(attempt)
            continue
        resp.raise_for_status()  # 400/401 (invalid_grant / expired refresh token) -> propagate
        tok = resp.json()
        new_refresh = tok.get("refresh_token")
        if new_refresh and new_refresh != creds.get("refresh_token"):
            from ..security import save_credentials

            save_credentials("quickbooks", {**creds, "refresh_token": new_refresh})
        return tok["access_token"]
    raise last_exc or ValueError("QuickBooks token refresh failed")


def _request(client: httpx.Client, base: str, realm: str, path: str, params: dict, creds: dict, state: dict) -> dict:
    """GET a QBO endpoint with retry/backoff and one mid-run re-auth on 401."""
    last_exc = None
    for attempt in range(5):
        try:
            resp = client.get(
                f"{base}/v3/company/{realm}/{path}",
                params={**params, "minorversion": _MINOR},
                headers={"Authorization": f"Bearer {state['token']}", "Accept": "application/json"},
            )
        except httpx.RequestError as e:
            last_exc = e
            _backoff(attempt)
            continue
        tid = resp.headers.get("intuit_tid")
        if resp.is_error:
            log.warning("QBO %s -> HTTP %s intuit_tid=%s body=%s",
                        path, resp.status_code, tid, resp.text[:500])
        if resp.status_code == 401:
            state["token"] = _refresh_access_token(creds)
            continue
        if resp.status_code in _RETRY_STATUS:
            _backoff(attempt)
            continue
        resp.raise_for_status()
        return resp.json()
    raise last_exc or ValueError("QuickBooks request failed after retries")


def fetch_estimate_pdf(creds: dict, estimate_id: str) -> bytes:
    """Fetch the native QuickBooks Estimate PDF (bytes) for a QBO entity Id."""
    state = {"token": _refresh_access_token(creds)}
    base = _api_base(creds)
    realm = creds["realm_id"]
    url = f"{base}/v3/company/{realm}/estimate/{estimate_id}/pdf"
    last_exc = None
    for attempt in range(4):
        try:
            resp = httpx.get(
                url,
                params={"minorversion": _MINOR},
                headers={"Authorization": f"Bearer {state['token']}", "Accept": "application/pdf"},
                timeout=_TIMEOUT,
            )
        except httpx.RequestError as e:
            last_exc = e
            _backoff(attempt)
            continue
        if resp.status_code == 401:
            state["token"] = _refresh_access_token(creds)
            continue
        if resp.status_code in _RETRY_STATUS:
            _backoff(attempt)
            continue
        if resp.is_error:
            log.warning("QBO estimate %s pdf -> HTTP %s intuit_tid=%s body=%s",
                        estimate_id, resp.status_code, resp.headers.get("intuit_tid"), resp.text[:300])
        resp.raise_for_status()
        return resp.content
    raise last_exc or ValueError("QuickBooks estimate PDF fetch failed")


def fetch_estimate_order(creds: dict, doc_number: str) -> dict | None:
    """Live-fetch one Estimate by DocNumber, normalized to the Order contract. Used for
    cache-misses (e.g. estimates below the backfill cutoff)."""
    state = {"token": _refresh_access_token(creds)}
    base = _api_base(creds)
    realm = creds["realm_id"]
    stmt = f"SELECT * FROM Estimate WHERE DocNumber = '{doc_number}'"
    with httpx.Client(timeout=_TIMEOUT) as client:
        data = _request(client, base, realm, "query", {"query": stmt}, creds, state)
    rows = (data.get("QueryResponse", {}) or {}).get("Estimate") or []
    return _normalize("Estimate", rows[0]) if rows else None


def fetch_payment_status(creds: dict, doc_number: str) -> dict | None:
    """Has the order's invoice been paid? Finds the estimate's linked invoice (via QBO
    LinkedTxn — invoice numbering is separate from estimates) and reports its balance.
    paid = balance is 0. Returns None if no such estimate."""
    state = {"token": _refresh_access_token(creds)}
    base = _api_base(creds)
    realm = creds["realm_id"]
    with httpx.Client(timeout=_TIMEOUT) as client:
        est = (
            _request(client, base, realm, "query",
                     {"query": f"SELECT * FROM Estimate WHERE DocNumber = '{doc_number}'"}, creds, state)
            .get("QueryResponse", {}) or {}
        ).get("Estimate") or []
        if not est:
            return None
        invoice_id = next(
            (lt.get("TxnId") for lt in (est[0].get("LinkedTxn") or []) if lt.get("TxnType") == "Invoice"),
            None,
        )
        if not invoice_id:
            return {"invoiced": False, "paid": False, "estimate_status": est[0].get("TxnStatus")}
        inv = (
            _request(client, base, realm, "query",
                     {"query": f"SELECT * FROM Invoice WHERE Id = '{invoice_id}'"}, creds, state)
            .get("QueryResponse", {}) or {}
        ).get("Invoice") or []
        if not inv:
            return {"invoiced": True, "paid": False, "note": "linked invoice not retrievable"}
        balance = _to_float(inv[0].get("Balance"))
        return {
            "invoiced": True,
            "paid": (balance == 0) if balance is not None else None,
            "balance": balance,
            "total": _to_float(inv[0].get("TotalAmt")),
            "invoice_number": inv[0].get("DocNumber"),
            "invoice_date": inv[0].get("TxnDate"),
            "due_date": inv[0].get("DueDate"),
        }


def _within_cdc_window(cursor: str | None) -> bool:
    dt = _parse_dt(cursor)
    if not dt:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt) < _CDC_WINDOW


def _parse_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _to_float(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _line_items(rec: dict) -> list[dict]:
    """Flatten a QBO transaction's Line array into a readable item breakdown."""
    items = []
    for ln in rec.get("Line") or []:
        if ln.get("DetailType") == "SalesItemLineDetail":
            d = ln.get("SalesItemLineDetail") or {}
            items.append(
                {
                    "item": (d.get("ItemRef") or {}).get("name"),
                    "description": ln.get("Description"),
                    "qty": d.get("Qty"),
                    "unit_price": d.get("UnitPrice"),
                    "amount": ln.get("Amount"),
                }
            )
        elif ln.get("Description"):  # description-only / sub-line
            items.append({"description": ln.get("Description"), "amount": ln.get("Amount")})
    return items


def _normalize(entity: str, rec: dict) -> dict:
    return {
        "source": "quickbooks",
        "external_id": f"{entity.lower()}:{rec.get('Id')}",
        "number": rec.get("DocNumber"),
        "customer": (rec.get("CustomerRef") or {}).get("name"),
        "status": entity,  # Estimate | Invoice | SalesReceipt
        "total": _to_float(rec.get("TotalAmt")),
        "currency": (rec.get("CurrencyRef") or {}).get("value"),
        "order_date": _parse_dt(rec.get("TxnDate")),
        "source_updated_at": _parse_dt((rec.get("MetaData") or {}).get("LastUpdatedTime")),
        "raw": {
            "entity": entity,
            "Id": rec.get("Id"),
            "DocNumber": rec.get("DocNumber"),
            "TxnDate": rec.get("TxnDate"),
            "TotalAmt": rec.get("TotalAmt"),
            "Balance": rec.get("Balance"),
            "TxnStatus": rec.get("TxnStatus"),
            "PrivateNote": rec.get("PrivateNote"),
            "line_items": _line_items(rec),
        },
    }


def _collect(orders: list, rec: dict, entity: str, max_dt):
    o = _normalize(entity, rec)
    orders.append(o)
    dt = o.get("source_updated_at")
    if dt is not None and (max_dt is None or dt > max_dt):
        return dt
    return max_dt


def _entities(creds: dict) -> tuple:
    parsed = tuple(e.strip() for e in (creds.get("entities") or "").split(",") if e.strip())
    return parsed or _ENTITIES


def _min_doc(creds: dict):
    v = creds.get("min_doc_number")
    try:
        return int(str(v)) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _docnum_int(rec: dict):
    try:
        return int(str(rec.get("DocNumber")))
    except (TypeError, ValueError):
        return None


def _keep(entity: str, rec: dict, min_doc) -> bool:
    """Apply the optional minimum-estimate-number cutoff (estimates only)."""
    if entity == "Estimate" and min_doc is not None:
        n = _docnum_int(rec)
        if n is not None and n < min_doc:
            return False
    return True


def _estimate_txndate(client, base, realm, doc_number, creds, state):
    data = _request(client, base, realm, "query",
                    {"query": f"SELECT * FROM Estimate WHERE DocNumber = '{doc_number}'"}, creds, state)
    rows = (data.get("QueryResponse", {}) or {}).get("Estimate") or []
    return rows[0].get("TxnDate") if rows else None


def _incremental_cdc(client, base, realm, cursor, creds, state, orders, max_dt):
    entities = _entities(creds)
    min_doc = _min_doc(creds)
    data = _request(client, base, realm, "cdc",
                    {"entities": ",".join(entities), "changedSince": cursor}, creds, state)
    for block in data.get("CDCResponse") or []:
        for qr in block.get("QueryResponse") or []:
            for entity in entities:
                for rec in qr.get(entity) or []:
                    if str(rec.get("status")).lower() == "deleted":
                        continue  # tombstone — leave the cache as-is
                    if not _keep(entity, rec, min_doc):
                        continue
                    max_dt = _collect(orders, rec, entity, max_dt)
    return max_dt


def _backfill_query(client, base, realm, cursor, creds, state, orders, max_dt):
    entities = _entities(creds)
    min_doc = _min_doc(creds)
    # Translate the min estimate number into a TxnDate cutoff for an efficient server-side
    # filter (QBO compares DocNumber lexically, so a numeric range filter isn't reliable).
    cutoff = _estimate_txndate(client, base, realm, min_doc, creds, state) if min_doc is not None else None
    for entity in entities:
        clauses = []
        if cursor:
            clauses.append(f"Metadata.LastUpdatedTime >= '{cursor}'")
        if cutoff and entity == "Estimate":
            clauses.append(f"TxnDate >= '{cutoff}'")
        where = ("WHERE " + " AND ".join(clauses) + " ") if clauses else ""
        start = 1
        while True:
            stmt = (
                f"SELECT * FROM {entity} {where}"
                f"ORDERBY Metadata.LastUpdatedTime STARTPOSITION {start} MAXRESULTS {_PAGE}"
            )
            data = _request(client, base, realm, "query", {"query": stmt}, creds, state)
            rows = (data.get("QueryResponse", {}) or {}).get(entity) or []
            for rec in rows:
                if not _keep(entity, rec, min_doc):
                    continue
                max_dt = _collect(orders, rec, entity, max_dt)
            if len(rows) < _PAGE:
                break
            start += _PAGE
    return max_dt


def sync(creds: dict, cursor: str | None) -> dict:
    for key in ("client_id", "client_secret", "refresh_token", "realm_id"):
        if not creds.get(key):
            raise ValueError(f"QuickBooks requires {key}")

    state = {"token": _refresh_access_token(creds)}
    base = _api_base(creds)
    realm = creds["realm_id"]
    orders: list[dict] = []
    max_dt = _parse_dt(cursor)

    with httpx.Client(timeout=_TIMEOUT) as client:
        if _within_cdc_window(cursor):
            max_dt = _incremental_cdc(client, base, realm, cursor, creds, state, orders, max_dt)
        else:
            max_dt = _backfill_query(client, base, realm, cursor, creds, state, orders, max_dt)

    return {"orders": orders, "cursor": max_dt.isoformat() if max_dt else cursor}
