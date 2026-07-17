"""Order kanban + magic-link handlers.

Two audiences:

* Logged-in operator on ``/orders`` — full kanban board, one column
  per non-terminal status. Buttons on every card move it one step
  through the state machine.

* An email recipient who clicked a magic link ``/orders/action/{token}``.
  No auth required — the token itself is the credential. Landing page
  shows what would happen and asks for a confirm click to prevent
  drive-by state changes when a link is prefetched.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .. import auth, db, orders, supply_library
from ..db import customers as customers_tbl


router = APIRouter()


# ---------------------------------------------------------------------------
# Kanban board — /orders
# ---------------------------------------------------------------------------

@router.get("/orders", response_class=HTMLResponse, include_in_schema=False)
async def orders_board(request: Request):
    user = auth.require_user(request)
    visible = auth.visible_customer_ids(user)

    # v0.18.2: single-customer filter, sticky in session so a reload keeps it.
    q = request.query_params
    raw = q.get("customer", "").strip()
    if raw == "all":
        filter_customer = ""
        try:
            request.session.pop("orders_customer", None)
        except AssertionError:
            pass
    elif raw.isdigit() and int(raw) in visible:
        filter_customer = raw
        try:
            request.session["orders_customer"] = raw
        except AssertionError:
            pass
    else:
        try:
            sess = request.session.get("orders_customer", "")
        except AssertionError:
            sess = ""
        filter_customer = sess if (sess.isdigit() and int(sess) in visible) else ""

    scope = [int(filter_customer)] if filter_customer else visible
    active_orders = orders.list_orders(
        scope, statuses=("draft", "ordered", "delivered"))
    recent_closed = orders.list_orders(
        scope, statuses=("installed", "cancelled"), limit=30)

    # Fill each card with the customer display name and the resolved
    # supply record so the template can render a "reorder" link even
    # for closed orders.
    customer_names = _customer_names(visible)
    for lst in (active_orders, recent_closed):
        for o in lst:
            o["customer_name"] = customer_names.get(o["customer_id"], "")
            o["supply"] = supply_library.resolve_supply(
                o["customer_id"], o["printer_id"],
                None,  # model unknown at kanban time; override or template by-id only
                o["color"])

    grouped = orders.group_by_status(active_orders)

    # Dropdown source: every customer the operator can see (not just
    # ones with active orders) so the filter stays discoverable when
    # a tenant is quiet.
    customer_choices = sorted(customer_names.items(), key=lambda kv: kv[1].lower())

    return request.app.state.templates.TemplateResponse(
        "orders/board.html",
        {
            "request": request,
            "lang": request.state.lang,
            "user": user,
            "grouped": grouped,
            "recent_closed": recent_closed,
            "filter_customer":   filter_customer,
            "customer_choices":  customer_choices,
            "info":  request.query_params.get("info", ""),
            "error": request.query_params.get("error", ""),
            # v0.24.1: the "🛒 Bestellen" button on /toner redirects here
            # with the order id it just created (or reused) so the
            # kanban can highlight + scroll to it — the operator lands
            # directly on the card they need to review, instead of
            # hunting for it in three columns.
            "highlight_id":  q.get("highlight", ""),
            "highlight_new": q.get("highlight_new", "") == "1",
        },
    )


def _customer_names(ids: list[int]) -> dict[int, str]:
    if not ids:
        return {}
    from sqlalchemy import select
    with db.get_conn() as conn:
        rows = conn.execute(
            select(customers_tbl.c.id, customers_tbl.c.name)
            .where(customers_tbl.c.id.in_(ids))
        ).all()
    return {r.id: r.name for r in rows}


@router.post("/orders/{order_id}/status", include_in_schema=False)
async def orders_transition(order_id: int, request: Request):
    user = auth.require_user(request)
    o = orders.get_order(order_id)
    if o is None:
        return RedirectResponse("/orders?error=order_not_found", status_code=303)
    # Tenant fence — an operator can only touch orders for a customer
    # they have access to.
    if not auth.user_can_see_customer(user, o["customer_id"]):
        return RedirectResponse("/orders?error=forbidden", status_code=303)

    form = await request.form()
    new_status = (form.get("status") or "").strip().lower()
    reason = (form.get("reason") or "").strip()

    try:
        orders.transition(order_id, new_status, user_id=user["id"],
                          reason=reason)
    except orders.OrderError as e:
        return RedirectResponse(
            f"/orders?error={str(e)[:120].replace('&','')}", status_code=303)
    return RedirectResponse(f"/orders?info=order_{new_status}",
                            status_code=303)


@router.get("/orders/{order_id}/mail_suggestion", include_in_schema=False)
async def orders_mail_suggestion(order_id: int, request: Request):
    """v0.24.6 — draft a ready-to-copy supplier order email for one
    order. Never sends anything; the operator copies the text into
    their own mail client. Same tenant fence as every other order
    action."""
    user = auth.require_user(request)
    o = orders.get_order(order_id)
    if o is None:
        return JSONResponse({"ok": False, "error": "order_not_found"}, status_code=404)
    if not auth.user_can_see_customer(user, o["customer_id"]):
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)

    customer_names = _customer_names([o["customer_id"]])
    supply = supply_library.resolve_supply(
        o["customer_id"], o["printer_id"], None, o["color"])
    mail = supply_library.ai_suggest_order_mail(
        o, supply, customer_names.get(o["customer_id"], ""),
        lang=request.state.lang)
    if mail is None:
        return JSONResponse({"ok": False, "error": "llm_unavailable"}, status_code=400)
    return JSONResponse({"ok": True, **mail})


@router.post("/orders/new", include_in_schema=False)
async def orders_new(request: Request):
    """Manual draft-create — from the kanban 'add' button when the
    operator noticed a printer needs toner before the runner did."""
    user = auth.require_user(request)
    form = await request.form()
    try:
        customer_id = int(form.get("customer_id") or 0)
    except ValueError:
        customer_id = 0
    if not auth.user_can_see_customer(user, customer_id):
        return RedirectResponse("/orders?error=forbidden", status_code=303)

    printer_id = (form.get("printer_id") or "").strip()
    printer_name = (form.get("printer_name") or "").strip()
    color = (form.get("color") or "K").strip()
    sku = (form.get("sku") or "").strip()
    try:
        qty = int(form.get("quantity") or 1)
    except ValueError:
        qty = 1
    notes = (form.get("notes") or "").strip()

    if not printer_id:
        return RedirectResponse("/orders?error=printer_id_missing",
                                status_code=303)
    try:
        orders.create_draft(customer_id, printer_id, printer_name, color,
                            sku=sku, quantity=qty, notes=notes,
                            ordered_by_user_id=user["id"])
    except orders.OrderError as e:
        return RedirectResponse(
            f"/orders?error={str(e)[:120].replace('&','')}", status_code=303)
    return RedirectResponse("/orders?info=order_created", status_code=303)


@router.post("/toner/{customer_id}/{printer_id}/order", include_in_schema=False)
async def toner_order_now(customer_id: int, printer_id: str, request: Request):
    """v0.24.1 — the "🛒 Bestellen" button on the toner grid/list.
    This is the missing bridge Marcus asked about: seeing a
    critical/warn toner color on /toner and having ONE persistent
    button that actually starts an order, instead of only reacting
    to the alert e-mail whenever the runner happens to fire.

    One click either creates a fresh draft — pre-filled with the
    resolved supply template's SKU + default quantity, if a
    template exists for this (model, color) — or reuses the
    existing active order for this exact (customer, printer, color)
    slot. Same idempotent create_draft_if_none() path the alert
    runner already uses, so clicking twice (or the runner firing a
    moment later) can never spawn a duplicate active order.

    Redirects straight into the kanban with that order highlighted
    + customer filter cleared, so it's always visible: the operator
    lands exactly where they can check the SKU, send it (mark
    ordered), edit quantity via the printer's supply override, or
    delete the draft if it was a mistake."""
    user = auth.require_user(request)
    if not auth.user_can_see_customer(user, customer_id):
        return RedirectResponse("/toner?error=forbidden", status_code=303)

    form = await request.form()
    color = (form.get("color") or "K").strip().upper()
    printer_name = (form.get("printer_name") or "").strip()
    model = (form.get("model") or "").strip()

    supply = supply_library.resolve_supply(customer_id, printer_id, model, color)
    sku = (supply or {}).get("sku") or ""
    try:
        qty = int((supply or {}).get("default_quantity") or 1)
    except (TypeError, ValueError):
        qty = 1

    order_id, created = orders.create_draft_if_none(
        customer_id, printer_id, printer_name, color, sku=sku, quantity=qty)

    if created:
        db.audit(user["id"], "order.created",
                 target_type="order", target_id=str(order_id),
                 meta_json=json.dumps({"printer_id": printer_id, "color": color,
                                        "sku": sku, "via": "toner_grid"}))

    return RedirectResponse(
        f"/orders?customer=all&highlight={order_id}"
        f"&highlight_new={'1' if created else '0'}",
        status_code=303)


@router.post("/orders/{order_id}/delete", include_in_schema=False)
async def orders_delete_draft(order_id: int, request: Request):
    user = auth.require_user(request)
    o = orders.get_order(order_id)
    if o is None:
        return RedirectResponse("/orders", status_code=303)
    if not auth.user_can_see_customer(user, o["customer_id"]):
        return RedirectResponse("/orders?error=forbidden", status_code=303)
    try:
        orders.delete_order(order_id)
    except orders.OrderError as e:
        return RedirectResponse(f"/orders?error={str(e)[:120].replace('&','')}",
                                status_code=303)
    return RedirectResponse("/orders?info=draft_deleted", status_code=303)


# ---------------------------------------------------------------------------
# Magic-link handlers — no auth required, token IS the credential
# ---------------------------------------------------------------------------

@router.get("/orders/action/{token}", response_class=HTMLResponse,
            include_in_schema=False)
async def orders_action_land(token: str, request: Request):
    """Landing page — shows what would happen, asks for a confirm
    click so a mail-client link-preview / prefetch can't accidentally
    change state."""
    result = orders.verify_action_token(token)
    if result is None:
        return request.app.state.templates.TemplateResponse(
            "orders/action.html",
            {"request": request, "lang": request.state.lang,
             "user": None, "state": "invalid_token"},
            status_code=400,
        )
    order_id, action = result
    o = orders.get_order(order_id)
    if o is None:
        return request.app.state.templates.TemplateResponse(
            "orders/action.html",
            {"request": request, "lang": request.state.lang,
             "user": None, "state": "not_found"},
            status_code=404,
        )

    # If already in the target state (or a later terminal state), tell
    # the recipient it's done — no confirm button.
    if o["status"] == action:
        state = "already_done"
    elif o["status"] in ("installed", "cancelled"):
        state = "closed"
    elif action not in _allowed_next(o["status"]):
        state = "not_allowed"
    else:
        state = "confirm"

    return request.app.state.templates.TemplateResponse(
        "orders/action.html",
        {"request": request, "lang": request.state.lang,
         "user": None, "state": state, "order": o,
         "action": action, "token": token},
    )


@router.post("/orders/action/{token}/confirm", response_class=HTMLResponse,
             include_in_schema=False)
async def orders_action_confirm(token: str, request: Request):
    result = orders.verify_action_token(token)
    if result is None:
        return RedirectResponse("/orders/action/" + token, status_code=303)
    order_id, action = result
    try:
        orders.transition(order_id, action, user_id=None,
                          reason="magic-link-confirm")
    except orders.OrderError:
        return RedirectResponse(f"/orders/action/{token}", status_code=303)
    o = orders.get_order(order_id)
    return request.app.state.templates.TemplateResponse(
        "orders/action.html",
        {"request": request, "lang": request.state.lang,
         "user": None, "state": "done", "order": o, "action": action,
         "token": token},
    )


def _allowed_next(current: str) -> tuple[str, ...]:
    return orders._ALLOWED_TRANSITIONS.get(current, ())  # noqa: SLF001
