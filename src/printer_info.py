"""Per-printer metadata overrides — resolver + CRUD.

Printix BI already returns `location`, `serial_number`, `model`,
`vendor` for every printer, but the operator often has better info:
a proper room number instead of "Etage 2", the asset-tag from the
in-house tracker, a purchase date, a per-device contact email for
notification escalation, or a group name for filter/list-grouping.

This module owns the merge — call :func:`enrich` on every BI row
before rendering, and every downstream template can just look at
``row["location"]`` / ``row["serial_number"]`` without caring where
the value came from.

Rows can be sparse. An empty override string means "use BI value".
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from sqlalchemy import and_, delete, distinct, func, insert, select, update

from . import db


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

def get_info(customer_id: int, printer_id: str) -> dict[str, Any] | None:
    with db.get_conn() as conn:
        row = conn.execute(
            select(db.printer_info).where(and_(
                db.printer_info.c.customer_id == customer_id,
                db.printer_info.c.printer_id == printer_id,
            ))
        ).first()
    return db._row_to_dict(row) if row else None


def list_info_for_customer(customer_id: int) -> dict[str, dict]:
    """Return ``{printer_id: info_row}`` — cheap enough to bulk-load
    on every /toner render because customers have at most a few
    hundred printers and the row is small."""
    with db.get_conn() as conn:
        rows = conn.execute(
            select(db.printer_info)
            .where(db.printer_info.c.customer_id == customer_id)
        ).all()
    return {r.printer_id: db._row_to_dict(r) for r in rows}


def list_groups_for_customer(customer_id: int) -> list[str]:
    """Distinct non-empty group_name values for one customer, sorted."""
    with db.get_conn() as conn:
        rows = conn.execute(
            select(distinct(db.printer_info.c.group_name))
            .where(and_(
                db.printer_info.c.customer_id == customer_id,
                db.printer_info.c.group_name != "",
            ))
        ).all()
    return sorted((r[0] for r in rows), key=str.lower)


def list_all_groups(customer_ids: Sequence[int]) -> list[str]:
    """Distinct group names across a set of customers — used by the
    toner grid group filter when the operator sees more than one
    customer at once."""
    if not customer_ids:
        return []
    with db.get_conn() as conn:
        rows = conn.execute(
            select(distinct(db.printer_info.c.group_name))
            .where(and_(
                db.printer_info.c.customer_id.in_(list(customer_ids)),
                db.printer_info.c.group_name != "",
            ))
        ).all()
    return sorted((r[0] for r in rows), key=str.lower)


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

def upsert_info(
    customer_id: int, printer_id: str,
    fields: dict[str, Any], updated_by_user_id: int,
) -> None:
    """Insert or replace one printer_info row. If every override
    would be empty, delete the row so we don't accumulate stubs."""
    clean = _sanitise(fields)

    is_empty = not any(clean.values())
    if is_empty:
        delete_info(customer_id, printer_id)
        return

    clean["updated_by_user_id"] = updated_by_user_id
    clean["updated_at"] = func.current_timestamp()
    with db.get_conn() as conn:
        # Portable upsert: delete-then-insert on the composite PK.
        conn.execute(
            delete(db.printer_info).where(and_(
                db.printer_info.c.customer_id == customer_id,
                db.printer_info.c.printer_id == printer_id,
            ))
        )
        conn.execute(insert(db.printer_info).values(
            customer_id=customer_id, printer_id=printer_id, **clean,
        ))


def delete_info(customer_id: int, printer_id: str) -> None:
    with db.get_conn() as conn:
        conn.execute(
            delete(db.printer_info).where(and_(
                db.printer_info.c.customer_id == customer_id,
                db.printer_info.c.printer_id == printer_id,
            ))
        )


# ---------------------------------------------------------------------------
# Merge (BI row + override)
# ---------------------------------------------------------------------------

_MERGED_FIELDS = {
    # bi_key         override_key
    "location":      "location_override",
    "serial_number": "serial_override",
}

_INFO_PASSTHROUGH = ("asset_tag", "group_name", "contact_email",
                     "purchased_at", "warranty_until", "notes")


def enrich(bi_row: dict[str, Any], info: dict[str, Any] | None) -> dict[str, Any]:
    """Return a new dict merging BI values with per-device overrides.

    Called from the toner grid + dashboard render path. Every value
    from `bi_row` stays; overrides win where present; a `has_info`
    flag lets the template badge overridden devices.
    """
    out = dict(bi_row)
    if not info:
        # Even without info, expose the passthrough keys as empty so
        # templates can .get() them without a None check.
        for k in _INFO_PASSTHROUGH:
            out.setdefault(k, "")
        out["has_info"] = False
        return out

    for bi_key, ov_key in _MERGED_FIELDS.items():
        override = (info.get(ov_key) or "").strip()
        if override:
            out[bi_key] = override
    for k in _INFO_PASSTHROUGH:
        out[k] = (info.get(k) or "")
    out["has_info"] = True
    return out


# ---------------------------------------------------------------------------
# Field sanitisation
# ---------------------------------------------------------------------------

_FIELDS = (
    "location_override", "serial_override", "asset_tag", "group_name",
    "contact_email", "purchased_at", "warranty_until", "notes",
)


def _sanitise(f: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in _FIELDS:
        v = f.get(k)
        out[k] = ("" if v is None else str(v)).strip()
    return out
