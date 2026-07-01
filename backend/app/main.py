from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from starlette.middleware.sessions import SessionMiddleware

from .admin.routes import router as admin_router
from .config import get_settings
from .db import init_db
from .llm.proxy import router as llm_router
from .print_routes import router as print_router
from .security import ensure_admin
from .sync.scheduler import shutdown_scheduler, start_scheduler
from .tools.openapi_tools import require_tools_token, router as tools_router

_settings = get_settings()

# Log to stdout (captured by Docker) AND to a rotating file on the bound volume, so
# diagnostic logs persist with the project and can be shared for troubleshooting.
_log_handlers: list[logging.Handler] = [logging.StreamHandler()]
try:
    from logging.handlers import RotatingFileHandler

    _log_handlers.append(
        RotatingFileHandler(_settings.cara_dir / "cara.log", maxBytes=5_000_000, backupCount=3)
    )
except OSError:
    pass
logging.basicConfig(
    level=logging.INFO,
    handlers=_log_handlers,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    ensure_admin()
    start_scheduler()
    try:
        yield
    finally:
        shutdown_scheduler()


app = FastAPI(
    title="CARA Backend",
    description=(
        "Tools and admin for the Collegiate Awards & Recognition Assistant. "
        "The /tools endpoints are exposed to Open WebUI as an OpenAPI tool server."
    ),
    version="0.1.0",
    lifespan=lifespan,
    # Hide the main app's API schema/docs — they would reveal the admin/print/proxy endpoints.
    # (The /tools sub-app keeps its own spec at /tools/openapi.json for Open WebUI.)
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.add_middleware(SessionMiddleware, secret_key=_settings.secret_key)


@app.middleware("http")
async def _security_headers(request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    return resp

# Tools are a mounted sub-app so its /openapi.json (served at /tools/openapi.json)
# contains ONLY the read-only tools. Open WebUI ingests that spec, so admin endpoints
# are never exposed to the model. Point Open WebUI's tool server at /tools.
tools_app = FastAPI(
    title="CARA Tools",
    version="0.1.0",
    description="Read-only tools the assistant can call (orders, inventory, documentation).",
    dependencies=[Depends(require_tools_token)],
)
tools_app.include_router(tools_router)


@tools_app.get("/", include_in_schema=False)
def tools_root():
    return {"service": "CARA tools", "spec": "/tools/openapi.json"}


app.mount("/tools", tools_app)

app.include_router(admin_router)
app.include_router(print_router)
# OpenAI-compatible LLM proxy (Open WebUI -> /llm/v1 -> LM Studio): classifies each chat and
# applies the per-mode thinking (reasoning_effort) + sampling settings.
app.include_router(llm_router)


@app.get("/healthz", tags=["system"])
def healthz():
    return {"status": "ok"}
