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

from .. import auth, bi_client, db, printer_info, saved_views, supply_library
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

        # v0.21.0 — read from the cache only. A background warmer
        # (bi_cache.py, running every N minutes per settings.runner)
        # keeps the cache hot. This makes /toner grid render in
        # constant time regardless of Azure SQL latency, and gives the
        # operator a "warming up" banner instead of a spinning wheel
        # while the first fetch runs.
        bi_customer = bi_client.customer_for_bi(c)
        printers = bi_client.fetch_all_printer_supplies_cached_only(bi_customer)
        if printers is None:
            errors.append({"customer_id": c["id"], "customer_name": c["name"],
                           "reason": "cache_cold"})
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
    # v0.17.2: opt-in grouping — printers get partitioned into
    # sections by group_name (all "" go into a single "Ungrouped"
    # section). Toggle sits next to the view-toggle and persists
    # in the session so a reload keeps the operator's choice.
    group_by_raw = q.get("group_by", "")
    if group_by_raw in ("1", "0"):
        group_by = (group_by_raw == "1")
        try:
            request.session["toner_group_by"] = "1" if group_by else "0"
        except AssertionError:
            pass
    else:
        try:
            group_by = request.session.get("toner_group_by", "0") == "1"
        except AssertionError:
            group_by = False
    # v0.18.1: Printix Anywhere Printers are virtual (vendor="Printix"),
    # they have no toner and would clutter every list. Hide by default;
    # persist the operator's choice in the session so a reload keeps it.
    hide_anywhere_raw = q.get("hide_anywhere", "")
    if hide_anywhere_raw in ("1", "0"):
        hide_anywhere = (hide_anywhere_raw == "1")
        try:
            request.session["toner_hide_anywhere"] = "1" if hide_anywhere else "0"
        except AssertionError:
            pass
    else:
        try:
            hide_anywhere = request.session.get("toner_hide_anywhere", "1") == "1"
        except AssertionError:
            hide_anywhere = True
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

    # v0.23.5 — the v0.18.1 filter only matched vendor="Printix", but
    # Anywhere printers can also carry the real vendor (HP, Kyocera,
    # …) with the "Anywhere" marker in the model or printer_name. Do
    # the union of all three signals so "hide anywhere" hides them
    # regardless of which field Printix populated.
    def _is_anywhere_printer(r: dict) -> bool:
        vendor = (r.get("vendor") or "").strip().lower()
        model  = (r.get("model") or "").strip().lower()
        name   = (r.get("printer_name") or "").strip().lower()
        return (vendor == "printix"
                or "anywhere" in model
                or "anywhere" in name)

    anywhere_count = sum(1 for r in rows if _is_anywhere_printer(r))

    filtered = rows
    if hide_anywhere:
        filtered = [r for r in filtered if not _is_anywhere_printer(r)]
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

    # Distinct customer list for the dropdown. Populate from EVERY
    # customer the operator can see — not just the ones that
    # returned rows — so the filter is still discoverable when a
    # tenant has no BI creds yet or the BI DB is asleep.
    customer_choices = sorted(
        {(c["id"], c["name"]) for c in _visible_customers(user)},
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

    # v0.11: saved views the user can pick from (own + shared)
    views = saved_views.list_visible(user["id"], "toner")
    # Match a view when the current filter state is identical to a
    # saved one so we can highlight the active chip.
    current_filters = {
        "customer": filter_customer, "severity": filter_severity,
        "group":    filter_group,    "q":        filter_search,
        "view":     view_mode,
    }
    current_filters_norm = {k: v for k, v in current_filters.items() if v}
    active_view_id = None
    for v in views:
        if v.get("filters") == current_filters_norm:
            active_view_id = v["id"]
            break

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
            "saved_views":     views,
            "active_view_id":  active_view_id,
            "view_saved_flag": q.get("view_saved") == "1",
            "view_deleted_flag": q.get("view_deleted") == "1",
            "group_by":        group_by,
            "hide_anywhere":   hide_anywhere,
            "anywhere_count":  anywhere_count,
            # Buckets: [(group_name_or_None, [printer, printer, …]), …]
            # sorted by group name (ungrouped last). Only used when
            # group_by is True; grid.html / list.html each decide
            # whether to render buckets or the flat list.
            "grouped_printers": _bucket_by_group(filtered),
        },
    )


def _bucket_by_group(printers: list[dict]) -> list[tuple[str, list[dict]]]:
    """Partition into [(group_name, printers), …]. Ungrouped devices
    (empty group_name) go into a synthetic "" bucket rendered last."""
    buckets: dict[str, list[dict]] = {}
    for p in printers:
        g = (p.get("group_name") or "").strip()
        buckets.setdefault(g, []).append(p)
    # Named groups first, alphabetically; ungrouped last
    named = sorted((k for k in buckets if k), key=str.lower)
    if "" in buckets:
        named.append("")
    return [(g, buckets[g]) for g in named]


@router.get("/toner/diagnose", response_class=HTMLResponse,
            include_in_schema=False)
async def toner_diagnose(request: Request):
    """v0.23.6 — dump distinct (vendor, model, printer_name)
    combinations per visible customer so an operator can see what
    the BI-DB actually reports for their Anywhere queues. The
    'hide anywhere' filter can only match what's actually in the
    data — if it's not working, the operator needs to know which
    field carries the Printix-Anywhere marker in their tenant."""
    user = auth.require_user(request)
    from collections import Counter as _Counter
    diagnostics: list[dict] = []
    for c in _visible_customers(user):
        bi_customer = bi_client.customer_for_bi(c)
        printers = bi_client.fetch_all_printer_supplies_cached_only(bi_customer)
        if not printers:
            diagnostics.append({"customer_id": c["id"],
                                "customer_name": c["name"],
                                "status": "cache_cold_or_empty",
                                "rows": []})
            continue
        vendor_counts = _Counter((p.get("vendor") or "(empty)") for p in printers)
        rows = []
        seen = set()
        for p in printers:
            key = ((p.get("vendor") or "").strip(),
                    (p.get("model") or "").strip(),
                    (p.get("printer_name") or "").strip()[:60])
            if key in seen:
                continue
            seen.add(key)
            rows.append({"vendor": p.get("vendor") or "",
                          "model":  p.get("model") or "",
                          "printer_name": p.get("printer_name") or "",
                          "would_be_hidden": _looks_like_anywhere(p)})
        rows.sort(key=lambda r: (not r["would_be_hidden"],
                                   r["vendor"].lower(),
                                   r["model"].lower()))
        diagnostics.append({"customer_id": c["id"],
                            "customer_name": c["name"],
                            "status": "ok",
                            "printer_count": len(printers),
                            "vendor_counts": vendor_counts.most_common(10),
                            "rows": rows[:50]})
    return request.app.state.templates.TemplateResponse(
        "toner/diagnose.html",
        {"request": request, "lang": request.state.lang,
         "user": user, "diagnostics": diagnostics},
    )


@router.get("/toner/printer_raw", response_class=HTMLResponse,
            include_in_schema=False)
async def toner_printer_raw(request: Request):
    """v0.23.7 — SELECT * FROM dbo.printers WHERE id = ?  and render
    every field so the operator can see EXACTLY what Printix BI
    reports. If our 6-column extract misses the Anywhere marker,
    the missing field is visible here + we can extend the extract."""
    user = auth.require_user(request)
    q = request.query_params
    cust_id_raw = q.get("customer", "").strip()
    printer_id  = q.get("id", "").strip()

    customers = _visible_customers(user)
    selected_cust = None
    if cust_id_raw.isdigit():
        cid = int(cust_id_raw)
        for c in customers:
            if c["id"] == cid:
                selected_cust = c
                break

    printer_choices: list = []
    raw = None
    # v0.23.8 — if no printer_id is provided, dump the first 10 printers
    # with the full schema as JSON so the operator can paste it into a
    # ticket/message.
    bulk_json = ""
    bulk_cols: list = []
    bulk_row_count = 0
    if selected_cust:
        printer_choices = bi_client.list_printer_ids(
            bi_client.customer_for_bi(selected_cust), limit=200)
        if printer_id:
            raw = bi_client.fetch_printer_raw(
                bi_client.customer_for_bi(selected_cust), printer_id)
        else:
            dump = bi_client.fetch_printers_raw(
                bi_client.customer_for_bi(selected_cust), limit=10)
            if dump:
                import json as _json
                bulk_cols = dump["columns"]
                bulk_row_count = len(dump["rows"])
                bulk_json = _json.dumps(
                    dump, indent=2, ensure_ascii=False, default=str)

    return request.app.state.templates.TemplateResponse(
        "toner/printer_raw.html",
        {"request": request, "lang": request.state.lang, "user": user,
         "customers": customers, "selected_cust": selected_cust,
         "printer_choices": printer_choices,
         "printer_id": printer_id, "raw": raw,
         "bulk_json": bulk_json, "bulk_cols": bulk_cols,
         "bulk_row_count": bulk_row_count},
    )


def _looks_like_anywhere(p: dict) -> bool:
    """Same predicate as v0.23.5 hide-Anywhere filter — kept in one
    place so the diagnose view can flag exactly what would be hidden."""
    vendor = (p.get("vendor") or "").strip().lower()
    model  = (p.get("model") or "").strip().lower()
    name   = (p.get("printer_name") or "").strip().lower()
    return (vendor == "printix"
            or "anywhere" in model
            or "anywhere" in name)


@router.post("/toner/refresh", include_in_schema=False)
async def toner_refresh(request: Request):
    """Drop the cache for the customers the user can see, then do one
    synchronous fetch so /toner comes back with fresh data (not a
    "cache_cold" banner). Cheap way to force a fresh BI-DB pull
    without waiting for the background warmer."""
    user = auth.require_user(request)
    for c in _visible_customers(user):
        bi_client.invalidate_customer_cache(c["id"])
        # v0.21.0 — kick off a synchronous refetch so the user's next
        # GET /toner reads from a hot cache. Errors are silently
        # swallowed — the render path handles the fallback via the
        # cache_cold banner.
        try:
            bi_customer = bi_client.customer_for_bi(c)
            bi_client.fetch_all_printer_supplies(bi_customer)
        except Exception:  # noqa: BLE001
            pass
    db.audit(user["id"], "toner.cache_flushed")
    return RedirectResponse("/toner", status_code=303)
