"""Sparpotential-Report — v0.24.4.

Computes real, data-grounded cost facts for one customer from order
history and stored supply pricing (never invents numbers), then
optionally asks the configured LLM to turn those facts into a short
sales-ready narrative. The narrative is decoration only — every number
in it must trace back to :func:`compute_savings_facts`.

No historical toner-level time series exists yet (only the latest
snapshot in ``toner_state``), so this report is deliberately scoped to
what's actually on file: order history + supply pricing. It does NOT
attempt consumption forecasting — that needs the time-series work
tracked separately.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

from sqlalchemy import select

from . import db

logger = logging.getLogger(__name__)


def compute_savings_facts(customer_id: int) -> dict[str, Any]:
    """Pure, DB-only computation — no LLM, no network. Every field here
    is directly traceable to a stored value."""
    with db.get_conn() as conn:
        orders = conn.execute(
            select(db.toner_orders.c.printer_id, db.toner_orders.c.printer_name,
                   db.toner_orders.c.color, db.toner_orders.c.sku,
                   db.toner_orders.c.quantity, db.toner_orders.c.status)
            .where(db.toner_orders.c.customer_id == customer_id)
            .where(db.toner_orders.c.status != "cancelled")
        ).fetchall()
        supplies = conn.execute(
            select(db.printer_supplies.c.printer_id, db.printer_supplies.c.color,
                   db.printer_supplies.c.sku, db.printer_supplies.c.unit_price_cents,
                   db.printer_supplies.c.supplier)
            .where(db.printer_supplies.c.customer_id == customer_id)
            .where(db.printer_supplies.c.sku != "")
        ).fetchall()
        templates = conn.execute(
            select(db.supply_templates.c.sku, db.supply_templates.c.unit_price_cents)
            .where(db.supply_templates.c.sku != "")
        ).fetchall()

    # SKU -> shared library price (fallback only).
    template_price_by_sku: dict[str, int] = {
        r.sku: r.unit_price_cents for r in templates if r.unit_price_cents}
    # (printer_id, color) -> this customer's own price for that toner
    # slot. Keyed by slot, NOT by SKU alone — the same SKU can be
    # priced differently on different printers of this customer (that
    # divergence is exactly what sku_price_variance below reports), so
    # a SKU-only lookup would silently collapse those distinct prices
    # into whichever row happened to be inserted last.
    override_price_by_slot: dict[tuple[str, str], int] = {
        (r.printer_id, r.color): r.unit_price_cents
        for r in supplies if r.unit_price_cents}

    total_spend_cents = 0
    priced_orders = 0
    unpriced_orders = 0
    for o in orders:
        price = override_price_by_slot.get((o.printer_id, o.color))
        if price is None and o.sku:
            price = template_price_by_sku.get(o.sku)
        if price is None:
            unpriced_orders += 1
            continue
        priced_orders += 1
        total_spend_cents += price * max(o.quantity, 1)

    # Same SKU, different price on file across this customer's printers
    # — direct evidence of avoidable spend (no forecasting needed: it's
    # the same physical cartridge at two different prices today).
    prices_seen: dict[str, set[int]] = defaultdict(set)
    for r in supplies:
        if r.unit_price_cents:
            prices_seen[r.sku].add(r.unit_price_cents)
    sku_price_variance = []
    for sku, prices in prices_seen.items():
        if len(prices) > 1:
            lo, hi = min(prices), max(prices)
            sku_price_variance.append({
                "sku": sku, "min_price_cents": lo, "max_price_cents": hi,
                "spread_cents": hi - lo,
            })
    sku_price_variance.sort(key=lambda x: x["spread_cents"], reverse=True)

    # Customer-specific override priced ABOVE the shared library
    # default for the identical SKU — switching to the library default
    # supplier/price is a concrete, immediately actionable saving.
    override_above_template = []
    for r in supplies:
        if not r.unit_price_cents or r.sku not in template_price_by_sku:
            continue
        tmpl_price = template_price_by_sku[r.sku]
        if r.unit_price_cents > tmpl_price:
            override_above_template.append({
                "printer_id": r.printer_id, "color": r.color, "sku": r.sku,
                "override_price_cents": r.unit_price_cents,
                "template_price_cents": tmpl_price,
                "diff_cents": r.unit_price_cents - tmpl_price,
            })
    override_above_template.sort(key=lambda x: x["diff_cents"], reverse=True)

    total_orders = len(orders)
    priced_supplies = sum(1 for r in supplies if r.unit_price_cents)
    return {
        "total_orders": total_orders,
        "priced_orders": priced_orders,
        "unpriced_orders": unpriced_orders,
        "total_spend_cents": total_spend_cents,
        "supplies_on_file": len(supplies),
        "priced_supplies": priced_supplies,
        "unpriced_supplies_pct": (
            round(100 * (1 - priced_supplies / len(supplies)))
            if supplies else 0),
        "sku_price_variance": sku_price_variance[:10],
        "override_above_template": override_above_template[:10],
    }


def generate_savings_narrative(customer_name: str, facts: dict[str, Any],
                                lang: str = "de") -> str | None:
    """Ask the configured LLM to phrase the already-computed facts as a
    short sales-ready paragraph. Returns ``None`` if the LLM isn't
    configured or the call fails — callers show the facts table either
    way, the narrative is a bonus, never a dependency."""
    from . import llm_client
    if not llm_client.is_configured():
        return None

    lang_name = {"de": "German", "en": "English", "fr": "French",
                 "it": "Italian", "es": "Spanish"}.get(lang, "English")
    system = (
        "You write short cost-savings summaries for an MSP's print-"
        "supply monitoring product, to be read aloud by a sales rep in "
        f"a customer meeting. Write in {lang_name}. Use ONLY the "
        "numbers given below — never invent, round dramatically, or "
        "estimate a number yourself. If a number is zero or a list is "
        "empty, say so plainly rather than inventing a finding. "
        "3-5 sentences, plain prose, no bullet lists, no markdown, no "
        "greeting or sign-off — just the paragraph."
    )
    import json as _json
    user = f"Customer: {customer_name}\nFacts (JSON, all amounts in cents): {_json.dumps(facts)}"
    try:
        resp = llm_client.chat(system, user)
    except llm_client.LLMError as e:
        logger.warning("[savings_report] LLM error for customer %r: %s", customer_name, e)
        return None
    return (resp.text or "").strip() or None
