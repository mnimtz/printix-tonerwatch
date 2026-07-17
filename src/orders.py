"""Order flow — draft → ordered → delivered → installed (or cancelled).

The state machine is deliberately small: draft/ordered/delivered/installed
form the happy path, cancelled is a terminal side-branch. Every
transition writes a `toner_events` row so the dashboard timeline picks
it up automatically.

Draft orders can be created:
* automatically, by the alert runner when a critical/warn threshold
  crosses AND no active order for that (customer, printer, color) tuple
  exists yet — this is the "auto-draft" mode that prevents duplicate
  orders while still letting the operator confirm before spending money;
* manually, from the kanban board (`/orders`).

Magic-link tokens signed with :func:`auth.sign_magic_token` let an
email recipient jump straight to a single-action confirm page without
having to log in. Tokens are scoped to (order_id, action) and expire
after 14 days — long enough for a weekend + a vacation, short enough
that a leaked mail from months ago can't reopen a closed order.
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Sequence

from sqlalchemy import and_, desc, func, insert, or_, select, update

from . import auth, db


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

STATUSES = ("draft", "ordered", "delivered", "installed", "cancelled")

# What can move to what. Once installed, an order is done — no further
# transitions. Cancelled is terminal too.
_ALLOWED_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "draft":     ("ordered", "cancelled"),
    "ordered":   ("delivered", "cancelled"),
    "delivered": ("installed", "cancelled"),
    "installed": (),
    "cancelled": (),
}

# Statuses considered "active" — an active order blocks auto-drafting
# a duplicate for the same slot.
ACTIVE_STATUSES = ("draft", "ordered", "delivered")

# Magic-link expiry: two weeks. Long enough for a weekend + a short
# vacation, short enough that a leaked mail from months ago can't
# resurrect a closed order.
MAGIC_LINK_TTL_SECONDS = 14 * 24 * 3600

# Salt used for the itsdangerous serializer — anything unique to this
# feature keeps signed tokens from being cross-used with the auth
# module's password-reset tokens.
_MAGIC_SALT = "order-action-v1"


class OrderError(Exception):
    """Raised for invalid transitions or unknown orders."""


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

def _row_to_order(row: Any) -> dict[str, Any]:
    """Same shape as db._row_to_dict, kept as a helper in case we later
    want to enrich orders with joined columns (customer name, resolved
    supply). Right now it's a thin wrapper."""
    return db._row_to_dict(row)  # type: ignore[return-value]


def get_order(order_id: int) -> dict[str, Any] | None:
    with db.get_conn() as conn:
        row = conn.execute(
            select(db.toner_orders).where(db.toner_orders.c.id == order_id)
        ).first()
    return _row_to_order(row) if row else None


def list_orders(customer_ids: Sequence[int],
                statuses: Sequence[str] | None = None,
                limit: int | None = None) -> list[dict[str, Any]]:
    """Every order for the given customers, newest first. Empty
    customer_ids returns an empty list (never spills across tenants)."""
    if not customer_ids:
        return []
    with db.get_conn() as conn:
        q = select(db.toner_orders).where(
            db.toner_orders.c.customer_id.in_(list(customer_ids))
        )
        if statuses:
            q = q.where(db.toner_orders.c.status.in_(list(statuses)))
        q = q.order_by(desc(db.toner_orders.c.ordered_at))
        if limit:
            q = q.limit(limit)
        rows = conn.execute(q).all()
    return [_row_to_order(r) for r in rows]


def group_by_status(orders: list[dict]) -> dict[str, list[dict]]:
    """Bucket a list of orders into a status→orders dict, with every
    known status pre-populated (empty lists for missing buckets so the
    kanban template can iterate STATUSES without KeyError)."""
    out: dict[str, list[dict]] = {s: [] for s in STATUSES}
    for o in orders:
        out.setdefault(o["status"], []).append(o)
    return out


def active_order_for(customer_id: int, printer_id: str, color: str) -> dict | None:
    """Newest active order for one (customer, printer, color) slot,
    or None. Used by the alert runner to skip a printer whose toner
    is already 'ordered but not yet delivered'."""
    with db.get_conn() as conn:
        row = conn.execute(
            select(db.toner_orders)
            .where(and_(
                db.toner_orders.c.customer_id == customer_id,
                db.toner_orders.c.printer_id == printer_id,
                db.toner_orders.c.color == color,
                db.toner_orders.c.status.in_(list(ACTIVE_STATUSES)),
            ))
            .order_by(desc(db.toner_orders.c.ordered_at))
            .limit(1)
        ).first()
    return _row_to_order(row) if row else None


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------

def create_draft(
    customer_id: int,
    printer_id: str,
    printer_name: str,
    color: str,
    sku: str = "",
    quantity: int = 1,
    notes: str = "",
    ordered_by_user_id: int | None = None,
) -> int:
    """Insert a draft order and return its id. Does NOT check for an
    existing active order — that's the caller's job (see
    :func:`create_draft_if_none`).
    """
    color = _validate_color(color)
    quantity = max(1, int(quantity or 1))
    with db.get_conn() as conn:
        result = conn.execute(insert(db.toner_orders).values(
            customer_id=customer_id,
            printer_id=printer_id,
            printer_name=printer_name or "",
            color=color,
            sku=sku or "",
            quantity=quantity,
            status="draft",
            ordered_by_user_id=ordered_by_user_id,
            notes=notes or "",
        ))
        order_id = int(result.inserted_primary_key[0])
        conn.execute(insert(db.toner_events).values(
            customer_id=customer_id, kind="order.created",
            printer_id=printer_id, color=color,
            meta_json=json.dumps({"order_id": order_id, "sku": sku,
                                  "auto": ordered_by_user_id is None}),
        ))
    return order_id


def update_draft_sku(order_id: int, sku: str,
                      notes_append: str = "") -> None:
    """v0.20.0 — after the runner enriches a draft with an AI-suggested
    SKU, persist it. Only touches DRAFT orders so an admin who already
    saw + edited the draft doesn't get it silently rewritten."""
    with db.get_conn() as conn:
        conn.execute(update(db.toner_orders)
                     .where(db.toner_orders.c.id == order_id)
                     .where(db.toner_orders.c.status == "draft")
                     .values(sku=(sku or "").strip(),
                             notes=(func.coalesce(db.toner_orders.c.notes, "")
                                     + ("\n" + notes_append
                                        if notes_append else ""))))


def count_today(customer_id: int) -> int:
    """v0.20.0 — how many orders were opened today for this customer?
    Used by the autonomous-order path to enforce the daily cap.
    Counts every non-cancelled row created since midnight UTC."""
    with db.get_conn() as conn:
        row = conn.execute(select(func.count(db.toner_orders.c.id))
                            .where(db.toner_orders.c.customer_id == customer_id)
                            .where(db.toner_orders.c.status != "cancelled")
                            .where(db.toner_orders.c.created_at
                                    >= func.date("now"))
                            ).first()
    return int(row[0] or 0) if row else 0


def create_draft_if_none(
    customer_id: int, printer_id: str, printer_name: str, color: str,
    sku: str, quantity: int = 1,
) -> tuple[int, bool]:
    """Idempotent draft creation: (order_id, created). If an active
    order already exists for the slot, returns its id + False. Used by
    the alert runner so a stuck printer doesn't spawn one draft per
    poll tick.

    v0.17.2: check-then-insert isn't atomic — if a manual
    /customers/{id}/alerts/run collides with the scheduler tick,
    both see no active order, both call create_draft, second insert
    violates the partial unique index. Catch that and re-query
    instead of surfacing a 500.
    """
    from sqlalchemy.exc import IntegrityError as _IE
    existing = active_order_for(customer_id, printer_id, color)
    if existing is not None:
        return existing["id"], False
    try:
        oid = create_draft(customer_id, printer_id, printer_name, color,
                           sku=sku, quantity=quantity)
    except _IE:
        # Race: the other caller committed first. Re-query.
        existing = active_order_for(customer_id, printer_id, color)
        if existing is not None:
            return existing["id"], False
        raise
    return oid, True


def transition(
    order_id: int,
    new_status: str,
    user_id: int | None,
    reason: str = "",
) -> dict[str, Any]:
    """Move an order to a new status. Raises :class:`OrderError` on
    unknown order or disallowed transition. Returns the fresh row."""
    if new_status not in STATUSES:
        raise OrderError(f"unknown status: {new_status!r}")

    with db.get_conn() as conn:
        row = conn.execute(
            select(db.toner_orders).where(db.toner_orders.c.id == order_id)
        ).first()
        if row is None:
            raise OrderError(f"order {order_id} not found")
        current = row.status
        if new_status == current:
            return _row_to_order(row)  # no-op, don't audit
        allowed = _ALLOWED_TRANSITIONS.get(current, ())
        if new_status not in allowed:
            raise OrderError(
                f"cannot move order from {current!r} to {new_status!r}")

        values: dict[str, Any] = {"status": new_status}
        if new_status in ("installed", "cancelled"):
            values["closed_at"] = func.current_timestamp()
            values["closed_reason"] = reason or ""
        # v0.24.31: used to overwrite ordered_by_user_id here on every
        # transition, losing who actually created the draft the moment
        # anyone else moved it along. ordered_by_user_id is set once,
        # at creation (create_draft); this is who touched it last.
        if user_id is not None:
            values["updated_by_user_id"] = user_id
            values["updated_at"] = func.current_timestamp()

        conn.execute(update(db.toner_orders)
                     .where(db.toner_orders.c.id == order_id)
                     .values(**values))
        conn.execute(insert(db.toner_events).values(
            customer_id=row.customer_id, kind=f"order.{new_status}",
            printer_id=row.printer_id, color=row.color,
            meta_json=json.dumps({"order_id": order_id,
                                  "from": current, "to": new_status,
                                  "user_id": user_id,
                                  "reason": reason or ""}),
        ))
        fresh = conn.execute(
            select(db.toner_orders).where(db.toner_orders.c.id == order_id)
        ).first()
    return _row_to_order(fresh)


def delete_order(order_id: int) -> None:
    """Hard-delete an order (draft only). Anything past draft should be
    cancelled via :func:`transition` so the audit trail is preserved."""
    with db.get_conn() as conn:
        row = conn.execute(
            select(db.toner_orders).where(db.toner_orders.c.id == order_id)
        ).first()
        if row is None:
            return
        if row.status != "draft":
            raise OrderError("only draft orders can be hard-deleted")
        conn.execute(db.toner_orders.delete().where(
            db.toner_orders.c.id == order_id))


# ---------------------------------------------------------------------------
# Magic-link tokens for one-click transitions from alert mails
# ---------------------------------------------------------------------------

_MAGIC_ACTIONS = {"ordered", "cancelled", "delivered", "installed"}


def sign_action_token(order_id: int, action: str) -> str:
    if action not in _MAGIC_ACTIONS:
        raise OrderError(f"cannot sign token for action {action!r}")
    return auth.sign_magic_token(
        {"o": order_id, "a": action}, salt=_MAGIC_SALT)


def verify_action_token(token: str) -> tuple[int, str] | None:
    payload = auth.verify_magic_token(
        token, max_age=MAGIC_LINK_TTL_SECONDS, salt=_MAGIC_SALT)
    if not payload:
        return None
    order_id = payload.get("o")
    action = payload.get("a")
    if not isinstance(order_id, int) or action not in _MAGIC_ACTIONS:
        return None
    return order_id, action


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------

_ALLOWED_COLORS = ("K", "C", "M", "Y", "other")


def _validate_color(c: str) -> str:
    c = (c or "").strip()
    if c not in _ALLOWED_COLORS:
        raise OrderError(f"unknown color: {c!r}")
    return c
