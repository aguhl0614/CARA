"""Browser-reachable endpoint that serves a printable order PDF.

Mounted on the MAIN app (not the /tools spec) so it's reachable at the public base URL from the
user's browser. For a QuickBooks estimate it streams QBO's native PDF; for a BigCommerce order it
generates one from the cached data.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from .connectors import quickbooks
from .db import get_session
from .pdf import render_order_pdf
from .security import load_credentials, verify_print_token
from .tools.openapi_tools import _resolve_order_live

router = APIRouter(prefix="/print", tags=["print"])


def _pdf_response(pdf: bytes, filename: str) -> Response:
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


@router.get("/order")
def print_order(
    number: str = Query(..., description="Order/job number, e.g. 22736 or BC4106"),
    token: str = Query("", description="Signed token from get_order_pdf"),
):
    if not verify_print_token(number, token):
        raise HTTPException(status_code=403, detail="Invalid or missing print token.")
    with get_session() as s:
        _raw, _job, order = _resolve_order_live(s, number)
        if not order:
            raise HTTPException(status_code=404, detail="No QuickBooks or BigCommerce order found for that number.")
        source = order.source
        num = order.number
        if source == "bigcommerce":
            return _pdf_response(render_order_pdf(order), f"bc-{num}.pdf")
        estimate_id = (order.external_id or "").split(":", 1)[-1]

    if source == "quickbooks":
        creds = load_credentials("quickbooks") or {}
        try:
            pdf = quickbooks.fetch_estimate_pdf(creds, estimate_id)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"Could not fetch the QuickBooks PDF: {e}")
        return _pdf_response(pdf, f"estimate-{num}.pdf")

    raise HTTPException(status_code=404, detail=f"Printing isn't supported for source '{source}'.")
