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
    # v0.19.0 — LEFT JOIN users so the template can say "Marcus" instead
    # of "user_id=1", and (best-effort) resolve customer/user target_ids
    # into readable names.
    from ..db import users as _users_tbl, customers as _customers_tbl
    from sqlalchemy import select as _select
    recent_events: list[dict] = []
    with db.get_conn() as conn:
        raw_events = conn.execute(
            _select(
                audit_log.c.action, audit_log.c.created_at,
                audit_log.c.target_type, audit_log.c.target_id,
                audit_log.c.user_id, audit_log.c.meta_json,
                _users_tbl.c.name.label("actor_name"),
                _users_tbl.c.email.label("actor_email"),
            )
            .select_from(
                audit_log.outerjoin(
                    _users_tbl, _users_tbl.c.id == audit_log.c.user_id))
            .order_by(desc(audit_log.c.created_at))
            .limit(10)
        ).all()
        cust_cache: dict[int, str] = {}
        user_cache: dict[int, str] = {}
        for r in raw_events:
            row = dict(r._mapping)
            # Resolve target_id to a human label for the two most common
            # target types. Keeps the SELECT above dialect-portable.
            label = ""
            if row["target_type"] == "customer" and row["target_id"]:
                try:
                    cid = int(row["target_id"])
                except (TypeError, ValueError):
                    cid = 0
                if cid:
                    if cid not in cust_cache:
                        cust_row = conn.execute(
                            _select(_customers_tbl.c.name)
                            .where(_customers_tbl.c.id == cid)
                        ).first()
                        cust_cache[cid] = cust_row.name if cust_row else ""
                    label = cust_cache[cid]
            elif row["target_type"] == "user" and row["target_id"]:
                try:
                    uid = int(row["target_id"])
                except (TypeError, ValueError):
                    uid = 0
                if uid:
                    if uid not in user_cache:
                        u_row = conn.execute(
                            _select(_users_tbl.c.name, _users_tbl.c.email)
                            .where(_users_tbl.c.id == uid)
                        ).first()
                        user_cache[uid] = (u_row.name or u_row.email
                                            if u_row else "")
                    label = user_cache[uid]
            row["target_label"] = label or row["target_id"] or ""
            row["actor_display"] = (row["actor_name"] or row["actor_email"]
                                     or "system")
            recent_events.append(row)

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
            "recent_events": recent_events,
        },
    )
