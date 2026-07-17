"""Supply library — model templates + per-printer overrides.

Two data sources feed one resolver. Templates ("Vorlagen") are keyed
by (printer_model, color) and are shared across every customer — one
SKU list per HP LaserJet family is enough. Overrides are keyed by
(customer_id, printer_id, color) and win over templates when a
customer needs a different SKU, price, or supplier for one specific
device (rebranded cartridges, framework contracts, "please always
order the XL cartridge for THIS printer").

The resolver is what the toner grid and the alert mail call — it
returns the effective supply record for a printer's toner slot, or
None when nothing matches. Downstream code should treat "no match"
as "no order info to display" and never surface an unresolved slot.
"""

from __future__ import annotations

from typing import Any, Iterable

from sqlalchemy import and_, delete, func, insert, or_, select, update
from sqlalchemy.exc import IntegrityError

from . import db


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------

def resolve_supply(
    customer_id: int,
    printer_id: str,
    printer_model: str | None,
    color: str,
) -> dict[str, Any] | None:
    """Return the supply record for one (customer, printer, color) slot.

    Precedence: per-printer override → global model template → None.

    Returns a normalized dict — the same shape either way, so the
    caller doesn't need to know which source it came from. A `source`
    key discriminates ("override" | "template") in case the UI wants
    to badge overrides as "custom for this device".
    """
    color = _normalize_color(color)
    if not color:
        return None

    with db.get_conn() as conn:
        # 1) Per-printer override
        row = conn.execute(
            select(db.printer_supplies).where(
                and_(db.printer_supplies.c.customer_id == customer_id,
                     db.printer_supplies.c.printer_id == printer_id,
                     db.printer_supplies.c.color == color)
            )
        ).first()
        if row is not None:
            d = db._row_to_dict(row)
            d["source"] = "override"
            return d

        # 2) Global template by printer_model
        if printer_model:
            row = conn.execute(
                select(db.supply_templates).where(
                    and_(db.supply_templates.c.printer_model == printer_model.strip(),
                         db.supply_templates.c.color == color)
                )
            ).first()
            if row is not None:
                d = db._row_to_dict(row)
                d["source"] = "template"
                return d

    return None


def resolve_all_for_printer(
    customer_id: int,
    printer_id: str,
    printer_model: str | None,
    colors: Iterable[str],
) -> dict[str, dict[str, Any] | None]:
    """Bulk-resolve every color slot for one printer. Returns
    ``{color: supply|None}``. Cheap because the tables are small
    (a few hundred templates at most) and hit only two indexes."""
    out: dict[str, dict[str, Any] | None] = {}
    for c in colors:
        out[c] = resolve_supply(customer_id, printer_id, printer_model, c)
    return out


# ---------------------------------------------------------------------------
# Templates (model-level, shared across customers)
# ---------------------------------------------------------------------------

def list_templates() -> list[dict[str, Any]]:
    """Every template, ordered by model then color (K → C → M → Y → other)."""
    order = {"K": 0, "C": 1, "M": 2, "Y": 3, "other": 4}
    with db.get_conn() as conn:
        rows = conn.execute(select(db.supply_templates)).all()
    result = [db._row_to_dict(r) for r in rows]
    result.sort(key=lambda r: (r["printer_model"].lower(), order.get(r["color"], 9)))
    return result


def get_template(template_id: int) -> dict[str, Any] | None:
    with db.get_conn() as conn:
        row = conn.execute(
            select(db.supply_templates).where(db.supply_templates.c.id == template_id)
        ).first()
    return db._row_to_dict(row) if row else None


def upsert_template(
    template_id: int | None,
    fields: dict[str, Any],
    updated_by_user_id: int,
) -> tuple[int, str | None]:
    """Insert or update a template. Returns (id, error) — error is
    ``"duplicate_model_color"`` when the (model, color) uniqueness
    constraint would be violated, otherwise ``None``."""
    fields = _sanitise_template_fields(fields)
    fields["updated_by_user_id"] = updated_by_user_id
    fields["updated_at"] = func.current_timestamp()

    with db.get_conn() as conn:
        try:
            if template_id is None:
                result = conn.execute(insert(db.supply_templates).values(**fields))
                new_id = result.inserted_primary_key[0]
                return int(new_id), None
            conn.execute(
                update(db.supply_templates)
                .where(db.supply_templates.c.id == template_id)
                .values(**fields)
            )
            return template_id, None
        except IntegrityError as e:
            # SQLite reports "UNIQUE constraint failed:
            # supply_templates.printer_model, supply_templates.color".
            # Postgres/MSSQL use the named constraint. Detect both.
            msg = str(e).lower()
            if ("uq_supply_templates_model_color" in msg
                    or ("unique" in msg and "printer_model" in msg
                        and "color" in msg)):
                return template_id or 0, "duplicate_model_color"
            raise


def delete_template(template_id: int) -> None:
    with db.get_conn() as conn:
        conn.execute(
            delete(db.supply_templates).where(db.supply_templates.c.id == template_id)
        )


# ---------------------------------------------------------------------------
# Per-printer overrides
# ---------------------------------------------------------------------------

def list_overrides_for_customer(customer_id: int) -> list[dict[str, Any]]:
    """All overrides for one customer, keyed printer_id then color."""
    order = {"K": 0, "C": 1, "M": 2, "Y": 3, "other": 4}
    with db.get_conn() as conn:
        rows = conn.execute(
            select(db.printer_supplies)
            .where(db.printer_supplies.c.customer_id == customer_id)
        ).all()
    result = [db._row_to_dict(r) for r in rows]
    result.sort(key=lambda r: (r["printer_id"], order.get(r["color"], 9)))
    return result


def get_overrides_for_printer(
    customer_id: int,
    printer_id: str,
) -> dict[str, dict[str, Any]]:
    """Overrides for one printer, keyed by color."""
    with db.get_conn() as conn:
        rows = conn.execute(
            select(db.printer_supplies).where(
                and_(db.printer_supplies.c.customer_id == customer_id,
                     db.printer_supplies.c.printer_id == printer_id)
            )
        ).all()
    return {r.color: db._row_to_dict(r) for r in rows}


def upsert_override(
    customer_id: int,
    printer_id: str,
    color: str,
    fields: dict[str, Any],
    updated_by_user_id: int,
) -> None:
    """Insert-or-replace one override slot. Empty SKU + empty
    supplier_url + empty description are treated as "clear this
    override" and delete the row instead — keeps the table small and
    makes the resolver fall back to the template automatically."""
    color = _normalize_color(color)
    if not color:
        raise ValueError(f"unknown color: {color!r}")
    fields = _sanitise_override_fields(fields)

    is_empty = not any([
        fields.get("sku"), fields.get("supplier_url"),
        fields.get("description"), fields.get("notes"),
        fields.get("supplier_id"),
    ])
    if is_empty:
        delete_override(customer_id, printer_id, color)
        return

    fields["customer_id"] = customer_id
    fields["printer_id"] = printer_id
    fields["color"] = color
    fields["updated_by_user_id"] = updated_by_user_id
    fields["updated_at"] = func.current_timestamp()

    with db.get_conn() as conn:
        # Portable upsert: delete-then-insert. The PK is
        # (customer_id, printer_id, color) so this is idempotent.
        conn.execute(
            delete(db.printer_supplies).where(
                and_(db.printer_supplies.c.customer_id == customer_id,
                     db.printer_supplies.c.printer_id == printer_id,
                     db.printer_supplies.c.color == color)
            )
        )
        conn.execute(insert(db.printer_supplies).values(**fields))


def delete_override(customer_id: int, printer_id: str, color: str | None = None) -> None:
    """Delete one override (color=…) or every override for a printer."""
    with db.get_conn() as conn:
        cond = and_(
            db.printer_supplies.c.customer_id == customer_id,
            db.printer_supplies.c.printer_id == printer_id,
        )
        if color:
            cond = and_(cond, db.printer_supplies.c.color == color)
        conn.execute(delete(db.printer_supplies).where(cond))


# ---------------------------------------------------------------------------
# Field helpers
# ---------------------------------------------------------------------------

_ALLOWED_COLORS = ("K", "C", "M", "Y", "other")


def ai_suggest_supply(printer_model: str,
                       color: str) -> dict[str, Any] | None:
    """v0.20.0 — ask the configured LLM for the OEM cartridge SKU +
    description for one printer/colour combo. Returns a dict of
    {sku, description, manufacturer, yield_pages, provider, model}
    on success, or ``None`` if LLM isn't configured or the response
    can't be parsed. Shared by /supplies/ai/suggest (UI) and the
    toner alert runner when auto-drafting for a printer whose
    supply template has no SKU on file."""
    from . import llm_client
    if not llm_client.is_configured():
        return None
    color = (color or "K").strip().upper()
    if color not in ("K", "C", "M", "Y", "OTHER"):
        color = "K"
    color_word = {"K": "black", "C": "cyan", "M": "magenta",
                  "Y": "yellow", "OTHER": "other"}[color]

    system = (
        "You are a printer-supply lookup assistant. Given a printer "
        "model and a toner colour, return the OEM cartridge SKU and "
        "a one-line description. Never invent numbers you're unsure "
        "about — if you don't know a value, return null. "
        "Reply with ONE JSON object only, no prose, no code fences: "
        "{\"sku\": \"…\", \"description\": \"…\", "
        "\"manufacturer\": \"…\", \"yield_pages\": 12345}"
    )
    user = (f"Printer model: {printer_model}\n"
            f"Toner slot colour: {color_word}\n"
            "Return the OEM cartridge (not a compatible / generic).")

    try:
        resp = llm_client.chat(system, user)
    except llm_client.LLMError as e:
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "[ai_suggest_supply] LLM error for %s/%s: %s",
            printer_model, color, e)
        return None

    import json as _json
    raw = (resp.text or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:].lstrip("\n")
    try:
        data = _json.loads(raw)
    except _json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return {
        "sku":          (data.get("sku") or "").strip() or None,
        "description":  (data.get("description") or "").strip() or None,
        "manufacturer": (data.get("manufacturer") or "").strip() or None,
        "yield_pages":  _clean_int_or_none(data.get("yield_pages")),
        "provider":     resp.provider,
        "model":        resp.model,
    }


def ai_suggest_supply_set(printer_model: str) -> dict[str, Any] | None:
    """v0.24.0 — one LLM call that determines whether ``printer_model``
    is monochrome or color (CMYK) and returns OEM SKU + description +
    manufacturer + yield for every toner slot it actually has. Backs
    the "Neues Toner-Set" flow: the operator picks a model once, the
    UI auto-checks C/M/Y (or leaves them off for a mono device) and
    fills every enabled row in one round-trip instead of four.

    Returns::

        {"is_color": bool,
         "slots": {"K": {"sku":.., "description":.., "manufacturer":..,
                          "yield_pages":..}, "C": {...}, "M": {...}, "Y": {...}},
         "provider": "...", "model": "..."}

    ``slots`` only contains C/M/Y when ``is_color`` is true. Returns
    ``None`` if the LLM isn't configured or the response can't be
    parsed — caller falls back to the per-color ai_suggest_supply()
    flow or plain manual entry."""
    from . import llm_client
    if not llm_client.is_configured():
        return None

    system = (
        "You are a printer-supply lookup assistant. Given a printer "
        "model, first determine whether it is a MONOCHROME-only "
        "device (black toner only) or a COLOR device (CMYK — "
        "black + cyan + magenta + yellow) — this is only a hint for "
        "the UI, not a reason to omit data. Then return the OEM "
        "cartridge SKU, a one-line description, manufacturer, and "
        "page yield for ALL FOUR toner slots K/C/M/Y, even for a "
        "monochrome device (leave C/M/Y null in that case) — the "
        "operator may still want them filled in for a shared toner "
        "family or a variant of this model. Never invent numbers "
        "you're unsure about — return null for any field you don't "
        "know. Reply with ONE JSON object only, no prose, no code "
        "fences: {\"is_color\": true|false, \"slots\": {"
        "\"K\": {\"sku\": \"…\", \"description\": \"…\", "
        "\"manufacturer\": \"…\", \"yield_pages\": 12345}, "
        "\"C\": {…}, \"M\": {…}, \"Y\": {…}}} "
        "— always include all four slot keys."
    )
    user = (f"Printer model: {printer_model}\n"
            "Return the OEM cartridges (not compatible/generic) for "
            "every toner slot this model actually has.")

    try:
        resp = llm_client.chat(system, user)
    except llm_client.LLMError as e:
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "[ai_suggest_supply_set] LLM error for %s: %s",
            printer_model, e)
        return None

    import json as _json
    raw = (resp.text or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:].lstrip("\n")
    try:
        data = _json.loads(raw)
    except _json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None

    is_color = bool(data.get("is_color"))
    raw_slots = data.get("slots") or {}
    if not isinstance(raw_slots, dict):
        raw_slots = {}

    # v0.24.15 fix: always parse every color the LLM was asked for,
    # regardless of its own is_color guess — that flag is only a UI
    # hint for which checkboxes to default to, never a reason to
    # discard C/M/Y data the model actually returned. Previously this
    # was gated on `is_color`, so a printer the LLM misjudged as mono
    # silently lost its C/M/Y slots even if the operator had checked
    # every color box by hand.
    slots: dict[str, dict[str, Any]] = {}
    for c in ("K", "C", "M", "Y"):
        s = raw_slots.get(c)
        if not isinstance(s, dict):
            s = {}
        slots[c] = {
            "sku":          (s.get("sku") or "").strip() or None,
            "description":  (s.get("description") or "").strip() or None,
            "manufacturer": (s.get("manufacturer") or "").strip() or None,
            "yield_pages":  _clean_int_or_none(s.get("yield_pages")),
        }

    return {
        "is_color": is_color,
        "slots":    slots,
        "provider": resp.provider,
        "model":    resp.model,
    }


def ai_suggest_order_mail(order: dict[str, Any], supply: dict[str, Any] | None,
                          customer_name: str, lang: str = "de",
                          contact: dict[str, Any] | None = None) -> dict[str, str] | None:
    """v0.24.6 — draft a ready-to-send supplier order email for one
    order row. The LLM only writes the prose; every fact in it (SKU,
    quantity, printer, customer reference) is handed in already
    resolved — it's told explicitly not to invent anything it isn't
    given. TonerWatch never sends this itself: the operator copies it
    into their own mail client, so there's no new supplier-facing
    sending surface to secure or misconfigure.

    ``contact`` — v0.24.14 — is the resolved supplier record from
    ``suppliers.resolve_supplier_contact()``: the target order-mailbox
    and this customer's account number with that supplier, when the
    SKU is linked to a formal supplier record. When given, the
    customer number is handed to the LLM as a fact to mention (most
    distributors expect it in the mail body); the target address
    itself is returned alongside the draft rather than put in the
    prose, so the operator's own mail client addresses the message.

    Returns ``{"subject": .., "body": ..}`` or ``None`` if the LLM
    isn't configured or the call fails."""
    from . import llm_client
    if not llm_client.is_configured():
        return None

    sku         = (order.get("sku") or (supply or {}).get("sku") or "").strip()
    description = (supply or {}).get("description") or ""
    manufacturer = (supply or {}).get("manufacturer") or ""
    supplier    = (contact or {}).get("supplier_name") or (supply or {}).get("supplier") or ""
    customer_number = (contact or {}).get("customer_number") or ""
    contact_person  = (contact or {}).get("contact_person") or ""
    quantity    = order.get("quantity") or 1
    printer     = order.get("printer_name") or order.get("printer_id") or ""
    color_word  = {"K": "black", "C": "cyan", "M": "magenta",
                   "Y": "yellow"}.get((order.get("color") or "").upper(), "")

    lang_name = {"de": "German", "en": "English", "fr": "French",
                 "it": "Italian", "es": "Spanish"}.get(lang, "English")
    system = (
        "You draft short, professional B2B purchase-order emails to a "
        "print-supply distributor, sent by an MSP on behalf of one of "
        f"its customers. Write in {lang_name}. Use ONLY the order "
        "details given below — never invent a SKU, quantity, price, or "
        "delivery date. If a field is missing (e.g. no SKU on file), "
        "ask the supplier to confirm the correct part for the stated "
        "printer instead of guessing one. If a customer account number "
        "is given, mention it so the supplier can bill/ship to the "
        "right account. If a named contact person is given, address the "
        "greeting to them by name instead of a generic salutation. Keep "
        "it short: a subject line and a brief "
        "body — greeting, the order itself, a polite close. No "
        "markdown, no placeholders like [Your Name]. Reply with ONE "
        "JSON object only, no prose, no code fences: "
        "{\"subject\": \"…\", \"body\": \"…\"}"
    )
    user = (
        f"Customer reference: {customer_name}\n"
        f"Our account number with this supplier: {customer_number or 'n/a'}\n"
        f"Contact person at the supplier: {contact_person or 'n/a (use a generic greeting)'}\n"
        f"Printer: {printer}\n"
        f"Toner colour: {color_word or 'n/a'}\n"
        f"SKU: {sku or '(not on file — ask supplier to confirm)'}\n"
        f"Description: {description or 'n/a'}\n"
        f"Manufacturer: {manufacturer or 'n/a'}\n"
        f"Supplier: {supplier or 'n/a'}\n"
        f"Quantity: {quantity}\n"
    )
    try:
        resp = llm_client.chat(system, user)
    except llm_client.LLMError as e:
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "[ai_suggest_order_mail] LLM error for order: %s", e)
        return None

    import json as _json
    raw = (resp.text or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:].lstrip("\n")
    try:
        data = _json.loads(raw)
    except _json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    subject = (data.get("subject") or "").strip()
    body = (data.get("body") or "").strip()
    if not subject or not body:
        return None
    return {"subject": subject, "body": body}


def _normalize_color(c: str) -> str:
    c = (c or "").strip()
    return c if c in _ALLOWED_COLORS else ""


def _clean_str(v: Any) -> str:
    return (str(v) if v is not None else "").strip()


def _clean_int_or_none(v: Any) -> int | None:
    s = _clean_str(v)
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None


def _sanitise_template_fields(f: dict[str, Any]) -> dict[str, Any]:
    color = _normalize_color(f.get("color", ""))
    if not color:
        raise ValueError("color must be one of K/C/M/Y/other")
    model = _clean_str(f.get("printer_model"))
    if not model:
        raise ValueError("printer_model is required")
    return {
        "printer_model":    model,
        "color":            color,
        "sku":              _clean_str(f.get("sku")),
        "description":      _clean_str(f.get("description")),
        "manufacturer":     _clean_str(f.get("manufacturer")),
        "supplier":         _clean_str(f.get("supplier")),
        "supplier_id":      _clean_int_or_none(f.get("supplier_id")),
        "supplier_url":     _clean_str(f.get("supplier_url")),
        "default_quantity": max(1, _clean_int_or_none(f.get("default_quantity")) or 1),
        "unit_price_cents": _clean_int_or_none(f.get("unit_price_cents")),
        "yield_pages":      _clean_int_or_none(f.get("yield_pages")),
        "notes":            _clean_str(f.get("notes")),
        "is_shared":        1,
    }


def _sanitise_override_fields(f: dict[str, Any]) -> dict[str, Any]:
    return {
        "sku":              _clean_str(f.get("sku")),
        "description":      _clean_str(f.get("description")),
        "manufacturer":     _clean_str(f.get("manufacturer")),
        "supplier":         _clean_str(f.get("supplier")),
        "supplier_id":      _clean_int_or_none(f.get("supplier_id")),
        "supplier_url":     _clean_str(f.get("supplier_url")),
        "default_quantity": max(1, _clean_int_or_none(f.get("default_quantity")) or 1),
        "unit_price_cents": _clean_int_or_none(f.get("unit_price_cents")),
        "notes":            _clean_str(f.get("notes")),
    }


# ---------------------------------------------------------------------------
# Seed data — a handful of common cartridges so the library isn't empty
# ---------------------------------------------------------------------------

# Real-world starter set. Prices are placeholders (EUR cents excl. VAT)
# just so the "estimated cost" column has something to render on the
# demo dashboard; every entry is meant to be edited/replaced by the
# operator before the first real order goes out.
SEED_TEMPLATES: list[dict[str, Any]] = [
    # HP LaserJet Pro 400 M401 family (26A / 26X)
    dict(printer_model="HP LaserJet Pro M401", color="K", sku="CF226A",
         description="HP 26A Original LaserJet Toner",
         manufacturer="HP", supplier="",
         supplier_url="https://www.google.com/search?q=HP+CF226A",
         yield_pages=3100, unit_price_cents=8500, default_quantity=1),

    # HP Color LaserJet Pro M479 (415A K/C/M/Y)
    dict(printer_model="HP Color LaserJet Pro M479", color="K", sku="W2030A",
         description="HP 415A Black", manufacturer="HP",
         supplier_url="https://www.google.com/search?q=HP+W2030A",
         yield_pages=2400, unit_price_cents=11500),
    dict(printer_model="HP Color LaserJet Pro M479", color="C", sku="W2031A",
         description="HP 415A Cyan", manufacturer="HP",
         supplier_url="https://www.google.com/search?q=HP+W2031A",
         yield_pages=2100, unit_price_cents=14500),
    dict(printer_model="HP Color LaserJet Pro M479", color="M", sku="W2033A",
         description="HP 415A Magenta", manufacturer="HP",
         supplier_url="https://www.google.com/search?q=HP+W2033A",
         yield_pages=2100, unit_price_cents=14500),
    dict(printer_model="HP Color LaserJet Pro M479", color="Y", sku="W2032A",
         description="HP 415A Yellow", manufacturer="HP",
         supplier_url="https://www.google.com/search?q=HP+W2032A",
         yield_pages=2100, unit_price_cents=14500),

    # Brother HL-L8360CDW (TN-421 K/C/M/Y)
    dict(printer_model="Brother HL-L8360CDW", color="K", sku="TN-421BK",
         description="Brother TN-421 Black", manufacturer="Brother",
         supplier_url="https://www.google.com/search?q=Brother+TN-421BK",
         yield_pages=3000, unit_price_cents=7900),
    dict(printer_model="Brother HL-L8360CDW", color="C", sku="TN-421C",
         description="Brother TN-421 Cyan", manufacturer="Brother",
         supplier_url="https://www.google.com/search?q=Brother+TN-421C",
         yield_pages=1800, unit_price_cents=8900),
    dict(printer_model="Brother HL-L8360CDW", color="M", sku="TN-421M",
         description="Brother TN-421 Magenta", manufacturer="Brother",
         supplier_url="https://www.google.com/search?q=Brother+TN-421M",
         yield_pages=1800, unit_price_cents=8900),
    dict(printer_model="Brother HL-L8360CDW", color="Y", sku="TN-421Y",
         description="Brother TN-421 Yellow", manufacturer="Brother",
         supplier_url="https://www.google.com/search?q=Brother+TN-421Y",
         yield_pages=1800, unit_price_cents=8900),

    # Kyocera ECOSYS P5026cdw (TK-5220 K/C/M/Y)
    dict(printer_model="Kyocera ECOSYS P5026cdw", color="K", sku="TK-5220K",
         description="Kyocera TK-5220 Black", manufacturer="Kyocera",
         supplier_url="https://www.google.com/search?q=Kyocera+TK-5220K",
         yield_pages=1200, unit_price_cents=5900),
    dict(printer_model="Kyocera ECOSYS P5026cdw", color="C", sku="TK-5220C",
         description="Kyocera TK-5220 Cyan", manufacturer="Kyocera",
         supplier_url="https://www.google.com/search?q=Kyocera+TK-5220C",
         yield_pages=1200, unit_price_cents=6900),
    dict(printer_model="Kyocera ECOSYS P5026cdw", color="M", sku="TK-5220M",
         description="Kyocera TK-5220 Magenta", manufacturer="Kyocera",
         supplier_url="https://www.google.com/search?q=Kyocera+TK-5220M",
         yield_pages=1200, unit_price_cents=6900),
    dict(printer_model="Kyocera ECOSYS P5026cdw", color="Y", sku="TK-5220Y",
         description="Kyocera TK-5220 Yellow", manufacturer="Kyocera",
         supplier_url="https://www.google.com/search?q=Kyocera+TK-5220Y",
         yield_pages=1200, unit_price_cents=6900),
]


def seed_templates_if_empty(admin_user_id: int) -> int:
    """One-shot seed: only runs when the table is empty. Returns the
    number of rows inserted. Called from the settings page via a
    dedicated 'seed sample data' button — never runs automatically,
    because we want the operator to see and consent to it."""
    with db.get_conn() as conn:
        existing = conn.execute(
            select(db.supply_templates.c.id).limit(1)
        ).first()
    if existing is not None:
        return 0

    # v0.14.3: catch per-row IntegrityError (FK on updated_by_user_id
    # when the DB has been cleaned up under us, duplicate on
    # model+color if half the seed already ran and got interrupted)
    # so a single bad row doesn't take down the whole batch. Also
    # fall back to updated_by_user_id=None if the admin_user_id
    # doesn't exist — happens when the DB got restored from a backup
    # into a fresh instance and the calling user was recreated with
    # a new PK.
    from sqlalchemy.exc import IntegrityError as _IE
    from sqlalchemy import select as _sel
    # Sanity-check: does that user actually exist? If not, don't
    # FK-fail every insert — fall back to NULL owner (column is
    # ondelete=SET NULL, so NULL is a legal value).
    with db.get_conn() as conn:
        exists = conn.execute(
            _sel(db.users.c.id).where(db.users.c.id == admin_user_id).limit(1)
        ).first()
    effective_owner = admin_user_id if exists else None

    n = 0
    for row in SEED_TEMPLATES:
        try:
            upsert_template(None, row, updated_by_user_id=effective_owner)
            n += 1
        except (_IE, Exception) as e:  # noqa: BLE001 — seed must survive one bad row
            import logging
            logging.getLogger(__name__).warning(
                "seed_templates_if_empty: skipping %s / %s: %s",
                row.get("printer_model"), row.get("color"), str(e)[:200])
    return n
