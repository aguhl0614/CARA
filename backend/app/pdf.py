"""Generate a printable order PDF from a cached Order (used for BigCommerce orders, which
have no native PDF). QuickBooks estimates use their own native PDF instead."""

from __future__ import annotations

import io
from datetime import datetime

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


def _esc(value) -> str:
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _money(value) -> str:
    try:
        return f"${float(value):,.2f}" if value not in (None, "") else ""
    except (TypeError, ValueError):
        return str(value or "")


def _fmt_date(value) -> str:
    if not value:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%m-%d-%Y")
    try:
        return datetime.fromisoformat(str(value)[:19]).strftime("%m-%d-%Y")
    except (ValueError, TypeError):
        return str(value)


def render_order_pdf(order) -> bytes:
    raw = order.raw or {}
    line_items = raw.get("line_items") or []
    billing = raw.get("billing") or {}

    styles = getSampleStyleSheet()
    cell = ParagraphStyle("cell", parent=styles["Normal"], fontSize=8, leading=10)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=letter,
        leftMargin=0.6 * inch, rightMargin=0.6 * inch, topMargin=0.6 * inch, bottomMargin=0.6 * inch,
        title=f"Order {order.number}",
    )

    elems = [
        Paragraph("Collegiate Awards &amp; Recognition", styles["Title"]),
        Paragraph(f"Order {_esc(order.number)} &nbsp;—&nbsp; {_esc(order.customer or '')}", styles["Heading2"]),
    ]
    meta = []
    if order.order_date:
        meta.append("Date: " + _fmt_date(order.order_date))
    meta.append("Source: " + _esc(order.source))
    if order.status:
        meta.append("Status: " + _esc(order.status))
    elems.append(Paragraph(" &nbsp;|&nbsp; ".join(meta), styles["Normal"]))

    bill = "<br/>".join(
        _esc(x) for x in (billing.get("name"), billing.get("company"), billing.get("email"), billing.get("phone")) if x
    )
    if bill:
        elems += [Spacer(1, 0.12 * inch), Paragraph("<b>Bill to:</b><br/>" + bill, styles["Normal"])]

    elems.append(Spacer(1, 0.2 * inch))

    rows = [["Item", "Options", "Qty", "Unit", "Amount"]]
    for li in line_items:
        item = _esc(li.get("item") or "")
        if li.get("sku"):
            item += f"<br/><font size=7 color='#666666'>{_esc(li.get('sku'))}</font>"
        opts = "<br/>".join(
            f"{_esc(o.get('name'))}: {_esc(o.get('value'))}"
            for o in (li.get("options") or [])
            if o.get("name") or o.get("value")
        )
        rows.append([
            Paragraph(item, cell),
            Paragraph(opts, cell),
            Paragraph(_esc(li.get("qty") if li.get("qty") is not None else ""), cell),
            Paragraph(_money(li.get("unit_price")), cell),
            Paragraph(_money(li.get("amount")), cell),
        ])

    table = Table(rows, colWidths=[2.0 * inch, 2.6 * inch, 0.5 * inch, 0.8 * inch, 0.9 * inch], repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#22324a")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 8),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f5f6f8")]),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    elems.append(table)

    if order.total is not None:
        elems += [
            Spacer(1, 0.15 * inch),
            Paragraph(f"<b>Total: {_money(order.total)} {_esc(order.currency or '')}</b>", styles["Normal"]),
        ]

    doc.build(elems)
    return buf.getvalue()
