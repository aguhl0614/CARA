"""Sync orchestration: pull only what changed, upsert into the local cache.

Each connector returns a normalized dict:
    {"orders": [..], "monday": [..], "cursor": "<new cursor>"}
Upserts are keyed so post-creation edits (req #2) and frequent Monday status
changes (req #3) overwrite the cached row rather than duplicating it.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlmodel import select

from ..db import get_session
from ..models import MondayJob, Order, SyncState
from ..security import load_credentials

log = logging.getLogger("cara.sync")

# source -> default poll interval (seconds). Monday changes fastest, so polls fastest.
SOURCES: dict[str, int] = {
    "bigcommerce": 600,
    "quickbooks": 600,
    "monday": 180,
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_sync_states() -> None:
    with get_session() as s:
        for source, interval in SOURCES.items():
            if not s.get(SyncState, source):
                s.add(SyncState(source=source, interval_seconds=interval))
        s.commit()


def _get_cursor(source: str):
    with get_session() as s:
        st = s.get(SyncState, source)
        return st.cursor if st else None


def _finish(source, status, cursor=None, count=0, error=None) -> None:
    with get_session() as s:
        st = s.get(SyncState, source) or SyncState(source=source)
        st.last_run_at = _now()
        st.last_status = status
        st.last_error = error
        if cursor is not None:
            st.cursor = cursor
        if status == "ok":
            st.record_count = count
        s.add(st)
        s.commit()


def upsert_orders(records: list[dict]) -> int:
    n = 0
    with get_session() as s:
        for r in records:
            existing = s.exec(
                select(Order).where(
                    Order.source == r["source"], Order.external_id == r["external_id"]
                )
            ).first()
            o = existing or Order(source=r["source"], external_id=r["external_id"])
            for k, v in r.items():
                setattr(o, k, v)
            o.updated_at = _now()
            s.add(o)
            n += 1
        s.commit()
    return n


def upsert_monday_jobs(records: list[dict], replace: bool = False) -> int:
    n = 0
    with get_session() as s:
        seen: set[str] = set()
        for r in records:
            job = s.get(MondayJob, r["monday_item_id"]) or MondayJob(monday_item_id=r["monday_item_id"])
            for k, v in r.items():
                setattr(job, k, v)
            job.updated_at = _now()
            s.add(job)
            _link_job_to_orders(s, job)
            seen.add(r["monday_item_id"])
            n += 1
        if replace:
            # The connector returned the WHOLE synced group, so any cached job not in it
            # has left the group (e.g. completed jobs that moved out of "Open Jobs") — drop
            # it. Reconcile in Python to avoid SQLite's bound-parameter limit on large sets.
            for job in s.exec(select(MondayJob)).all():
                if job.monday_item_id not in seen:
                    s.delete(job)
        s.commit()
    return n


def _link_job_to_orders(s, job: MondayJob) -> None:
    """Stamp a cached order with this job's headline status when their numbers match."""
    targets = []
    if job.bc_number:
        targets.append(("bigcommerce", job.bc_number))
    if job.job_number:
        targets.append(("quickbooks", job.job_number))  # QBO estimate #
    if job.sr_number:
        targets.append(("quickbooks", job.sr_number))  # QBO sales receipt #
    for source, number in targets:
        order = s.exec(
            select(Order).where(Order.source == source, Order.number == number)
        ).first()
        if order:
            order.monday_status = job.main_status
            order.monday_item_id = job.monday_item_id
            s.add(order)


def run_sync(source: str) -> dict:
    """Run one connector and apply its results. Safe to call from the scheduler or the UI."""
    from ..connectors import bigcommerce, monday, quickbooks

    handlers = {
        "quickbooks": quickbooks.sync,
        "monday": monday.sync,
        "bigcommerce": bigcommerce.sync,
    }
    if source not in handlers:
        return {"ok": False, "error": f"unknown sync source: {source}"}

    with get_session() as s:
        st = s.get(SyncState, source)
        if st and not st.enabled:
            return {"skipped": True}

    try:
        creds = load_credentials(source) or {}
        if not creds:
            _finish(source, "error", error="not configured")
            return {"ok": False, "error": "not configured"}

        result = handlers[source](creds, _get_cursor(source)) or {}
        count = (
            upsert_orders(result.get("orders", []))
            + upsert_monday_jobs(result.get("monday_jobs", []), replace=result.get("replace", False))
        )
        _finish(source, "ok", cursor=result.get("cursor"), count=count)
        return {"ok": True, "count": count, "cursor": result.get("cursor")}
    except Exception as e:  # noqa: BLE001 — record the error, keep the scheduler alive
        log.exception("sync failed for %s", source)
        _finish(source, "error", error=str(e))
        return {"ok": False, "error": str(e)}
