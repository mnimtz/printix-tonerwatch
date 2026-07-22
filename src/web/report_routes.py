"""Flexible reporting hub — v0.24.36.

Three routes, one shared parameter set (date range + customer scope +
category checkboxes):

* ``GET /reports``            — the builder + quick-launch templates
* ``GET /reports/run``         — computes the selected categories and
  renders the result tables (bookmarkable — plain query params, no POST)
* ``GET /reports/run/narrative`` — AJAX-only: the same params, asks the
  LLM to phrase the already-computed facts (button-triggered, never
  automatic — a wide report can touch a lot of data and there's no
  reason to spend an LLM call before the operator has looked at the
  tables and decided they want the paragraph too)
* ``GET /reports/run/export.csv`` — same params, streams a CSV of
  whatever tables the result page would have shown

Every route funnels the customer scope through
``auth.visible_customer_ids`` / ``auth.user_can_see_customer`` — a
report is not a way to see data across a tenant fence that the toner
grid or orders board wouldn't otherwise show this operator.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from .. import auth, db, reports
from ..db import customers as customers_tbl


router = APIRouter()

_ALL_CATEGORIES = ("orders", "consumption", "device_health",
                   "supplier_performance", "active_users",
                   "registered_users", "user_comparison")
# active_users / registered_users / user_comparison are opt-in only —
# they're a live-ish BI-DB snapshot, not a date-windowed historical
# aggregate like the others, so they shouldn't silently ride along
# whenever an operator just wants "everything".
_DEFAULT_CATEGORIES = ("orders", "consumption", "device_health", "supplier_performance")


def _customer_choices(user: dict) -> list[tuple[int, str]]:
    ids = auth.visible_customer_ids(user)
    if not ids:
        return []
    from sqlalchemy import select
    with db.get_conn() as conn:
        rows = conn.execute(
            select(customers_tbl.c.id, customers_tbl.c.name)
            .where(customers_tbl.c.id.in_(ids))
        ).all()
    return sorted(((r.id, r.name) for r in rows), key=lambda kv: kv[1].lower())


def _parse_scope(request: Request, user: dict) -> tuple[list[int], str]:
    """Returns (customer_ids, scope_label). A ``customer`` query param
    of "all" or absent means every customer this operator can see;
    a numeric id not in that set is silently dropped rather than
    trusted — same tenant fence as every other report/order route."""
    visible = auth.visible_customer_ids(user)
    raw = (request.query_params.get("customer") or "").strip()
    if raw and raw != "all" and raw.isdigit() and int(raw) in visible:
        cid = int(raw)
        with db.get_conn() as conn:
            from sqlalchemy import select
            row = conn.execute(
                select(customers_tbl.c.name).where(customers_tbl.c.id == cid)
            ).first()
        return [cid], (row.name if row else f"#{cid}")
    return visible, "all"


def _parse_date_range(request: Request) -> tuple[str, str]:
    today = date.today()
    default_from = (today - timedelta(days=30)).isoformat()
    default_to = today.isoformat()
    date_from = (request.query_params.get("date_from") or "").strip() or default_from
    date_to = (request.query_params.get("date_to") or "").strip() or default_to
    # Defensive swap — a from/to typo shouldn't silently return an
    # empty (or worse, backwards-interpreted) window.
    if date_from > date_to:
        date_from, date_to = date_to, date_from
    return date_from, date_to


def _parse_categories(request: Request) -> list[str]:
    raw = request.query_params.getlist("category")
    picked = [c for c in raw if c in _ALL_CATEGORIES]
    return picked or list(_DEFAULT_CATEGORIES)


def _compute(categories: list[str], customer_ids: list[int],
             date_from: str, date_to: str) -> dict[str, Any]:
    facts: dict[str, Any] = {}
    if "orders" in categories:
        facts["orders"] = reports.compute_orders_facts(customer_ids, date_from, date_to)
    if "consumption" in categories:
        facts["consumption"] = reports.compute_consumption_facts(
            customer_ids, date_from, date_to)
    if "device_health" in categories:
        facts["device_health"] = reports.compute_device_health_facts(
            customer_ids, date_from, date_to)
    if "supplier_performance" in categories:
        facts["supplier_performance"] = reports.compute_supplier_performance_facts(
            customer_ids, date_from, date_to)
    if "active_users" in categories:
        facts["active_users"] = reports.compute_active_users_facts(
            customer_ids, date_from, date_to)
    if "registered_users" in categories:
        facts["registered_users"] = reports.compute_registered_users_facts(
            customer_ids, date_from, date_to)
    if "user_comparison" in categories:
        facts["user_comparison"] = reports.compute_user_comparison_facts(
            customer_ids, date_from, date_to)
    return facts


# ---------------------------------------------------------------------------
# Hub
# ---------------------------------------------------------------------------

@router.get("/reports", response_class=HTMLResponse, include_in_schema=False)
async def reports_hub(request: Request):
    user = auth.require_user(request)
    today = date.today()
    return request.app.state.templates.TemplateResponse(
        "reports/hub.html",
        {
            "request": request,
            "lang": request.state.lang,
            "user": user,
            "customer_choices": _customer_choices(user),
            "default_from": (today - timedelta(days=30)).isoformat(),
            "default_to": today.isoformat(),
            "today": today.isoformat(),
        },
    )


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

@router.get("/reports/run", response_class=HTMLResponse, include_in_schema=False)
async def reports_run(request: Request):
    user = auth.require_user(request)
    customer_ids, scope_label = _parse_scope(request, user)
    date_from, date_to = _parse_date_range(request)
    categories = _parse_categories(request)
    customer_choices = _customer_choices(user)

    facts = _compute(categories, customer_ids, date_from, date_to)

    return request.app.state.templates.TemplateResponse(
        "reports/result.html",
        {
            "request": request,
            "lang": request.state.lang,
            "user": user,
            "categories": categories,
            "date_from": date_from,
            "date_to": date_to,
            "customer_id": request.query_params.get("customer") or "all",
            "customer_choices": customer_choices,
            "scope_label": scope_label,
            "facts": facts,
            "query_string": str(request.url.query),
        },
    )


@router.get("/reports/run/narrative", include_in_schema=False)
async def reports_run_narrative(request: Request):
    """AJAX-only, JSON. Recomputes the same facts server-side rather
    than trusting anything from the client — the narrative must be
    grounded in numbers this admin is actually allowed to see."""
    user = auth.require_user(request)
    customer_ids, scope_label = _parse_scope(request, user)
    date_from, date_to = _parse_date_range(request)
    categories = _parse_categories(request)
    facts = _compute(categories, customer_ids, date_from, date_to)
    if scope_label == "all":
        scope_label = "all customers"

    narrative, narrative_error = reports.generate_report_narrative(
        scope_label, date_from, date_to, facts, lang=request.state.lang)
    if narrative is None:
        if narrative_error:
            return JSONResponse(
                {"ok": False, "error": "llm_call_failed", "detail": narrative_error},
                status_code=502)
        return JSONResponse({"ok": False, "error": "llm_unavailable"}, status_code=400)
    return JSONResponse({"ok": True, "narrative": narrative})


@router.get("/reports/run/export.csv", include_in_schema=False)
async def reports_run_export_csv(request: Request):
    user = auth.require_user(request)
    customer_ids, _ = _parse_scope(request, user)
    date_from, date_to = _parse_date_range(request)
    categories = _parse_categories(request)
    facts = _compute(categories, customer_ids, date_from, date_to)

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([f"TonerWatch report — {date_from} to {date_to}"])
    w.writerow([])

    if "orders" in facts:
        f = facts["orders"]
        w.writerow(["ORDERS"])
        w.writerow(["total_orders", f["total_orders"]])
        w.writerow(["total_quantity", f["total_quantity"]])
        w.writerow(["avg_fulfillment_days", f["avg_fulfillment_days"]])
        w.writerow([])
        w.writerow(["status", "count"])
        for status, count in f["by_status"].items():
            w.writerow([status, count])
        w.writerow([])
        w.writerow(["printer_id", "printer_name", "orders", "quantity"])
        for row in f["top_printers"]:
            w.writerow([row["printer_id"], row["printer_name"], row["orders"], row["quantity"]])
        w.writerow([])

    if "consumption" in facts:
        f = facts["consumption"]
        w.writerow(["CONSUMPTION"])
        w.writerow(["total_units", f["total_units"]])
        w.writerow(["total_spend_cents", f["total_spend_cents"]])
        w.writerow([])
        w.writerow(["color", "units"])
        for color, units in f["by_color"].items():
            w.writerow([color, units])
        w.writerow([])
        w.writerow(["customer", "units", "spend_cents"])
        for cust, row in f["by_customer"].items():
            w.writerow([cust, row["units"], row["spend_cents"]])
        w.writerow([])

    if "device_health" in facts:
        f = facts["device_health"]
        w.writerow(["DEVICE HEALTH"])
        w.writerow(["total_anomalies", f["total_anomalies"]])
        w.writerow([])
        w.writerow(["printer_id", "color", "level", "prev_level", "anomaly_kind", "created_at"])
        for row in f["recent_anomalies"]:
            w.writerow([row["printer_id"], row["color"], row["level"],
                        row["prev_level"], row["anomaly_kind"], row["created_at"]])
        w.writerow([])

    if "supplier_performance" in facts:
        f = facts["supplier_performance"]
        w.writerow(["SUPPLIER PERFORMANCE"])
        w.writerow(["supplier", "orders", "quantity", "spend_cents", "avg_fulfillment_days"])
        for row in f["suppliers"]:
            w.writerow([row["supplier"], row["orders"], row["quantity"],
                        row["spend_cents"], row["avg_fulfillment_days"]])
        w.writerow([])

    if "active_users" in facts:
        f = facts["active_users"]
        w.writerow(["ACTIVE USERS"])
        w.writerow(["total_active_users", f["total_active_users"]])
        w.writerow([])
        w.writerow(["customer", "active_users"])
        for row in f["by_customer"]:
            w.writerow([row["customer_name"],
                        row["active_users"] if row["active_users"] is not None else ""])
        w.writerow([])
        if f["users_detail"]:
            w.writerow([f'USERS — {f["detail_customer_name"]}'])
            w.writerow(["name", "email", "department"])
            for u in f["users_detail"]:
                w.writerow([u["name"], u["email"], u["department"]])
            w.writerow([])

    if "registered_users" in facts:
        f = facts["registered_users"]
        w.writerow(["REGISTERED USERS"])
        w.writerow(["total_registered_users", f["total_registered_users"]])
        w.writerow([])
        w.writerow(["customer", "registered_users"])
        for row in f["by_customer"]:
            w.writerow([row["customer_name"],
                        row["registered_users"] if row["registered_users"] is not None else ""])
        w.writerow([])
        if f["users_detail"]:
            w.writerow([f'USERS — {f["detail_customer_name"]}'])
            w.writerow(["name", "email", "department"])
            for u in f["users_detail"]:
                w.writerow([u["name"], u["email"], u["department"]])
            w.writerow([])

    if "user_comparison" in facts:
        f = facts["user_comparison"]
        w.writerow(["ACTIVE VS REGISTERED USERS"])
        w.writerow(["total_active_users", f["total_active_users"]])
        w.writerow(["total_registered_users", f["total_registered_users"]])
        w.writerow(["total_gap", f["total_gap"]])
        w.writerow(["overall_active_pct", f["overall_active_pct"]
                    if f["overall_active_pct"] is not None else ""])
        w.writerow([])
        w.writerow(["customer", "registered_users", "active_users", "gap", "active_pct"])
        for row in f["by_customer"]:
            w.writerow([row["customer_name"],
                        row["registered_users"] if row["registered_users"] is not None else "",
                        row["active_users"] if row["active_users"] is not None else "",
                        row["gap"] if row["gap"] is not None else "",
                        row["active_pct"] if row["active_pct"] is not None else ""])
        w.writerow([])
        if f["users_detail"]:
            w.writerow([f'USERS — {f["detail_customer_name"]}'])
            w.writerow(["name", "email", "department", "status"])
            for u in f["users_detail"]:
                w.writerow([u["name"], u["email"], u["department"],
                            "active" if u["is_active"] else "registered_only"])
            w.writerow([])

    csv_bytes = buf.getvalue().encode("utf-8-sig")  # BOM so Excel gets UTF-8 right
    filename = f"tonerwatch-report_{date_from}_{date_to}.csv"

    def _stream():
        yield csv_bytes

    return StreamingResponse(
        _stream(),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(csv_bytes)),
            "Cache-Control": "no-store",
        },
    )
