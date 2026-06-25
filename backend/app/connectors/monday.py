"""Monday.com connector (read-only) — the 'Jobs' production board.

Each Monday item is a job. Identifiers are embedded in the item NAME:
  - leading number  -> QuickBooks Estimate #   (the job number)
  - 'BC####'        -> BigCommerce order #     (links to cached BigCommerce orders)
  - 'SR#####'       -> QuickBooks Sales Receipt #
All status-type columns are captured so any stage can be asked about (req #3).

Scope: only the "Open Jobs" group is pulled — the other groups are completed jobs and
aren't accessed. The group title is configurable (admin Setting `monday_group`, default
"Open Jobs"). That group is the small active set, so each sync pulls the WHOLE group and
the caller REPLACES the cache with it, dropping jobs that left the group when completed.
Column IDs are auto-discovered from the board, so this adapts if columns change.

Returns {"monday_jobs": [...], "cursor": <iso>, "replace": True}.  creds: {"token", "board_id"}
"""

from __future__ import annotations

import re
import time
from datetime import datetime

import httpx

_URL = "https://api.monday.com/v2"
_PAGE = 100
_TIMEOUT = httpx.Timeout(30.0)

_LEAD_RE = re.compile(r"^\s*#?\s*(\d+)")
_BC_RE = re.compile(r"BC\s*#?\s*(\d+)", re.I)
_SR_RE = re.compile(r"SR\s*#?\s*(\d+)", re.I)

_DEFAULT_GROUP = "Open Jobs"

_BOARD_META = """
query ($boardId: ID!) {
  boards(ids: [$boardId]) { name columns { id title type } groups { id title } }
}
"""

_GROUP_FIRST_PAGE = """
query ($boardId: ID!, $groupId: String!, $limit: Int!) {
  boards(ids: [$boardId]) {
    groups(ids: [$groupId]) {
      items_page(limit: $limit) {
        cursor
        items { id name updated_at column_values { id text } }
      }
    }
  }
}
"""

_NEXT_PAGE = """
query ($limit: Int!, $cursor: String!) {
  next_items_page(limit: $limit, cursor: $cursor) {
    cursor
    items { id name updated_at column_values { id text } }
  }
}
"""


def _headers(token: str) -> dict:
    return {"Authorization": token, "Content-Type": "application/json", "API-Version": "2024-10"}


def _parse_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _to_float(value):
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _parse_name(name: str):
    name = name or ""
    lead = _LEAD_RE.search(name)
    bc = _BC_RE.search(name)
    sr = _SR_RE.search(name)
    job_number = lead.group(1) if lead else None
    title = name[lead.end():].strip(" -\t") if lead else name.strip()
    return job_number, (bc.group(1) if bc else None), (sr.group(1) if sr else None), title


def _post(client: httpx.Client, query: str, variables: dict) -> dict:
    for _ in range(5):
        resp = client.post(_URL, json={"query": query, "variables": variables})
        if resp.status_code == 429:
            time.sleep(min(int(resp.headers.get("Retry-After", "30")), 60))
            continue
        resp.raise_for_status()
        data = resp.json()
        if data.get("errors"):
            raise ValueError(f"Monday API error: {data['errors']}")
        return data["data"]
    raise ValueError("Monday API: too many retries (rate limited)")


def fetch_columns(creds: dict) -> list[dict]:
    """Return the board's columns [{id, title, type}] for the admin column picker."""
    if not creds.get("token") or not creds.get("board_id"):
        return []
    with httpx.Client(timeout=_TIMEOUT, headers=_headers(creds["token"])) as client:
        data = _post(client, _BOARD_META, {"boardId": creds["board_id"]})
    boards = data.get("boards") or []
    return boards[0]["columns"] if boards else []


def fetch_groups(creds: dict) -> list[dict]:
    """Return the board's groups [{id, title}] for the admin group picker."""
    if not creds.get("token") or not creds.get("board_id"):
        return []
    with httpx.Client(timeout=_TIMEOUT, headers=_headers(creds["token"])) as client:
        data = _post(client, _BOARD_META, {"boardId": creds["board_id"]})
    boards = data.get("boards") or []
    return boards[0].get("groups", []) if boards else []


def _resolve_group_id(groups: list[dict], title: str) -> str | None:
    want = (title or "").strip().lower()
    return next((g["id"] for g in groups if (g.get("title") or "").strip().lower() == want), None)


def _discover_columns(columns: list[dict]) -> dict:
    """Map board columns to roles. Admin-chosen column IDs (saved as settings) win;
    anything left blank falls back to auto-detection by title/type."""
    from ..kv import get_setting

    col_ids = {c["id"] for c in columns}

    def first(pred):
        return next((c["id"] for c in columns if pred(c)), None)

    def chosen(key, auto):
        v = (get_setting(key) or "").strip()
        return v if v in col_ids else auto

    stages_cfg = [x.strip() for x in (get_setting("monday_col_stages") or "").split(",") if x.strip()]
    if stages_cfg:
        status = {c["id"]: c["title"] for c in columns if c["id"] in stages_cfg}
    else:
        status = {c["id"]: c["title"] for c in columns if c["type"] == "status"}

    return {
        "status": status,
        "main_status": chosen("monday_col_status", first(lambda c: c["title"].strip().lower() == "status")),
        "amount": chosen("monday_col_amount", first(lambda c: c["type"] == "numbers")),
        "person": chosen("monday_col_person", first(lambda c: c["type"] == "people")),
        "due": chosen("monday_col_due", first(lambda c: c["title"] == "Due Date")),
        "hard": chosen("monday_col_hard", first(lambda c: c["title"] == "Hard Date")),
        "quick": chosen("monday_col_quick", first(lambda c: c["title"] == "Quick Info") or first(lambda c: c["type"] == "text")),
    }


def sync(creds: dict, cursor: str | None) -> dict:
    token = creds.get("token")
    board_id = creds.get("board_id")
    if not token or not board_id:
        raise ValueError("Monday requires token and board_id")

    from ..kv import get_setting

    desired_group = (get_setting("monday_group") or "").strip() or _DEFAULT_GROUP
    jobs: list[dict] = []
    max_updated = None  # full re-pull each time (the group is the small active set), so no cursor filter

    with httpx.Client(timeout=_TIMEOUT, headers=_headers(token)) as client:
        meta = _post(client, _BOARD_META, {"boardId": board_id})
        boards = meta.get("boards") or []
        if not boards:
            raise ValueError(f"Monday board {board_id} not found or not accessible")
        cmap = _discover_columns(boards[0]["columns"])
        status_cols = cmap["status"]

        group_id = _resolve_group_id(boards[0].get("groups") or [], desired_group)
        if not group_id:
            titles = ", ".join(repr(g.get("title")) for g in (boards[0].get("groups") or [])) or "(none)"
            raise ValueError(f"Monday group {desired_group!r} not found on the board. Available groups: {titles}")

        data = _post(client, _GROUP_FIRST_PAGE, {"boardId": board_id, "groupId": group_id, "limit": _PAGE})
        grp = (data.get("boards") or [{}])[0].get("groups") or []
        page = (grp[0].get("items_page") if grp else None) or {"items": [], "cursor": None}

        while True:
            items = page.get("items") or []
            for it in items:
                updated = _parse_dt(it.get("updated_at"))
                cv = {c["id"]: c.get("text") for c in (it.get("column_values") or [])}
                job_number, bc, sr, title = _parse_name(it.get("name"))
                jobs.append(
                    {
                        "monday_item_id": str(it["id"]),
                        "job_number": job_number,
                        "bc_number": bc,
                        "sr_number": sr,
                        "title": title,
                        "main_status": cv.get(cmap["main_status"]) if cmap["main_status"] else None,
                        "statuses": {t: cv.get(cid) for cid, t in status_cols.items() if cv.get(cid)},
                        "amount": _to_float(cv.get(cmap["amount"])) if cmap["amount"] else None,
                        "due_date": cv.get(cmap["due"]) if cmap["due"] else None,
                        "hard_date": cv.get(cmap["hard"]) if cmap["hard"] else None,
                        "person": cv.get(cmap["person"]) if cmap["person"] else None,
                        "quick_info": cv.get(cmap["quick"]) if cmap["quick"] else None,
                        "source_updated_at": updated,
                    }
                )
                if updated:
                    iso = updated.isoformat()
                    if max_updated is None or iso > max_updated:
                        max_updated = iso

            next_cursor = page.get("cursor")
            if not next_cursor or not items:
                break
            data = _post(client, _NEXT_PAGE, {"limit": _PAGE, "cursor": next_cursor})
            page = data["next_items_page"]

    return {"monday_jobs": jobs, "cursor": max_updated, "replace": True}
