"""Flexible reporting — v0.24.36.

Four report categories, each a pure ``compute_*_facts(customer_ids,
date_from, date_to)`` function that only reads TonerWatch's own
tables (order history + supply pricing + toner events) and returns a
plain, JSON-serializable dict. No live BI-database queries — that
would need a per-customer round trip to Printix's own Azure SQL and
risks the same timeouts ``bi_client.py`` already works around for
routine polling; a report should render fast and consistently instead.

There is deliberately no continuous toner-level time series here.
TonerWatch's own DB only ever holds the LATEST reading per slot
(``toner_state``, overwritten every poll) — see ``savings_report.py``
for the same constraint. "Consumption" is therefore measured the
honest way: cartridges that actually shipped (order status
delivered/installed) in the window, not a level curve.

Same rule as every other AI feature in this codebase: the LLM only
ever phrases numbers that were already computed in Python — it is
never the source of a number that ends up in a report.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime
from typing import Any

from sqlalchemy import and_, select

from . import db

logger = logging.getLogger(__name__)

REPORT_CATEGORIES = ("orders", "consumption", "device_health",
                     "supplier_performance", "active_users")

# Orders in these statuses represent toner that actually left the
# shelf — the honest proxy for "consumed" without a level time series.
_CONSUMED_STATUSES = ("delivered", "installed")


def _parse_dt(text: str) -> datetime | None:
    """Best-effort parse of the free-text timestamp columns this app
    stores (SQLite/MSSQL current_timestamp defaults, both close to but
    not always exactly ISO 8601). Returns None rather than raising —
    a single bad/blank timestamp shouldn't sink an entire aggregate."""
    if not text:
        return None
    text = text.strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:26], fmt)
        except ValueError:
            continue
    return None


def _date_bounds(date_from: str, date_to: str) -> tuple[str, str]:
    """Normalise the inclusive [date_from, date_to] UI range (both
    'YYYY-MM-DD') into TEXT-comparable bounds against the app's
    current_timestamp-formatted columns — lexicographic comparison on
    ISO-ish strings sorts correctly without a dialect-specific date
    function, the same trick the rest of this codebase relies on."""
    return date_from, date_to + " 23:59:59"


def _customer_names(customer_ids: list[int]) -> dict[int, str]:
    if not customer_ids:
        return {}
    with db.get_conn() as conn:
        rows = conn.execute(
            select(db.customers.c.id, db.customers.c.name)
            .where(db.customers.c.id.in_(customer_ids))
        ).all()
    return {r.id: r.name for r in rows}


def _price_and_supplier_lookup(
    customer_ids: list[int],
) -> tuple[dict[tuple[int, str, str], int], dict[str, int],
           dict[tuple[int, str, str], int], dict[int, str]]:
    """Bulk-fetch pricing + supplier linkage once, shared by every
    report category that needs spend/supplier numbers — same
    override-wins-over-template precedence as ``savings_report.py``
    and ``supply_library.resolve_supply()``, just batched instead of
    resolved per-order to avoid an N+1 query pattern over what can be
    thousands of orders in a wide date range.

    Returns:
        override_price:   (customer_id, printer_id, color) -> cents
        template_price:    sku -> cents
        override_supplier: (customer_id, printer_id, color) -> supplier_id
        supplier_names:    supplier_id -> name
    """
    with db.get_conn() as conn:
        supplies = conn.execute(
            select(db.printer_supplies.c.customer_id, db.printer_supplies.c.printer_id,
                   db.printer_supplies.c.color, db.printer_supplies.c.unit_price_cents,
                   db.printer_supplies.c.supplier_id)
            .where(db.printer_supplies.c.customer_id.in_(customer_ids))
        ).all() if customer_ids else []
        templates = conn.execute(
            select(db.supply_templates.c.sku, db.supply_templates.c.unit_price_cents)
            .where(db.supply_templates.c.sku != "")
        ).all()
        supplier_rows = conn.execute(select(db.suppliers.c.id, db.suppliers.c.name)).all()

    override_price = {(r.customer_id, r.printer_id, r.color): r.unit_price_cents
                       for r in supplies if r.unit_price_cents}
    template_price = {r.sku: r.unit_price_cents for r in templates if r.unit_price_cents}
    override_supplier = {(r.customer_id, r.printer_id, r.color): r.supplier_id
                          for r in supplies if r.supplier_id}
    supplier_names = {r.id: r.name for r in supplier_rows}
    return override_price, template_price, override_supplier, supplier_names


def _fetch_orders(customer_ids: list[int], date_from: str, date_to: str) -> list[Any]:
    if not customer_ids:
        return []
    lo, hi = _date_bounds(date_from, date_to)
    with db.get_conn() as conn:
        return conn.execute(
            select(db.toner_orders)
            .where(and_(
                db.toner_orders.c.customer_id.in_(customer_ids),
                db.toner_orders.c.ordered_at >= lo,
                db.toner_orders.c.ordered_at <= hi,
            ))
        ).all()


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

def compute_orders_facts(customer_ids: list[int], date_from: str, date_to: str,
                         ) -> dict[str, Any]:
    orders = _fetch_orders(customer_ids, date_from, date_to)
    customer_names = _customer_names(customer_ids)

    by_status: dict[str, int] = defaultdict(int)
    by_customer: dict[str, dict[str, Any]] = defaultdict(lambda: {"orders": 0, "quantity": 0})
    by_printer: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"printer_name": "", "orders": 0, "quantity": 0})
    sku_counts: dict[str, dict[str, Any]] = defaultdict(lambda: {"orders": 0, "quantity": 0})
    fulfillment_days: list[float] = []
    total_quantity = 0

    for o in orders:
        by_status[o.status] += 1
        total_quantity += o.quantity
        cname = customer_names.get(o.customer_id, f"#{o.customer_id}")
        by_customer[cname]["orders"] += 1
        by_customer[cname]["quantity"] += o.quantity
        by_printer[o.printer_id]["printer_name"] = o.printer_name or o.printer_id
        by_printer[o.printer_id]["orders"] += 1
        by_printer[o.printer_id]["quantity"] += o.quantity
        if o.sku:
            sku_counts[o.sku]["orders"] += 1
            sku_counts[o.sku]["quantity"] += o.quantity
        if o.status in _CONSUMED_STATUSES:
            started, ended = _parse_dt(o.ordered_at), _parse_dt(o.closed_at)
            if started and ended and ended >= started:
                fulfillment_days.append((ended - started).total_seconds() / 86400)

    top_printers = sorted(by_printer.items(), key=lambda kv: kv[1]["quantity"], reverse=True)[:10]
    top_skus = sorted(sku_counts.items(), key=lambda kv: kv[1]["quantity"], reverse=True)[:10]

    return {
        "total_orders": len(orders),
        "total_quantity": total_quantity,
        "by_status": dict(by_status),
        "by_customer": dict(by_customer),
        "top_printers": [{"printer_id": pid, **v} for pid, v in top_printers],
        "top_skus": [{"sku": sku, **v} for sku, v in top_skus],
        "avg_fulfillment_days": (
            round(sum(fulfillment_days) / len(fulfillment_days), 1)
            if fulfillment_days else None),
        "fulfilled_count": len(fulfillment_days),
    }


# ---------------------------------------------------------------------------
# Consumption
# ---------------------------------------------------------------------------

def compute_consumption_facts(customer_ids: list[int], date_from: str, date_to: str,
                              ) -> dict[str, Any]:
    orders = [o for o in _fetch_orders(customer_ids, date_from, date_to)
              if o.status in _CONSUMED_STATUSES]
    customer_names = _customer_names(customer_ids)
    override_price, template_price, _, _ = _price_and_supplier_lookup(customer_ids)

    by_color: dict[str, int] = defaultdict(int)
    by_customer: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"units": 0, "spend_cents": 0})
    by_printer: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"printer_name": "", "units": 0, "spend_cents": 0})
    total_units = 0
    total_spend_cents = 0
    priced_units = 0

    for o in orders:
        total_units += o.quantity
        cname = customer_names.get(o.customer_id, f"#{o.customer_id}")
        by_color[o.color] += o.quantity
        by_customer[cname]["units"] += o.quantity
        by_printer[o.printer_id]["printer_name"] = o.printer_name or o.printer_id
        by_printer[o.printer_id]["units"] += o.quantity

        price = override_price.get((o.customer_id, o.printer_id, o.color))
        if price is None and o.sku:
            price = template_price.get(o.sku)
        if price is not None:
            spend = price * max(o.quantity, 1)
            total_spend_cents += spend
            priced_units += o.quantity
            by_customer[cname]["spend_cents"] += spend
            by_printer[o.printer_id]["spend_cents"] += spend

    top_printers = sorted(by_printer.items(), key=lambda kv: kv[1]["units"], reverse=True)[:10]

    return {
        "total_units": total_units,
        "total_spend_cents": total_spend_cents,
        "priced_units": priced_units,
        "unpriced_units": total_units - priced_units,
        "by_color": dict(by_color),
        "by_customer": dict(by_customer),
        "top_printers": [{"printer_id": pid, **v} for pid, v in top_printers],
    }


# ---------------------------------------------------------------------------
# Device health
# ---------------------------------------------------------------------------

def compute_device_health_facts(customer_ids: list[int], date_from: str, date_to: str,
                                ) -> dict[str, Any]:
    if not customer_ids:
        return {"total_anomalies": 0, "by_customer": {}, "high_turnover_printers": [],
                "recent_anomalies": []}
    lo, hi = _date_bounds(date_from, date_to)
    customer_names = _customer_names(customer_ids)
    with db.get_conn() as conn:
        events = conn.execute(
            select(db.toner_events)
            .where(and_(
                db.toner_events.c.customer_id.in_(customer_ids),
                db.toner_events.c.kind == "toner.anomaly",
                db.toner_events.c.created_at >= lo,
                db.toner_events.c.created_at <= hi,
            ))
            .order_by(db.toner_events.c.created_at.desc())
        ).all()

    by_customer: dict[str, int] = defaultdict(int)
    by_printer: dict[str, int] = defaultdict(int)
    recent: list[dict[str, Any]] = []
    for e in events:
        cname = customer_names.get(e.customer_id, f"#{e.customer_id}")
        by_customer[cname] += 1
        by_printer[e.printer_id] += 1
        if len(recent) < 20:
            try:
                meta = json.loads(e.meta_json) if e.meta_json else {}
            except json.JSONDecodeError:
                meta = {}
            recent.append({
                "customer": cname, "printer_id": e.printer_id, "color": e.color,
                "level": e.level, "prev_level": meta.get("prev_level"),
                "anomaly_kind": meta.get("kind", ""), "created_at": e.created_at,
            })

    # Printers with unusually many orders in the window — a coarse,
    # order-based proxy for "device that might need a look", since a
    # continuous fault/level history isn't stored (see module docstring).
    orders = _fetch_orders(customer_ids, date_from, date_to)
    order_counts: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"printer_name": "", "orders": 0})
    for o in orders:
        order_counts[o.printer_id]["printer_name"] = o.printer_name or o.printer_id
        order_counts[o.printer_id]["orders"] += 1
    high_turnover = sorted(
        ({"printer_id": pid, **v} for pid, v in order_counts.items() if v["orders"] >= 3),
        key=lambda x: x["orders"], reverse=True)[:10]

    return {
        "total_anomalies": len(events),
        "by_customer": dict(by_customer),
        "by_printer": dict(by_printer),
        "recent_anomalies": recent,
        "high_turnover_printers": high_turnover,
    }


# ---------------------------------------------------------------------------
# Supplier performance
# ---------------------------------------------------------------------------

def compute_supplier_performance_facts(customer_ids: list[int], date_from: str,
                                       date_to: str) -> dict[str, Any]:
    orders = _fetch_orders(customer_ids, date_from, date_to)
    override_price, template_price, override_supplier, supplier_names = \
        _price_and_supplier_lookup(customer_ids)

    by_supplier: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"orders": 0, "quantity": 0, "spend_cents": 0, "_fulfillment_days": []})
    unresolved_orders = 0

    for o in orders:
        supplier_id = override_supplier.get((o.customer_id, o.printer_id, o.color))
        supplier = supplier_names.get(supplier_id, "") if supplier_id else ""
        if not supplier:
            unresolved_orders += 1
            continue
        row = by_supplier[supplier]
        row["orders"] += 1
        row["quantity"] += o.quantity
        price = override_price.get((o.customer_id, o.printer_id, o.color))
        if price is None and o.sku:
            price = template_price.get(o.sku)
        if price is not None:
            row["spend_cents"] += price * max(o.quantity, 1)
        if o.status in _CONSUMED_STATUSES:
            started, ended = _parse_dt(o.ordered_at), _parse_dt(o.closed_at)
            if started and ended and ended >= started:
                row["_fulfillment_days"].append((ended - started).total_seconds() / 86400)

    ranked = []
    for name, row in by_supplier.items():
        days = row.pop("_fulfillment_days")
        ranked.append({
            "supplier": name, **row,
            "avg_fulfillment_days": round(sum(days) / len(days), 1) if days else None,
        })
    ranked.sort(key=lambda x: x["spend_cents"], reverse=True)

    return {
        "suppliers": ranked,
        "unresolved_orders": unresolved_orders,
    }


# ---------------------------------------------------------------------------
# Active Printix users — v0.24.42
# ---------------------------------------------------------------------------

def compute_active_users_facts(customer_ids: list[int], date_from: str,
                               date_to: str) -> dict[str, Any]:
    """Active Printix users per customer, from the cached BI-DB
    snapshot (bi_client.fetch_active_users_cached_only) — same
    "no live BI-DB query inside a report" rule as every other
    category here (see module docstring). ``date_from``/``date_to``
    are accepted for signature consistency with the other compute_*
    functions but unused: this is a live snapshot, not a
    time-windowed aggregate.

    Always returns a per-customer summary (name + count). The full
    per-user list (name/email/department) is populated ONLY when
    exactly one customer is in scope — a multi-customer report
    deliberately never dumps every visible customer's user directory
    into a single table."""
    from . import bi_client

    if not customer_ids:
        return {"total_active_users": 0, "by_customer": [],
                "users_detail": None, "detail_customer_name": None}

    with db.get_conn() as conn:
        rows = conn.execute(
            select(db.customers.c.id, db.customers.c.name,
                   db.customers.c.sql_server, db.customers.c.sql_database,
                   db.customers.c.sql_username)
            .where(db.customers.c.id.in_(customer_ids))
        ).all()

    by_customer: list[dict[str, Any]] = []
    total_active_users = 0
    users_detail: list[dict[str, Any]] | None = None
    detail_customer_name: str | None = None
    single_customer = len(customer_ids) == 1

    for r in rows:
        cust = {"id": r.id, "sql_server": r.sql_server,
                "sql_database": r.sql_database, "sql_username": r.sql_username}
        users = bi_client.fetch_active_users_cached_only(cust)
        count = len(users) if users is not None else None
        by_customer.append({"customer_id": r.id, "customer_name": r.name,
                            "active_users": count})
        if count:
            total_active_users += count
        if single_customer and users is not None:
            users_detail = sorted(
                users, key=lambda u: ((u.get("name") or "").lower(),
                                       (u.get("email") or "").lower()))
            detail_customer_name = r.name

    by_customer.sort(key=lambda x: x["customer_name"].lower())
    return {
        "total_active_users": total_active_users,
        "by_customer": by_customer,
        "users_detail": users_detail,
        "detail_customer_name": detail_customer_name,
    }


# ---------------------------------------------------------------------------
# AI narrative — decoration only, never a data source (see module docstring)
# ---------------------------------------------------------------------------

def generate_report_narrative(scope_label: str, date_from: str, date_to: str,
                              facts_by_category: dict[str, Any],
                              lang: str = "de") -> tuple[str | None, str | None]:
    """``facts_by_category`` — e.g. {"orders": {...}, "consumption": {...}}
    — only the categories the operator actually selected. Returns
    ``(narrative, error)`` — callers always show the underlying tables
    regardless, this is a bonus paragraph. ``error`` is ``None`` when
    the LLM isn't configured at all, or set on the actual provider
    error text when it IS configured but the call failed, so the UI
    doesn't tell an operator "not configured" when it's really a
    provider timeout / quota / bad-response issue."""
    from . import llm_client
    if not llm_client.is_configured():
        return None, None

    lang_name = {"de": "German", "en": "English", "fr": "French",
                 "it": "Italian", "es": "Spanish"}.get(lang, "English")
    system = (
        "You write short executive-summary paragraphs for an MSP's "
        "print-supply monitoring product, suitable for a quarterly "
        f"business review with a customer. Write in {lang_name}. Use "
        "ONLY the numbers given below — never invent, round "
        "dramatically, or estimate a number yourself. If a section is "
        "empty or all-zero, mention that plainly rather than inventing "
        "a finding. 4-6 sentences, plain prose, no bullet lists, no "
        "markdown, no greeting or sign-off — just the paragraph."
    )
    user = (
        f"Scope: {scope_label}\nPeriod: {date_from} to {date_to}\n"
        f"Facts by category (JSON, all money amounts in cents): "
        f"{json.dumps(facts_by_category)}"
    )
    try:
        resp = llm_client.chat(system, user)
    except llm_client.LLMError as e:
        logger.warning("[reports] LLM error for scope %r: %s", scope_label, e)
        return None, str(e)
    return (resp.text or "").strip() or None, None
