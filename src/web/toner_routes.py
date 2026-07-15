"""Cross-customer toner grid — /toner.

Reads the latest supply snapshot for every printer of every customer the
current user is allowed to see, then renders one card per printer with
CMYK bars. BI-DB queries are cache-first (10 min TTL); a cold customer
hits the DB synchronously with a short timeout and shows a per-customer
error state if the fetch fails.
"""

from __future__ import annotations

import logging
from typing import Iterable

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from .. import auth, bi_client, db, printer_info, supply_library
from ..db import customers as customers_tbl, customer_access
from ..web import printer_icons


logger = logging.getLogger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# Data shaping
# ---------------------------------------------------------------------------

def _visible_customers(user: dict) -> list[dict]:
    with db.get_conn() as conn:
        if user["role"] == "admin":
            rows = conn.execute(
                select(customers_tbl).where(customers_tbl.c.active == 1)
                .order_by(customers_tbl.c.name)
            ).all()
        else:
            rows = conn.execute(
                select(customers_tbl)
                .select_from(
                    customers_tbl.join(
                        customer_access,
                        customers_tbl.c.id == customer_access.c.customer_id,
                    )
                )
                .where(customer_access.c.user_id == user["id"])
                .where(customers_tbl.c.active == 1)
                .order_by(customers_tbl.c.name)
            ).all()
    return [db._row_to_dict(r) for r in rows]


def _severity_rank(sev: str) -> int:
    return {"CRITICAL": 0, "WARN": 1, "OK": 2, "UNKNOWN": 3}.get(sev, 3)


def _worst_supply_severity(supplies: list[dict], warn: int, crit: int) -> str:
    if not supplies:
        return "UNKNOWN"
    worst = "OK"
    for s in supplies:
        sev = bi_client.classify_severity(s["level"],
                                          warn_pct=warn, critical_pct=crit)
        if _severity_rank(sev) < _severity_rank(worst):
            worst = sev
    return worst


def _collect_printer_rows(user: dict) -> tuple[list[dict], list[dict]]:
    """Return (printer_rows, per_customer_errors).

    printer_rows: one dict per printer, with the customer name and a
    pre-computed worst-supply severity baked in so the template stays
    dumb. Ordered CRITICAL → WARN → OK → UNKNOWN, then by customer +
    printer name.

    per_customer_errors: entries for customers we could not query
    (missing creds, timeout, unreachable, etc.) so the template can
    surface them as banners instead of silently missing rows.
    """
    printer_rows: list[dict] = []
    errors: list[dict] = []
    for c in _visible_customers(user):
        creds_missing = not (c["sql_server"] and c["sql_database"]
                             and c["sql_username"])
        if creds_missing:
            errors.append({"customer_id": c["id"], "customer_name": c["name"],
                           "reason": "no_credentials"})
            continue

        bi_customer = bi_client.customer_for_bi(c)
        printers = bi_client.fetch_all_printer_supplies(bi_customer)
        if printers is None:
            errors.append({"customer_id": c["id"], "customer_name": c["name"],
                           "reason": "fetch_failed"})
            continue
        if not printers:
            # No active printers under the tenant — not an error, just empty.
            continue

        warn = int(c["warn_pct"] or 20)
        crit = int(c["critical_pct"] or 5)
        # v0.9: bulk-load per-printer overrides for this customer so
        # every row can be enriched without an N+1 SELECT.
        info_map = printer_info.list_info_for_customer(c["id"])
        for p in printers:
            supplies_scored = []
            for s in p["supplies"]:
                supply = supply_library.resolve_supply(
                    c["id"], p["id"], p.get("model") or "", s["color"])
                supplies_scored.append({
                    **s,
                    "severity": bi_client.classify_severity(
                        s["level"], warn_pct=warn, critical_pct=crit),
                    "supply": supply,
                })
            # Merge BI row with our own overrides (location, serial,
            # group, asset-tag, contact, notes). Location + serial
            # take precedence over BI when non-empty; the rest are
            # passthrough fields not exposed by BI at all.
            enriched = printer_info.enrich(p, info_map.get(p["id"]))
            printer_rows.append({
                **enriched,
                "supplies": supplies_scored,
                "worst_severity": _worst_supply_severity(
                    p["supplies"], warn, crit),
                "customer_id": c["id"],
                "customer_name": c["name"],
                "warn_pct": warn,
                "critical_pct": crit,
                "icon_key": printer_icons.classify_model(p.get("model") or ""),
            })

    printer_rows.sort(key=lambda r: (
        _severity_rank(r["worst_severity"]),
        r["customer_name"].lower(),
        (r["printer_name"] or "").lower(),
    ))
    return printer_rows, errors


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/toner", response_class=HTMLResponse, include_in_schema=False)
async def toner_grid(request: Request):
    user = auth.require_user(request)
    rows, errors = _collect_printer_rows(user)

    # Filter params from query string
    q = request.query_params
    filter_customer = q.get("customer", "")
    filter_severity = q.get("severity", "")
    filter_group    = q.get("group", "")
    filter_search   = q.get("q", "").strip().lower()
    # View mode — persist in session so page reloads / navigation keep it.
    view_mode = q.get("view", "").strip().lower()
    if view_mode in ("grid", "list"):
        try:
            request.session["toner_view"] = view_mode
        except AssertionError:
            pass
    else:
        try:
            view_mode = request.session.get("toner_view", "grid")
        except AssertionError:
            view_mode = "grid"
    if view_mode not in ("grid", "list"):
        view_mode = "grid"

    filtered = rows
    if filter_customer.isdigit():
        cid = int(filter_customer)
        filtered = [r for r in filtered if r["customer_id"] == cid]
    if filter_severity in ("CRITICAL", "WARN"):
        # "WARN" filter includes CRITICAL — an operator triaging warns
        # cares about criticals too
        wanted = {"CRITICAL"} if filter_severity == "CRITICAL" else {"CRITICAL", "WARN"}
        filtered = [r for r in filtered if r["worst_severity"] in wanted]
    if filter_group:
        filtered = [r for r in filtered
                    if (r.get("group_name") or "") == filter_group]
    if filter_search:
        # Case-insensitive substring match on the most useful fields.
        def _matches(r: dict) -> bool:
            hay = " ".join(str(r.get(k) or "") for k in
                           ("printer_name", "location", "model", "vendor",
                            "serial_number", "asset_tag", "notes")).lower()
            return filter_search in hay
        filtered = [r for r in filtered if _matches(r)]

    # Distinct customer list for the dropdown
    customer_choices = sorted(
        {(r["customer_id"], r["customer_name"]) for r in rows},
        key=lambda t: t[1].lower(),
    )
    # Distinct group list for the group filter (over the whole,
    # unfiltered set of rows the user can see).
    group_choices = sorted(
        {(r.get("group_name") or "") for r in rows if r.get("group_name")},
        key=str.lower,
    )

    # Summary counters (over the UNFILTERED set)
    counts = {
        "total":    len(rows),
        "critical": sum(1 for r in rows if r["worst_severity"] == "CRITICAL"),
        "warn":     sum(1 for r in rows if r["worst_severity"] == "WARN"),
        "ok":       sum(1 for r in rows if r["worst_severity"] == "OK"),
        "unknown":  sum(1 for r in rows if r["worst_severity"] == "UNKNOWN"),
    }

    templates = request.app.state.templates
    template_name = "toner/list.html" if view_mode == "list" else "toner/grid.html"
    return templates.TemplateResponse(
        template_name,
        {
            "request": request,
            "lang": request.state.lang,
            "user": user,
            "printers": filtered,
            "errors": errors,
            "customer_choices": customer_choices,
            "group_choices": group_choices,
            "counts": counts,
            "filter_customer": filter_customer,
            "filter_severity": filter_severity,
            "filter_group":    filter_group,
            "filter_search":   filter_search,
            "view_mode":       view_mode,
        },
    )


@router.post("/toner/refresh", include_in_schema=False)
async def toner_refresh(request: Request):
    """Drop the cache for the customers the user can see, then bounce back
    to /toner. Cheap way to force a fresh BI-DB pull without a full server
    restart."""
    user = auth.require_user(request)
    for c in _visible_customers(user):
        bi_client.invalidate_customer_cache(c["id"])
    db.audit(user["id"], "toner.cache_flushed")
    return RedirectResponse("/toner", status_code=303)
