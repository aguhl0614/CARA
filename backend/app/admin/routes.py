from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlmodel import select

from ..config import get_settings
from ..core_inventory import inventory_status
from ..db import get_session
from ..kv import get_setting, get_zoneinfo, set_setting
from ..models import AdminUser, Document, Machine, MondayJob, Order, SyncState
from ..rag.ingest import ingest_document
from ..rag.store import delete_document as delete_doc_chunks
from ..security import authenticate, credential_status, hash_password, load_credentials, save_credentials
from ..sync.service import SOURCES, run_sync

router = APIRouter(prefix="/admin", tags=["admin"])
_settings = get_settings()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

_SUBDIR = {"manual": "manuals", "workflow": "workflows", "inventory": "inventory"}


def _localdt(value):
    """Format a datetime (assumed UTC if naive) in the configured timezone as
    'MM-DD-YYYY h:mm AM/PM'."""
    if not value:
        return "—"
    dt = value
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt)
        except ValueError:
            return value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(get_zoneinfo())
    return f"{local.strftime('%m-%d-%Y')} {local.strftime('%I:%M %p').lstrip('0')}"


templates.env.filters["localdt"] = _localdt


def _logged_in(request: Request) -> bool:
    return bool(request.session.get("admin"))


def _redirect_login() -> RedirectResponse:
    return RedirectResponse("/admin/login", status_code=303)


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    if authenticate(username, password):
        request.session["admin"] = username
        return RedirectResponse("/admin", status_code=303)
    return templates.TemplateResponse(
        request, "login.html", {"error": "Invalid credentials"}, status_code=401
    )


@router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return _redirect_login()


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    if not _logged_in(request):
        return _redirect_login()
    with get_session() as s:
        sync_state_by_source = {st.source: st for st in s.exec(select(SyncState)).all()}
        sync_states = [sync_state_by_source[source] for source in SOURCES if source in sync_state_by_source]
        machines = s.exec(select(Machine)).all()
        documents = s.exec(select(Document)).all()
        # Total records CACHED per source (not the last run's delta).
        record_counts = {
            "bigcommerce": s.exec(select(func.count()).select_from(Order).where(Order.source == "bigcommerce")).one(),
            "quickbooks": s.exec(select(func.count()).select_from(Order).where(Order.source == "quickbooks")).one(),
            "monday": s.exec(select(func.count()).select_from(MondayJob)).one(),
        }
    cols_raw = get_setting("monday_columns_cache", "") or ""
    monday_columns = json.loads(cols_raw) if cols_raw else []
    monday_map = {
        "status": get_setting("monday_col_status", "") or "",
        "due": get_setting("monday_col_due", "") or "",
        "hard": get_setting("monday_col_hard", "") or "",
        "amount": get_setting("monday_col_amount", "") or "",
        "person": get_setting("monday_col_person", "") or "",
        "quick": get_setting("monday_col_quick", "") or "",
        "stages": [x for x in (get_setting("monday_col_stages", "") or "").split(",") if x],
    }
    groups_raw = get_setting("monday_groups_cache", "") or ""
    monday_groups = json.loads(groups_raw) if groups_raw else []
    monday_group = get_setting("monday_group", "") or "Open Jobs"
    _llm_defaults = {
        "quick": {"temperature": "0.7", "top_p": "0.8", "top_k": "20", "presence_penalty": "0.0", "repetition_penalty": "1.0"},
        "thinking": {"temperature": "0.6", "top_p": "0.95", "top_k": "20", "presence_penalty": "0.0", "repetition_penalty": "1.0"},
    }
    llm_modes = {
        mode: {p: get_setting(f"llm_{p}_{mode}", d) for p, d in defs.items()}
        for mode, defs in _llm_defaults.items()
    }
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "admin": request.session.get("admin"),
            "cred_status": credential_status(),
            "sync_states": sync_states,
            "record_counts": record_counts,
            "machines": machines,
            "documents": documents,
            "data_dir": str(_settings.data_dir),
            "support_contact": _settings.support_contact,
            "core_inventory": inventory_status(),
            "timezone": get_setting("timezone", "") or _settings.timezone,
            "monday_columns": monday_columns,
            "monday_map": monday_map,
            "monday_groups": monday_groups,
            "monday_group": monday_group,
            "monday_columns_error": get_setting("monday_columns_error", "") or "",
            "llm_modes": llm_modes,
            "pw_status": request.query_params.get("pw", ""),
        },
    )


@router.post("/credentials/{provider}")
async def save_creds(request: Request, provider: str):
    if not _logged_in(request):
        return _redirect_login()
    form = await request.form()
    data = {k: v for k, v in form.items() if str(v).strip() != ""}
    if data:
        save_credentials(provider, data)
    return RedirectResponse("/admin", status_code=303)


@router.post("/sync/{source}")
def sync_now(request: Request, source: str):
    if not _logged_in(request):
        return _redirect_login()
    run_sync(source)
    return RedirectResponse("/admin", status_code=303)


@router.post("/machines")
def add_machine(
    request: Request,
    name: str = Form(...),
    kind: str = Form("machine"),
    aliases: str = Form(""),
):
    if not _logged_in(request):
        return _redirect_login()
    alias_list = [a.strip() for a in aliases.split(",") if a.strip()]
    with get_session() as s:
        s.add(Machine(name=name, kind=kind, aliases=alias_list))
        s.commit()
    return RedirectResponse("/admin", status_code=303)


@router.post("/machines/{machine_id}")
def edit_machine(
    request: Request,
    machine_id: int,
    name: str = Form(...),
    kind: str = Form("machine"),
    aliases: str = Form(""),
):
    if not _logged_in(request):
        return _redirect_login()
    alias_list = [a.strip() for a in aliases.split(",") if a.strip()]
    with get_session() as s:
        m = s.get(Machine, machine_id)
        if m:
            m.name = name
            m.kind = kind
            m.aliases = alias_list
            s.add(m)
            s.commit()
    return RedirectResponse("/admin", status_code=303)


@router.post("/machines/{machine_id}/delete")
def delete_machine(request: Request, machine_id: int):
    if not _logged_in(request):
        return _redirect_login()
    with get_session() as s:
        m = s.get(Machine, machine_id)
        if m:
            s.delete(m)
            s.commit()
    return RedirectResponse("/admin", status_code=303)


@router.post("/documents")
async def upload_document(request: Request):
    if not _logged_in(request):
        return _redirect_login()
    form = await request.form()
    upload = form.get("file")
    if upload is None or not getattr(upload, "filename", ""):
        return RedirectResponse("/admin", status_code=303)

    doc_type = form.get("doc_type") or "manual"
    title = (form.get("title") or "").strip() or upload.filename
    machine_ids = [int(x) for x in form.getlist("machine_ids") if str(x).strip().isdigit()]

    rel = f"{_SUBDIR.get(doc_type, 'manuals')}/{upload.filename}"
    dest = _settings.documents_dir / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(await upload.read())

    with get_session() as s:
        doc = Document(title=title, path=rel, doc_type=doc_type, machine_ids=machine_ids)
        s.add(doc)
        s.commit()
        s.refresh(doc)
        doc_id = doc.id

    ingest_document(doc_id)  # may be slow on first run while the embed model loads
    return RedirectResponse("/admin", status_code=303)


@router.post("/documents/{doc_id}/delete")
def remove_document(request: Request, doc_id: int):
    if not _logged_in(request):
        return _redirect_login()
    with get_session() as s:
        doc = s.get(Document, doc_id)
        if not doc:
            return RedirectResponse("/admin", status_code=303)
        try:
            delete_doc_chunks(doc_id)  # remove this document's vectors from Chroma
        except Exception:  # noqa: BLE001
            pass
        # Only delete the file if no other document row points at it.
        others = s.exec(
            select(Document).where(Document.path == doc.path, Document.id != doc_id)
        ).all()
        if not others:
            try:
                (_settings.documents_dir / doc.path).unlink(missing_ok=True)
            except OSError:
                pass
        s.delete(doc)
        s.commit()
    return RedirectResponse("/admin", status_code=303)


@router.post("/settings")
def save_settings(
    request: Request,
    timezone: str = Form(""),
):
    if not _logged_in(request):
        return _redirect_login()
    set_setting("timezone", timezone.strip())
    return RedirectResponse("/admin", status_code=303)


@router.post("/password")
def change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
):
    if not _logged_in(request):
        return _redirect_login()
    username = request.session.get("admin") or ""
    if not authenticate(username, current_password):
        return RedirectResponse("/admin?pw=bad_current", status_code=303)
    if len(new_password) < 8:
        return RedirectResponse("/admin?pw=too_short", status_code=303)
    if new_password != confirm_password:
        return RedirectResponse("/admin?pw=mismatch", status_code=303)
    with get_session() as s:
        user = s.exec(select(AdminUser).where(AdminUser.username == username)).first()
        if not user:
            return RedirectResponse("/admin?pw=error", status_code=303)
        user.password_hash = hash_password(new_password)
        s.add(user)
        s.commit()
    return RedirectResponse("/admin?pw=ok", status_code=303)


@router.post("/llm/sampling")
async def save_llm_sampling(request: Request):
    """Per-mode sampling params consumed by the LLM proxy (backend/app/llm/proxy.py)."""
    if not _logged_in(request):
        return _redirect_login()
    form = await request.form()
    for mode in ("quick", "thinking"):
        for p in ("temperature", "top_p", "top_k", "presence_penalty", "repetition_penalty"):
            set_setting(f"llm_{p}_{mode}", (form.get(f"{mode}_{p}") or "").strip())
    return RedirectResponse("/admin", status_code=303)


@router.post("/monday/columns/refresh")
def refresh_monday_columns(request: Request):
    if not _logged_in(request):
        return _redirect_login()
    from ..connectors import monday as monday_connector

    creds = load_credentials("monday") or {}
    try:
        set_setting("monday_columns_cache", json.dumps(monday_connector.fetch_columns(creds)))
        set_setting("monday_groups_cache", json.dumps(monday_connector.fetch_groups(creds)))
        set_setting("monday_columns_error", "")
    except Exception as e:  # noqa: BLE001
        set_setting("monday_columns_error", str(e)[:200])
    return RedirectResponse("/admin", status_code=303)


@router.post("/monday/columns")
async def save_monday_columns(request: Request):
    if not _logged_in(request):
        return _redirect_login()
    form = await request.form()
    for role in ("status", "due", "hard", "amount", "person", "quick"):
        set_setting(f"monday_col_{role}", (form.get(role) or "").strip())
    set_setting("monday_col_stages", ",".join(x for x in form.getlist("stages") if str(x).strip()))
    set_setting("monday_group", (form.get("group") or "").strip())
    # Reset the Monday cursor so the next sync re-applies the new mapping/group to all jobs.
    with get_session() as s:
        st = s.get(SyncState, "monday")
        if st:
            st.cursor = None
            s.add(st)
            s.commit()
    return RedirectResponse("/admin", status_code=303)
