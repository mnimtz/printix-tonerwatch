"""Suppliers — global vendor list (admin CRUD) + per-customer account
details (any user with access to that customer, same permission model
as the per-printer supply override editor).
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import auth, db, suppliers
from ..db import customers as customers_tbl
from sqlalchemy import select


router = APIRouter()


# ---------------------------------------------------------------------------
# Global vendor list — admin CRUD
# ---------------------------------------------------------------------------

@router.get("/suppliers", response_class=HTMLResponse, include_in_schema=False)
async def suppliers_list(request: Request):
    user = auth.require_admin(request)
    rows = suppliers.list_suppliers(include_inactive=True)
    return request.app.state.templates.TemplateResponse(
        "suppliers/list.html",
        {
            "request": request,
            "lang": request.state.lang,
            "user": user,
            "suppliers": rows,
            "info":  request.query_params.get("info", ""),
            "error": request.query_params.get("error", ""),
        },
    )


@router.get("/suppliers/new", response_class=HTMLResponse, include_in_schema=False)
async def suppliers_new_form(request: Request):
    user = auth.require_admin(request)
    return request.app.state.templates.TemplateResponse(
        "suppliers/edit.html",
        {
            "request": request,
            "lang": request.state.lang,
            "user": user,
            "supplier": None,
            "form_action": "/suppliers/new",
            "error": request.query_params.get("error", ""),
        },
    )


@router.post("/suppliers/new", include_in_schema=False)
async def suppliers_new_save(request: Request):
    admin = auth.require_admin(request)
    form = await request.form()
    fields = dict(form)
    fields["active"] = form.get("active") == "on"
    try:
        new_id = suppliers.upsert_supplier(None, fields, updated_by_user_id=admin["id"])
    except suppliers.SupplierError as e:
        return RedirectResponse(f"/suppliers/new?error={str(e)[:160]}",
                                status_code=303)
    db.audit(admin["id"], "supplier.created",
             target_type="supplier", target_id=str(new_id))
    return RedirectResponse("/suppliers?info=supplier_saved", status_code=303)


@router.get("/suppliers/{supplier_id}/edit", response_class=HTMLResponse,
            include_in_schema=False)
async def suppliers_edit_form(supplier_id: int, request: Request):
    user = auth.require_admin(request)
    s = suppliers.get_supplier(supplier_id)
    if s is None:
        return RedirectResponse("/suppliers?error=supplier_not_found",
                                status_code=303)
    return request.app.state.templates.TemplateResponse(
        "suppliers/edit.html",
        {
            "request": request,
            "lang": request.state.lang,
            "user": user,
            "supplier": s,
            "form_action": f"/suppliers/{supplier_id}/edit",
            "error": request.query_params.get("error", ""),
        },
    )


@router.post("/suppliers/{supplier_id}/edit", include_in_schema=False)
async def suppliers_edit_save(supplier_id: int, request: Request):
    admin = auth.require_admin(request)
    form = await request.form()
    fields = dict(form)
    fields["active"] = form.get("active") == "on"
    try:
        suppliers.upsert_supplier(supplier_id, fields, updated_by_user_id=admin["id"])
    except suppliers.SupplierError as e:
        return RedirectResponse(f"/suppliers/{supplier_id}/edit?error={str(e)[:160]}",
                                status_code=303)
    db.audit(admin["id"], "supplier.updated",
             target_type="supplier", target_id=str(supplier_id))
    return RedirectResponse("/suppliers?info=supplier_saved", status_code=303)


@router.post("/suppliers/{supplier_id}/delete", include_in_schema=False)
async def suppliers_delete(supplier_id: int, request: Request):
    admin = auth.require_admin(request)
    suppliers.delete_supplier(supplier_id)
    db.audit(admin["id"], "supplier.deactivated",
             target_type="supplier", target_id=str(supplier_id))
    return RedirectResponse("/suppliers?info=supplier_deleted", status_code=303)


# ---------------------------------------------------------------------------
# Per-customer relationship — any user with access to the customer
# ---------------------------------------------------------------------------

def _customer_or_none(customer_id: int) -> dict | None:
    with db.get_conn() as conn:
        row = conn.execute(
            select(customers_tbl).where(customers_tbl.c.id == customer_id)
        ).first()
    return db._row_to_dict(row) if row else None


@router.get("/customers/{customer_id}/suppliers", response_class=HTMLResponse,
            include_in_schema=False)
async def customer_suppliers_form(customer_id: int, request: Request):
    user = auth.require_customer_access(request, customer_id)
    customer = _customer_or_none(customer_id)
    if customer is None:
        return RedirectResponse("/customers?error=customer_not_found",
                                status_code=303)

    all_suppliers = suppliers.list_suppliers()
    # list_customer_suppliers() selects db.suppliers joined to the
    # relationship row, so its `id` IS the supplier id (not a
    # separate `supplier_id` column — that only exists on the raw
    # customer_suppliers table).
    links = {l["id"]: l for l in suppliers.list_customer_suppliers(customer_id)}
    rows = [{"supplier": s, "link": links.get(s["id"])} for s in all_suppliers]

    return request.app.state.templates.TemplateResponse(
        "customers/suppliers.html",
        {
            "request": request,
            "lang": request.state.lang,
            "user": user,
            "customer": customer,
            "rows": rows,
            "info":  request.query_params.get("info", ""),
            "error": request.query_params.get("error", ""),
        },
    )


@router.post("/customers/{customer_id}/suppliers", include_in_schema=False)
async def customer_suppliers_save(customer_id: int, request: Request):
    user = auth.require_customer_access(request, customer_id)
    customer = _customer_or_none(customer_id)
    if customer is None:
        return RedirectResponse("/customers?error=customer_not_found",
                                status_code=303)
    form = await request.form()

    saved = 0
    cleared = 0
    for s in suppliers.list_suppliers():
        sid = s["id"]
        keys = (f"customer_number_{sid}", f"order_email_override_{sid}",
                f"contact_person_override_{sid}", f"phone_override_{sid}",
                f"notes_{sid}")
        if not any(k in form for k in keys):
            continue
        fields = {
            "customer_number":          form.get(f"customer_number_{sid}") or "",
            "order_email_override":     form.get(f"order_email_override_{sid}") or "",
            "contact_person_override":  form.get(f"contact_person_override_{sid}") or "",
            "phone_override":           form.get(f"phone_override_{sid}") or "",
            "notes":                    form.get(f"notes_{sid}") or "",
        }
        had_link = suppliers.get_customer_supplier(customer_id, sid) is not None
        if any(fields.values()):
            suppliers.upsert_customer_supplier(customer_id, sid, fields)
            saved += 1
        elif had_link:
            suppliers.remove_customer_supplier(customer_id, sid)
            cleared += 1

    db.audit(user["id"], "supplier.customer_link_saved",
             target_type="customer", target_id=str(customer_id))
    return RedirectResponse(
        f"/customers/{customer_id}/suppliers?info=suppliers_saved",
        status_code=303)
