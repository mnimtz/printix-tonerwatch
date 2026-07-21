"""Real dashboard replacing the coming-soon stub — cross-customer overview.

Shows four stat tiles at the top (customers, printers, critical, warn) plus
a per-customer status card list. Every stat is derived from the cached BI
snapshot to keep the page snappy; the fresh pull happens on /toner.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import desc, func, select

from .. import auth, bi_client, dashboard_greeting, db, toner_alerts
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
    total_active_users = 0

    for c in customers:
        bi_customer = bi_client.customer_for_bi(c)
        printers = bi_client.fetch_all_printer_supplies_cached_only(bi_customer)
        active_users = bi_client.fetch_active_users_cached_only(bi_customer)
        if active_users is not None:
            total_active_users += len(active_users)
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

    ok_count = sum(c["ok"] for c in per_customer_stats)

    # v0.24.10 — urgency-first ordering: customers with a critical
    # supply float to the top (worst customer's worst count first),
    # then warn-only, then fully-healthy, then no-data/no-credentials
    # last — those need a setup action, not a toner check.
    def _urgency_key(c: dict) -> tuple:
        if not c["has_data"]:
            return (3, c["name"])
        if c["critical"]:
            return (0, -c["critical"], -c["warn"], c["name"])
        if c["warn"]:
            return (1, -c["warn"], c["name"])
        return (2, c["name"])
    per_customer_stats.sort(key=_urgency_key)

    # Names of the (up to 2) worst-off customers, for the one-line
    # greeting summary — "Acme GmbH and Beta AG need a look first".
    urgent_names = [c["name"] for c in per_customer_stats if c["critical"]][:2]

    # v0.24.45 — same idea but with real counts, not just names, so
    # the AI greeting can say WHERE the problems are and how many
    # ("Acme has 3 critical, 1 warn") instead of a bare total.
    # per_customer_stats is already urgency-sorted above.
    problem_customers = [
        {"name": c["name"], "critical": c["critical"], "warn": c["warn"]}
        for c in per_customer_stats if c["critical"] or c["warn"]
    ][:3]

    # v0.24.13 — AI-phrased greeting, built from the exact same facts
    # as the static sentence below plus recent cross-customer
    # anomalies, so it can name a specific situation instead of just
    # totals. Cached ~hourly inside generate_greeting(); returns None
    # (falls back to the static sentence in the template) whenever the
    # LLM isn't configured, errors, or is slow.
    recent_anomalies = toner_alerts.list_recent_anomalies_multi(
        [c["id"] for c in customers], limit=5)
    ai_greeting = dashboard_greeting.generate_greeting(
        user["id"], user.get("name") or user.get("email") or "",
        {"customers": len(customers), "printers": total_printers,
         "critical": critical_count, "warn": warn_count},
        urgent_names, recent_anomalies, lang=request.state.lang,
        problem_customers=problem_customers)

    # v0.24.10 — cache freshness for the greeting line. toner_state is
    # the only place a "last seen" timestamp exists; MAX across every
    # customer the user can see is a reasonable proxy for "how stale
    # could this page be" without a live BI round-trip.
    visible_ids = [c["id"] for c in customers]
    last_seen_at = None
    if visible_ids:
        with db.get_conn() as conn:
            last_seen_at = conn.execute(
                select(func.max(db.toner_state.c.last_seen_at))
                .where(db.toner_state.c.customer_id.in_(visible_ids))
            ).scalar()

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
                "ok":        ok_count,
                "unknown":   unknown_count,
                "active_users": total_active_users,
            },
            "per_customer": per_customer_stats,
            "urgent_names": urgent_names,
            "ai_greeting": ai_greeting,
            "last_seen_at": last_seen_at or "",
            "recent_events": recent_events,
        },
    )
