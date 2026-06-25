"""Read-only tools exposed to Open WebUI as an OpenAPI tool server.

Open WebUI fetches this app's /openapi.json and lets the model call these endpoints.
Each route has a stable operation_id (the tool name the model sees) and a clear summary.
"""

from __future__ import annotations

import hmac
import re
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Header, HTTPException, Query
from sqlalchemy import and_, or_
from sqlmodel import select

from ..config import get_settings
from ..connectors import quickbooks
from ..db import get_session
from ..kv import get_timezone_name, get_zoneinfo
from ..models import InventoryItem, Machine, MondayJob, Order
from ..rag.store import search_documents
from ..security import load_credentials, print_token

router = APIRouter(tags=["cara-tools"])
_settings = get_settings()


def require_tools_token(authorization: str | None = Header(default=None)):
    """If a tools token is configured, require it as a Bearer header (set the same value in Open
    WebUI's tool-server auth). Empty token = open (back-compat)."""
    token = _settings.tools_token
    if not token:
        return
    if not (authorization and hmac.compare_digest(authorization.strip(), f"Bearer {token}")):
        raise HTTPException(status_code=401, detail="Unauthorized")


def _today():
    return datetime.now(get_zoneinfo()).date()


def _parse_date(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except (ValueError, TypeError):
        return None


def _order_summary(o: Order) -> dict:
    return {
        "source": o.source,
        "number": o.number,
        "customer": o.customer,
        "status": o.status,
        "pipeline_status": o.monday_status,
        "total": o.total,
        "currency": o.currency,
        "order_date": o.order_date,
        "updated_at": o.updated_at,
    }


def _job_summary(j: MondayJob) -> dict:
    return {
        "monday_item_id": j.monday_item_id,
        "job_number": j.job_number,           # QuickBooks estimate #
        "bigcommerce_number": j.bc_number,
        "sales_receipt_number": j.sr_number,
        "title": j.title,                     # customer / description
        "headline_status": j.main_status,
        "stages": j.statuses,                 # all status columns (Art, Proof, Production, …)
        "amount": j.amount,
        "due_date": j.due_date,    # should be done by
        "hard_date": j.hard_date,  # must be done by (firm deadline)
        "owner": j.person,
        "quick_info": j.quick_info,
        "updated_at": j.updated_at,
    }


@router.get(
    "/machines",
    operation_id="list_machines",
    summary="List the machines and software CARA has documentation for",
)
def list_machines():
    with get_session() as s:
        return [
            {"id": m.id, "name": m.name, "kind": m.kind, "aliases": m.aliases}
            for m in s.exec(select(Machine)).all()
        ]


@router.get(
    "/documentation",
    operation_id="search_documentation",
    summary="Search machine/software manuals and workflow docs, optionally scoped to one machine",
)
def search_documentation(
    query: str = Query(..., description="What to look up in the manuals"),
    machine: Optional[str] = Query(None, description="Machine or software name to scope the search to"),
    limit: int = Query(5, ge=1, le=20),
):
    return search_documents(query=query, machine=machine, limit=limit)


@router.get(
    "/orders/search",
    operation_id="search_orders",
    summary="Find orders (QuickBooks/BigCommerce) and Monday production jobs by number, customer, or status",
)
def search_orders(
    query: Optional[str] = Query(None, description="Order/job number or customer name"),
    status: Optional[str] = Query(None, description="A status to filter by"),
    limit: int = Query(10, ge=1, le=50),
):
    with get_session() as s:
        ostmt = select(Order)
        jstmt = select(MondayJob)
        if query:
            like = f"%{query}%"
            ostmt = ostmt.where(
                or_(Order.number.like(like), Order.customer.like(like), Order.external_id.like(like))
            )
            jstmt = jstmt.where(
                or_(
                    MondayJob.job_number.like(like),
                    MondayJob.bc_number.like(like),
                    MondayJob.sr_number.like(like),
                    MondayJob.title.like(like),
                )
            )
        if status:
            ostmt = ostmt.where(or_(Order.status == status, Order.monday_status == status))
            jstmt = jstmt.where(MondayJob.main_status == status)
        orders = [_order_summary(o) for o in s.exec(ostmt.limit(limit)).all()]
        jobs = [_job_summary(j) for j in s.exec(jstmt.limit(limit)).all()]
    return {"orders": orders, "jobs": jobs}


@router.get(
    "/today",
    operation_id="get_current_date",
    summary="Get today's date and the current week window. Call this for any 'today / this week / overdue / due soon' question — never assume the date.",
)
def get_current_date():
    today = _today()
    return {
        "today": today.isoformat(),
        "weekday": today.strftime("%A"),
        "timezone": get_timezone_name(),
        "week_through": (today + timedelta(days=7)).isoformat(),
    }


@router.get(
    "/orders/due",
    operation_id="orders_due",
    summary="List jobs due within a date window (default: today through the next 7 days), matching on EITHER the Due Date (should be done by) or the Hard Date (must be done by). Use for 'due this week / due today / overdue' questions.",
)
def orders_due(
    within_days: int = Query(7, ge=0, le=120, description="Days ahead of start_date when end_date is omitted"),
    start_date: Optional[str] = Query(None, description="YYYY-MM-DD; defaults to today"),
    end_date: Optional[str] = Query(None, description="YYYY-MM-DD; defaults to start_date + within_days"),
):
    start = _parse_date(start_date) or _today()
    end = _parse_date(end_date) or (start + timedelta(days=within_days))
    lo, hi = start.isoformat(), (end + timedelta(days=1)).isoformat()  # ISO strings sort correctly
    in_due = and_(MondayJob.due_date >= lo, MondayJob.due_date < hi)
    in_hard = and_(MondayJob.hard_date >= lo, MondayJob.hard_date < hi)
    with get_session() as s:
        rows = s.exec(select(MondayJob).where(or_(in_due, in_hard))).all()

        def _eff(j):  # sort by the earliest known deadline (hard date preferred)
            ds = [d for d in (j.hard_date, j.due_date) if d]
            return min(ds) if ds else "9999-99-99"

        jobs = [_job_summary(j) for j in sorted(rows, key=_eff)]
    return {
        "today": _today().isoformat(),
        "range": {"from": start.isoformat(), "to": end.isoformat()},
        "count": len(jobs),
        "jobs": jobs,
    }


def _resolve_order(s, number: str):
    """Resolve a typed number to (raw_input, MondayJob|None, Order|None)."""
    raw = (number or "").strip()
    digits = re.sub(r"^(bc|sr)\s*#?\s*", "", raw, flags=re.I).strip()
    job = (
        s.exec(select(MondayJob).where(MondayJob.job_number == digits)).first()
        or s.exec(select(MondayJob).where(MondayJob.bc_number == digits)).first()
        or s.exec(select(MondayJob).where(MondayJob.sr_number == digits)).first()
    )
    order = s.exec(
        select(Order).where(
            or_(
                Order.number == digits,                     # human order/estimate number
                Order.external_id == digits,                # BigCommerce order id
                Order.external_id == f"estimate:{digits}",  # QBO internal estimate Id (defensive)
            )
        )
    ).first()
    if not order and job and job.bc_number:
        order = s.exec(
            select(Order).where(Order.source == "bigcommerce", Order.number == job.bc_number)
        ).first()
    return raw, job, order


def _resolve_order_live(s, number: str):
    """Like _resolve_order, but on a QBO cache-miss does a live QuickBooks lookup by estimate
    number (covers estimates below the backfill cutoff). The fetched estimate is transient."""
    raw, job, order = _resolve_order(s, number)
    if order is not None:
        return raw, job, order
    digits = re.sub(r"^(bc|sr)\s*#?\s*", "", raw, flags=re.I).strip()
    doc_number = job.job_number if (job and job.job_number) else digits
    if not (doc_number and doc_number.isdigit()):
        return raw, job, order
    creds = load_credentials("quickbooks") or {}
    if creds:
        try:
            rec = quickbooks.fetch_estimate_order(creds, doc_number)
        except Exception:  # noqa: BLE001
            rec = None
        if rec:
            order = Order(**rec)  # transient (not persisted) — reflects current QBO data
    return raw, job, order


@router.get(
    "/orders/status",
    operation_id="get_order_status",
    summary=(
        "Get the Monday.com production status for an order/job by number (a QuickBooks estimate #, "
        "a BigCommerce BC####, or a sales receipt SR#####): its stages, dates, owner, and quick "
        "info. For the full order with line items, use get_order_details."
    ),
)
def get_order_status(
    number: str = Query(..., description="Order/job number, e.g. 16286, BC959, or SR10234"),
):
    with get_session() as s:
        raw, job, _order = _resolve_order(s, number)
        if not job:
            return {"found": False, "number": raw, "note": "No Monday job found for that number."}
        return {"found": True, "query": raw, "job": _job_summary(job)}


@router.get(
    "/orders/pdf",
    operation_id="get_order_pdf",
    summary=(
        "Get a printable PDF link for an order by number: the QuickBooks estimate PDF for a QBO order, "
        "or a generated order PDF for a BigCommerce (BC####) order. Give the returned link to the user "
        "and tell them to open it and print."
    ),
)
def get_order_pdf(
    number: str = Query(..., description="Order number, e.g. 22736 or BC4106"),
):
    with get_session() as s:
        raw, _job, order = _resolve_order_live(s, number)
        if not order:
            return {"found": False, "number": raw, "note": "No QuickBooks/BigCommerce order found to print."}
        source = order.source
        num = order.number
    base = _settings.public_base_url.rstrip("/")
    return {
        "found": True,
        "number": num,
        "source": source,
        "pdf_url": f"{base}/print/order?number={num}&token={print_token(num)}",
    }


@router.get(
    "/orders/payment",
    operation_id="get_payment_status",
    summary=(
        "Check whether an order's invoice has been PAID. For a QuickBooks order it finds the "
        "estimate's linked invoice and its outstanding balance (paid = $0 balance) and returns the "
        "invoice number + due date; if the order hasn't been invoiced yet it says so. For a "
        "BigCommerce order it returns the store payment status."
    ),
)
def get_payment_status(number: str = Query(..., description="Order number, e.g. 22736 or BC4106")):
    with get_session() as s:
        raw, job, order = _resolve_order(s, number)
        if order is not None and order.source == "bigcommerce":
            ps = (order.raw or {}).get("payment_status")
            return {
                "found": True, "number": order.number, "source": "bigcommerce",
                "payment_status": ps, "paid": str(ps).strip().lower() in ("captured", "paid"),
            }
        digits = re.sub(r"^(bc|sr)\s*#?\s*", "", raw, flags=re.I).strip()
        doc_number = job.job_number if (job and job.job_number) else digits

    if not (doc_number and doc_number.isdigit()):
        return {"found": False, "number": raw, "note": "Provide a QuickBooks estimate number to check payment."}
    creds = load_credentials("quickbooks") or {}
    if not creds:
        return {"found": False, "number": raw, "note": "QuickBooks isn't configured."}
    try:
        info = quickbooks.fetch_payment_status(creds, doc_number)
    except Exception as e:  # noqa: BLE001
        return {"found": False, "number": doc_number, "error": str(e)[:200]}
    if info is None:
        return {"found": False, "number": doc_number, "note": "No QuickBooks estimate found for that number."}
    return {"found": True, "number": doc_number, "source": "quickbooks", **info}


@router.get(
    "/orders/details",
    operation_id="get_order_details",
    summary=(
        "Get FULL details for an order by number (a QuickBooks estimate #, a BigCommerce BC####, "
        "or a sales receipt SR#####): the QuickBooks/BigCommerce order with ALL line items (incl. "
        "BigCommerce options and billing/customer) and totals, PLUS the Monday production status."
    ),
)
def get_order_details(
    number: str = Query(..., description="Order/job number, e.g. 16286, BC959, or SR10234"),
):
    with get_session() as s:
        raw, job, order = _resolve_order_live(s, number)
        if not job and not order:
            return {"found": False, "number": raw}
        order_obj = _order_summary(order) if order else None
        if order_obj is not None:
            order_obj["line_items"] = (order.raw or {}).get("line_items", [])
            order_obj["billing"] = (order.raw or {}).get("billing")
        return {
            "found": True,
            "query": raw,
            "job": _job_summary(job) if job else None,
            "order": order_obj,
        }


@router.get(
    "/inventory",
    operation_id="check_inventory",
    summary="Check current inventory quantity for an item or SKU",
)
def check_inventory(
    item: str = Query(..., description="Item name or SKU"),
    limit: int = Query(10, ge=1, le=50),
):
    with get_session() as s:
        like = f"%{item}%"
        rows = s.exec(
            select(InventoryItem)
            .where(or_(InventoryItem.name.like(like), InventoryItem.sku.like(like)))
            .limit(limit)
        ).all()
        return [
            {
                "sku": r.sku,
                "name": r.name,
                "quantity": r.quantity,
                "unit": r.unit,
                "location": r.location,
                "updated_at": r.updated_at,
            }
            for r in rows
        ]
