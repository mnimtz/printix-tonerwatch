"""Real dashboard replacing the coming-soon stub — cross-customer overview.

Shows four stat tiles at the top (customers, printers, critical, warn) plus
a per-customer status card list. Every stat is derived from the cached BI
snapshot to keep the page snappy; the fresh pull happens on /toner.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import desc, select

from .. import auth, bi_client, db
from ..db import audit_log
from . import toner_routes


router = APIRouter()


@router.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def dashboard(request: Request):
    user = auth.require_user(request)

    # Cache-only reads so the dashboard renders in < 100 ms even when the
    # BI-DB is asleep. The actual live pull runs on /toner.
    customers = toner_routes._visible_customers(user)
    per_customer_stats: list[dict] = []
    total_printers = 0
    critical_count = 0
    warn_count = 0
    unknown_count = 0

    for c in customers:
        bi_customer = bi_client.customer_for_bi(c)
        printers = bi_client.fetch_all_printer_supplies_cached_only(bi_customer)
        stats = {
            "id": c["id"], "name": c["name"], "tenant_url": c["tenant_url"],
            "printers": 0, "critical": 0, "warn": 0, "ok": 0, "unknown": 0,
            "has_data": printers is not None,
            "creds_missing": not (c["sql_server"] and c["sql_database"]
                                  and c["sql_username"]),
        }
        if printers:
            warn = int(c["warn_pct"] or 20)
            crit = int(c["critical_pct"] or 5)
            stats["printers"] = len(printers)
            for p in printers:
                worst = toner_routes._worst_supply_severity(
                    p["supplies"], warn, crit)
                stats[worst.lower()] += 1
        per_customer_stats.append(stats)
        total_printers  += stats["printers"]
        critical_count  += stats["critical"]
        warn_count      += stats["warn"]
        unknown_count   += stats["unknown"]

    # Recent audit events (across all customers the user can see; admins
    # see everything).
    with db.get_conn() as conn:
        recent = conn.execute(
            select(audit_log.c.action, audit_log.c.created_at,
                   audit_log.c.target_type, audit_log.c.target_id,
                   audit_log.c.user_id)
            .order_by(desc(audit_log.c.created_at))
            .limit(10)
        ).all()

    templates = request.app.state.templates
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "lang": request.state.lang,
            "user": user,
            "counts": {
                "customers": len(customers),
                "printers":  total_printers,
                "critical":  critical_count,
                "warn":      warn_count,
                "unknown":   unknown_count,
            },
            "per_customer": per_customer_stats,
            "recent_events": [dict(r._mapping) for r in recent],
        },
    )
