"""Suppliers — v0.24.14.

A global vendor list (shared across customers, like the supply
library) plus the per-customer relationship details: account/customer
number, and an order-mailbox override for the rare case a customer
orders through a different address than the supplier's own default.

supply_templates / printer_supplies carry a supplier_id so the order
flow can resolve "who do we email, and what's the customer's account
number" automatically instead of the operator typing it in every time.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import and_, delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError

from . import db


# ---------------------------------------------------------------------------
# Suppliers (global)
# ---------------------------------------------------------------------------

def list_suppliers(include_inactive: bool = False) -> list[dict[str, Any]]:
    with db.get_conn() as conn:
        q = select(db.suppliers)
        if not include_inactive:
            q = q.where(db.suppliers.c.active == 1)
        rows = conn.execute(q.order_by(db.suppliers.c.name)).all()
    return [db._row_to_dict(r) for r in rows]


def get_supplier(supplier_id: int) -> dict[str, Any] | None:
    with db.get_conn() as conn:
        row = conn.execute(
            select(db.suppliers).where(db.suppliers.c.id == supplier_id)
        ).first()
    return db._row_to_dict(row)


class SupplierError(Exception):
    """Raised on a name collision — suppliers.name is unique."""


def upsert_supplier(supplier_id: int | None, fields: dict[str, Any],
                    updated_by_user_id: int | None) -> int:
    payload = {
        "name":        (fields.get("name") or "").strip(),
        "order_email": (fields.get("order_email") or "").strip(),
        "website_url": (fields.get("website_url") or "").strip(),
        "notes":       (fields.get("notes") or "").strip(),
        "active":      1 if fields.get("active", True) else 0,
        "updated_by_user_id": updated_by_user_id,
    }
    if not payload["name"]:
        raise SupplierError("name is required")
    with db.get_conn() as conn:
        try:
            if supplier_id is None:
                result = conn.execute(insert(db.suppliers).values(**payload))
                return result.inserted_primary_key[0]
            conn.execute(update(db.suppliers)
                         .where(db.suppliers.c.id == supplier_id)
                         .values(**payload))
            return supplier_id
        except IntegrityError as e:
            raise SupplierError(
                f"A supplier named '{payload['name']}' already exists.") from e


def delete_supplier(supplier_id: int) -> None:
    """Soft-delete — keeps existing supply_templates/printer_supplies
    references intact (their supplier_id FK is ON DELETE SET NULL if
    ever hard-deleted, but deactivating is the normal path so past
    orders still show which supplier they went to)."""
    with db.get_conn() as conn:
        conn.execute(update(db.suppliers)
                     .where(db.suppliers.c.id == supplier_id)
                     .values(active=0))


# ---------------------------------------------------------------------------
# Per-customer relationship
# ---------------------------------------------------------------------------

def list_customer_suppliers(customer_id: int) -> list[dict[str, Any]]:
    """All suppliers assigned to this customer, with the relationship
    fields (customer_number, email override) joined in. Backs the
    customer's supplier-assignment page."""
    with db.get_conn() as conn:
        rows = conn.execute(
            select(db.suppliers, db.customer_suppliers.c.customer_number,
                   db.customer_suppliers.c.order_email_override,
                   db.customer_suppliers.c.notes.label("relationship_notes"))
            .select_from(db.suppliers.join(
                db.customer_suppliers,
                and_(db.customer_suppliers.c.supplier_id == db.suppliers.c.id,
                     db.customer_suppliers.c.customer_id == customer_id)))
            .where(db.suppliers.c.active == 1)
            .order_by(db.suppliers.c.name)
        ).all()
    return [db._row_to_dict(r) for r in rows]


def get_customer_supplier(customer_id: int, supplier_id: int) -> dict[str, Any] | None:
    with db.get_conn() as conn:
        row = conn.execute(
            select(db.customer_suppliers).where(
                and_(db.customer_suppliers.c.customer_id == customer_id,
                     db.customer_suppliers.c.supplier_id == supplier_id))
        ).first()
    return db._row_to_dict(row)


def upsert_customer_supplier(customer_id: int, supplier_id: int,
                             fields: dict[str, Any]) -> None:
    payload = {
        "customer_number": (fields.get("customer_number") or "").strip(),
        "order_email_override": (fields.get("order_email_override") or "").strip(),
        "notes": (fields.get("notes") or "").strip(),
    }
    with db.get_conn() as conn:
        exists = conn.execute(
            select(db.customer_suppliers.c.customer_id).where(
                and_(db.customer_suppliers.c.customer_id == customer_id,
                     db.customer_suppliers.c.supplier_id == supplier_id))
        ).first()
        if exists is None:
            conn.execute(insert(db.customer_suppliers).values(
                customer_id=customer_id, supplier_id=supplier_id, **payload))
        else:
            conn.execute(update(db.customer_suppliers).where(
                and_(db.customer_suppliers.c.customer_id == customer_id,
                     db.customer_suppliers.c.supplier_id == supplier_id)
            ).values(**payload, updated_at=func.current_timestamp()))


def remove_customer_supplier(customer_id: int, supplier_id: int) -> None:
    with db.get_conn() as conn:
        conn.execute(delete(db.customer_suppliers).where(
            and_(db.customer_suppliers.c.customer_id == customer_id,
                 db.customer_suppliers.c.supplier_id == supplier_id)))


# ---------------------------------------------------------------------------
# Order-flow resolution
# ---------------------------------------------------------------------------

def resolve_supplier_contact(customer_id: int, supplier_id: int | None
                             ) -> dict[str, Any] | None:
    """The order-mail feature's entry point: given the supplier a SKU
    is linked to, resolve the actual send-to address + this customer's
    account number with them. Prefers the per-customer email override
    over the supplier's own default; returns ``None`` if there's no
    supplier_id at all (SKU not yet linked to a formal supplier
    record) so callers can fall back to their existing behaviour."""
    if not supplier_id:
        return None
    supplier = get_supplier(supplier_id)
    if not supplier or not supplier.get("active"):
        return None
    link = get_customer_supplier(customer_id, supplier_id)
    email = ((link or {}).get("order_email_override")
             or supplier.get("order_email") or "")
    return {
        "supplier_id": supplier_id,
        "supplier_name": supplier["name"],
        "order_email": email,
        "customer_number": (link or {}).get("customer_number", ""),
    }
