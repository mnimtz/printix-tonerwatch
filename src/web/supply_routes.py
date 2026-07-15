"""Supply library — admin routes for model templates + per-printer overrides.

Two entry points into the same data:

* ``/supplies`` — model template library. Admin-only. One row per
  (printer_model, color). Feeds every printer that matches the model.

* ``/toner/{customer_id}/{printer_id}/supply`` — per-printer override
  editor. Any user with access to the customer can edit. Empty
  fields clear the override (falls back to the template).

The resolver is not exposed via HTTP — it's a Python call the toner
grid and the alert mailer use directly (see supply_library.py).
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from .. import auth, bi_client, db, supply_library
from ..db import customers as customers_tbl


router = APIRouter()


# ---------------------------------------------------------------------------
# Model templates — admin CRUD
# ---------------------------------------------------------------------------

@router.get("/supplies", response_class=HTMLResponse, include_in_schema=False)
async def supplies_list(request: Request):
    user = auth.require_admin(request)
    templates = supply_library.list_templates()
    # Group by printer_model so the list reads like a catalog, not a
    # flat table — each model gets its own section with K/C/M/Y rows.
    grouped: dict[str, list[dict]] = {}
    for t in templates:
        grouped.setdefault(t["printer_model"], []).append(t)
    return request.app.state.templates.TemplateResponse(
        "supplies/list.html",
        {
            "request": request,
            "lang": request.state.lang,
            "user": user,
            "grouped": grouped,
            "total": len(templates),
            "info":  request.query_params.get("info", ""),
            "error": request.query_params.get("error", ""),
        },
    )


@router.get("/supplies/new", response_class=HTMLResponse, include_in_schema=False)
async def supplies_new_form(request: Request):
    user = auth.require_admin(request)
    return request.app.state.templates.TemplateResponse(
        "supplies/edit.html",
        {
            "request": request,
            "lang": request.state.lang,
            "user": user,
            "template": None,
            "form_action": "/supplies/new",
            "error": request.query_params.get("error", ""),
            "prefill_model": request.query_params.get("model", ""),
        },
    )


@router.post("/supplies/new", include_in_schema=False)
async def supplies_new_save(request: Request):
    admin = auth.require_admin(request)
    form = await request.form()
    try:
        _, err = supply_library.upsert_template(
            None, dict(form), updated_by_user_id=admin["id"])
    except ValueError as e:
        return RedirectResponse(f"/supplies/new?error={str(e)[:120]}",
                                status_code=303)
    if err == "duplicate_model_color":
        return RedirectResponse(
            "/supplies/new?error=A+template+for+this+model+%2B+color+already+exists",
            status_code=303)
    db.audit(admin["id"], "supply.template_created",
             target_type="supply_template",
             target_id=f"{form.get('printer_model','')}:{form.get('color','')}")
    return RedirectResponse("/supplies?info=template_saved", status_code=303)


@router.get("/supplies/{tpl_id}/edit", response_class=HTMLResponse,
            include_in_schema=False)
async def supplies_edit_form(tpl_id: int, request: Request):
    user = auth.require_admin(request)
    tpl = supply_library.get_template(tpl_id)
    if tpl is None:
        return RedirectResponse("/supplies?error=template_not_found",
                                status_code=303)
    return request.app.state.templates.TemplateResponse(
        "supplies/edit.html",
        {
            "request": request,
            "lang": request.state.lang,
            "user": user,
            "template": tpl,
            "form_action": f"/supplies/{tpl_id}/edit",
            "error": request.query_params.get("error", ""),
        },
    )


@router.post("/supplies/{tpl_id}/edit", include_in_schema=False)
async def supplies_edit_save(tpl_id: int, request: Request):
    admin = auth.require_admin(request)
    form = await request.form()
    try:
        _, err = supply_library.upsert_template(
            tpl_id, dict(form), updated_by_user_id=admin["id"])
    except ValueError as e:
        return RedirectResponse(f"/supplies/{tpl_id}/edit?error={str(e)[:120]}",
                                status_code=303)
    if err == "duplicate_model_color":
        return RedirectResponse(
            f"/supplies/{tpl_id}/edit?error=Another+template+already+covers+this+model+%2B+color",
            status_code=303)
    db.audit(admin["id"], "supply.template_updated",
             target_type="supply_template", target_id=str(tpl_id))
    return RedirectResponse("/supplies?info=template_saved", status_code=303)


@router.post("/supplies/{tpl_id}/delete", include_in_schema=False)
async def supplies_delete(tpl_id: int, request: Request):
    admin = auth.require_admin(request)
    supply_library.delete_template(tpl_id)
    db.audit(admin["id"], "supply.template_deleted",
             target_type="supply_template", target_id=str(tpl_id))
    return RedirectResponse("/supplies?info=template_deleted", status_code=303)


@router.post("/supplies/seed", include_in_schema=False)
async def supplies_seed(request: Request):
    """One-shot: inserts ~15 common cartridge templates so a fresh
    install has something to work with. No-op if the table isn't
    empty."""
    admin = auth.require_admin(request)
    n = supply_library.seed_templates_if_empty(admin["id"])
    if n == 0:
        return RedirectResponse("/supplies?error=library_not_empty",
                                status_code=303)
    db.audit(admin["id"], "supply.seed",
             target_type="supply_template",
             meta_json=json.dumps({"inserted": n}))
    return RedirectResponse(f"/supplies?info=seeded_{n}", status_code=303)


# ---------------------------------------------------------------------------
# Per-printer overrides — any user with access to the customer
# ---------------------------------------------------------------------------

def _customer_or_none(customer_id: int) -> dict | None:
    with db.get_conn() as conn:
        row = conn.execute(
            select(customers_tbl).where(customers_tbl.c.id == customer_id)
        ).first()
    return db._row_to_dict(row) if row else None


def _printer_or_none(customer: dict, printer_id: str) -> dict | None:
    """Fetch one printer from the BI DB. Returns the same shape as
    fetch_all_printer_supplies() rows: dict with `id`, `name`,
    `model`, `location`, `supplies`, `error_states`, `reported_state`."""
    try:
        bi_c = bi_client.customer_for_bi(customer)
    except Exception:
        return None
    try:
        rows = bi_client.fetch_all_printer_supplies(bi_c)
    except Exception:
        return None
    for r in rows:
        if r.get("id") == printer_id:
            return r
    return None


@router.get("/toner/{customer_id}/{printer_id}/supply",
            response_class=HTMLResponse, include_in_schema=False)
async def printer_supply_form(customer_id: int, printer_id: str, request: Request):
    user = auth.require_customer_access(request, customer_id)
    customer = _customer_or_none(customer_id)
    if customer is None:
        return RedirectResponse("/toner?error=customer_not_found",
                                status_code=303)

    printer = _printer_or_none(customer, printer_id)
    overrides = supply_library.get_overrides_for_printer(customer_id, printer_id)

    # Which colors does the printer expose? Fall back to K if we have
    # no reading (still lets the operator pre-fill an override).
    colors = ["K"]
    if printer and printer.get("supplies"):
        colors = [s["color"] for s in printer["supplies"]]

    # Per color: (override, template_fallback) so the form can show
    # inherited values as placeholder text.
    rows = []
    for c in colors:
        rows.append({
            "color":    c,
            "override": overrides.get(c),
            "template": supply_library.resolve_supply(
                customer_id, printer_id,
                (printer or {}).get("model", ""), c) if not overrides.get(c) else None,
        })

    return request.app.state.templates.TemplateResponse(
        "supplies/printer_override.html",
        {
            "request": request,
            "lang": request.state.lang,
            "user": user,
            "customer": customer,
            "printer": printer or {"id": printer_id, "name": printer_id,
                                    "model": "", "location": ""},
            "rows": rows,
            "info":  request.query_params.get("info", ""),
            "error": request.query_params.get("error", ""),
        },
    )


@router.post("/toner/{customer_id}/{printer_id}/supply",
             include_in_schema=False)
async def printer_supply_save(customer_id: int, printer_id: str, request: Request):
    user = auth.require_customer_access(request, customer_id)
    form = await request.form()

    # The form posts multiple color rows in parallel: sku_K, url_K,
    # sku_C, url_C, … — one upsert per color.
    saved = 0
    cleared = 0
    for color in ("K", "C", "M", "Y", "other"):
        # skip a color that was not on the form at all
        keys = (f"sku_{color}", f"description_{color}", f"supplier_url_{color}")
        if not any(k in form for k in keys):
            continue
        fields = {
            "sku":              form.get(f"sku_{color}") or "",
            "description":      form.get(f"description_{color}") or "",
            "manufacturer":     form.get(f"manufacturer_{color}") or "",
            "supplier":         form.get(f"supplier_{color}") or "",
            "supplier_url":     form.get(f"supplier_url_{color}") or "",
            "default_quantity": form.get(f"default_quantity_{color}") or "1",
            "unit_price_cents": form.get(f"unit_price_cents_{color}") or "",
            "notes":            form.get(f"notes_{color}") or "",
        }
        before = supply_library.get_overrides_for_printer(customer_id, printer_id).get(color)
        supply_library.upsert_override(
            customer_id, printer_id, color, fields,
            updated_by_user_id=user["id"])
        after = supply_library.get_overrides_for_printer(customer_id, printer_id).get(color)
        if after and not before:
            saved += 1
        elif before and not after:
            cleared += 1
        elif after and before:
            saved += 1

    db.audit(user["id"], "supply.override_saved",
             target_type="printer",
             target_id=f"{customer_id}:{printer_id}",
             meta_json=json.dumps({"saved": saved, "cleared": cleared}))
    return RedirectResponse(
        f"/toner/{customer_id}/{printer_id}/supply?info=override_saved",
        status_code=303)


@router.post("/toner/{customer_id}/{printer_id}/supply/clear",
             include_in_schema=False)
async def printer_supply_clear(customer_id: int, printer_id: str, request: Request):
    user = auth.require_customer_access(request, customer_id)
    supply_library.delete_override(customer_id, printer_id)
    db.audit(user["id"], "supply.override_cleared",
             target_type="printer",
             target_id=f"{customer_id}:{printer_id}")
    return RedirectResponse(
        f"/toner/{customer_id}/{printer_id}/supply?info=override_cleared",
        status_code=303)
