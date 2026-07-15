"""Per-printer metadata edit routes — Standort, Serial, Gruppe, Asset-Tag.

Owns two URLs (both scoped to an operator with access to the customer):

* ``GET  /toner/{customer_id}/{printer_id}/info`` — form pre-filled
  with any existing overrides, with placeholders showing the BI
  values so the user sees what "empty" would fall back to.
* ``POST /toner/{customer_id}/{printer_id}/info`` — persist.
* ``POST /toner/{customer_id}/{printer_id}/info/delete`` — wipe.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from .. import auth, bi_client, db, printer_info
from ..db import customers as customers_tbl


router = APIRouter()


def _customer_or_none(customer_id: int) -> dict | None:
    with db.get_conn() as conn:
        row = conn.execute(
            select(customers_tbl).where(customers_tbl.c.id == customer_id)
        ).first()
    return db._row_to_dict(row) if row else None


def _printer_or_none(customer: dict, printer_id: str) -> dict | None:
    """Look up one printer via BI. Returns the raw BI row (no merge)
    so the form can show BI values as placeholders."""
    try:
        bi = bi_client.customer_for_bi(customer)
    except Exception:
        return None
    try:
        rows = bi_client.fetch_all_printer_supplies(bi)
    except Exception:
        return None
    for r in rows or ():
        if r.get("id") == printer_id or r.get("printer_id") == printer_id:
            return r
    return None


@router.get("/toner/{customer_id}/{printer_id}/info",
            response_class=HTMLResponse, include_in_schema=False)
async def printer_info_form(customer_id: int, printer_id: str, request: Request):
    user = auth.require_customer_access(request, customer_id)
    customer = _customer_or_none(customer_id)
    if customer is None:
        return RedirectResponse("/toner?error=customer_not_found",
                                status_code=303)

    printer = _printer_or_none(customer, printer_id)
    info = printer_info.get_info(customer_id, printer_id)
    known_groups = printer_info.list_groups_for_customer(customer_id)

    return request.app.state.templates.TemplateResponse(
        "printer_info/edit.html",
        {
            "request": request,
            "lang": request.state.lang,
            "user": user,
            "customer": customer,
            "printer": printer or {"id": printer_id, "printer_name": printer_id,
                                    "model": "", "location": "",
                                    "serial_number": "", "vendor": ""},
            "info": info or {},
            "known_groups": known_groups,
            "info": info or {},
            "info_present": info is not None,
            "known_groups": known_groups,
            "known_groups_present": bool(known_groups),
            "form_error": request.query_params.get("error", ""),
            "form_info":  request.query_params.get("info", ""),
        },
    )


@router.post("/toner/{customer_id}/{printer_id}/info",
             include_in_schema=False)
async def printer_info_save(customer_id: int, printer_id: str, request: Request):
    user = auth.require_customer_access(request, customer_id)
    form = await request.form()
    fields = {
        "location_override": form.get("location_override") or "",
        "serial_override":   form.get("serial_override") or "",
        "asset_tag":         form.get("asset_tag") or "",
        "group_name":        form.get("group_name") or "",
        "contact_email":     form.get("contact_email") or "",
        "purchased_at":      form.get("purchased_at") or "",
        "warranty_until":    form.get("warranty_until") or "",
        "notes":             form.get("notes") or "",
    }
    printer_info.upsert_info(customer_id, printer_id, fields,
                             updated_by_user_id=user["id"])
    db.audit(user["id"], "printer_info.saved",
             target_type="printer",
             target_id=f"{customer_id}:{printer_id}",
             meta_json=json.dumps({k: v for k, v in fields.items() if v}))
    return RedirectResponse(
        f"/toner/{customer_id}/{printer_id}/info?info=saved",
        status_code=303)


@router.post("/toner/{customer_id}/{printer_id}/info/delete",
             include_in_schema=False)
async def printer_info_delete(customer_id: int, printer_id: str, request: Request):
    user = auth.require_customer_access(request, customer_id)
    printer_info.delete_info(customer_id, printer_id)
    db.audit(user["id"], "printer_info.deleted",
             target_type="printer",
             target_id=f"{customer_id}:{printer_id}")
    return RedirectResponse(
        f"/toner/{customer_id}/{printer_id}/info?info=deleted",
        status_code=303)
