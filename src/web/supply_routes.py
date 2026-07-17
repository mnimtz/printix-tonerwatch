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

from fastapi.responses import JSONResponse

from .. import auth, bi_client, db, llm_client, suppliers, supply_library
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
    all_suppliers = suppliers.list_suppliers()
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
            "all_suppliers": all_suppliers,
            # v0.24.16: exactly one supplier on file — no point making
            # the operator pick it every time a new template is added.
            "default_supplier": all_suppliers[0] if len(all_suppliers) == 1 else None,
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


_SET_COLORS = ("K", "C", "M", "Y")


@router.get("/supplies/new-set", response_class=HTMLResponse,
            include_in_schema=False)
async def supplies_new_set_form(request: Request):
    """v0.24.0 — create every toner-color template for one printer
    model in a single form instead of repeating '+ New template'
    once per color. Defaults to all four (CMYK) checked; the AI
    button can auto-toggle C/M/Y off for a mono device."""
    user = auth.require_admin(request)
    all_suppliers = suppliers.list_suppliers()
    return request.app.state.templates.TemplateResponse(
        "supplies/new_set.html",
        {
            "request": request,
            "lang": request.state.lang,
            "user": user,
            "error": request.query_params.get("error", ""),
            "prefill_model": request.query_params.get("model", ""),
            "all_suppliers": all_suppliers,
            "default_supplier": all_suppliers[0] if len(all_suppliers) == 1 else None,
        },
    )


@router.post("/supplies/ai/suggest-set", include_in_schema=False)
async def supplies_ai_suggest_set(request: Request):
    """v0.24.0 — one LLM round-trip that both classifies the printer
    (mono vs color) and fills SKU/description/manufacturer/yield for
    every slot it has. Powers the "🤖 AI-Vorschlag" button on the
    Toner-Set form."""
    auth.require_admin(request)
    if not llm_client.is_configured():
        return JSONResponse(
            {"ok": False, "error": "llm_not_configured"}, status_code=400)

    form = await request.form()
    model = (form.get("printer_model") or "").strip()
    if not model:
        return JSONResponse(
            {"ok": False, "error": "printer_model_required"}, status_code=400)

    result = supply_library.ai_suggest_supply_set(model)
    if result is None:
        return JSONResponse(
            {"ok": False, "error": "llm_response_not_json_or_unavailable"},
            status_code=500)

    return JSONResponse({
        "ok": True,
        "provider": result["provider"],
        "model": result["model"],
        "is_color": result["is_color"],
        "slots": result["slots"],
    })


@router.post("/supplies/new-set", include_in_schema=False)
async def supplies_new_set_save(request: Request):
    """v0.24.0 — batch-create up to 4 (model, color) templates from
    one submit. Every checked color is its own upsert_template() call
    so a duplicate on one color (e.g. K already exists) doesn't block
    the others — the redirect summarises created vs skipped."""
    admin = auth.require_admin(request)
    form = await request.form()
    model = (form.get("printer_model") or "").strip()
    if not model:
        return RedirectResponse(
            "/supplies/new-set?error=printer_model_required",
            status_code=303)

    shared = {
        "printer_model":     model,
        "supplier":          form.get("supplier") or "",
        "supplier_id":       form.get("supplier_id") or "",
        "supplier_url":      form.get("supplier_url") or "",
        "default_quantity":  form.get("default_quantity") or "1",
    }

    created: list[str] = []
    skipped_duplicate: list[str] = []
    skipped_unchecked = 0
    for color in _SET_COLORS:
        if not form.get(f"enable_{color}"):
            skipped_unchecked += 1
            continue
        fields = {
            **shared,
            "color":            color,
            "sku":              form.get(f"sku_{color}") or "",
            "description":      form.get(f"description_{color}") or "",
            "manufacturer":     form.get(f"manufacturer_{color}") or "",
            "yield_pages":      form.get(f"yield_{color}") or "",
            "unit_price_cents": form.get(f"price_{color}") or "",
        }
        try:
            _, err = supply_library.upsert_template(
                None, fields, updated_by_user_id=admin["id"])
        except ValueError as e:
            return RedirectResponse(
                f"/supplies/new-set?error={str(e)[:120]}&model={model}",
                status_code=303)
        if err == "duplicate_model_color":
            skipped_duplicate.append(color)
        else:
            created.append(color)
            db.audit(admin["id"], "supply.template_created",
                     target_type="supply_template",
                     target_id=f"{model}:{color}",
                     meta_json=json.dumps({"via": "set"}))

    if not created and not skipped_duplicate:
        return RedirectResponse(
            "/supplies/new-set?error=select_at_least_one_color",
            status_code=303)

    info = f"set_saved_{len(created)}"
    if skipped_duplicate:
        info += f"_dup_{','.join(skipped_duplicate)}"
    return RedirectResponse(f"/supplies?info={info}", status_code=303)


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
            "all_suppliers": suppliers.list_suppliers(),
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


@router.get("/supplies/models", include_in_schema=False)
async def supplies_known_models(request: Request):
    """Return a JSON list of distinct printer_model strings observed
    across every active customer's BI feed the user can see. Used by
    the supply-template edit form to power the model-autocomplete
    datalist so the operator picks a real BI-reported model instead
    of typing a slightly-wrong name that no printer matches."""
    user = auth.require_admin(request)
    from sqlalchemy import select as _sel
    from .. import bi_client as _bi
    with db.get_conn() as conn:
        customers = [db._row_to_dict(r) for r in conn.execute(
            _sel(customers_tbl).where(customers_tbl.c.active == 1)
        ).all()]
    models: set[str] = set()
    for c in customers:
        # Only customers that actually have BI creds
        if not (c.get("sql_server") and c.get("sql_database")
                and c.get("sql_username")):
            continue
        try:
            bi_c = _bi.customer_for_bi(c)
            # Read-only from cache when possible; fall back to fresh
            # fetch when the cache is cold. We use the same call the
            # /toner grid uses so we hit the same cache entry.
            rows = _bi.fetch_all_printer_supplies_cached_only(bi_c)
            if rows is None:
                rows = _bi.fetch_all_printer_supplies(bi_c) or []
        except Exception:
            continue
        for r in rows or ():
            m = (r.get("model") or "").strip()
            if m:
                models.add(m)
    # Also merge in every model that already has a template — the
    # operator might have entered one for a printer that isn't
    # in BI yet.
    for t in supply_library.list_templates():
        if t.get("printer_model"):
            models.add(t["printer_model"])
    return JSONResponse({"models": sorted(models, key=str.lower)})


@router.post("/supplies/ai/suggest", include_in_schema=False)
async def supplies_ai_suggest(request: Request):
    """Ask the configured LLM for a SKU + description + yield given
    a printer model + colour. Returns a small JSON object the edit
    form JS pre-fills the empty fields with.

    Never overwrites values the operator has already typed — that's
    a client-side decision (JS only fills empty inputs).
    """
    auth.require_admin(request)
    if not llm_client.is_configured():
        return JSONResponse(
            {"ok": False, "error": "llm_not_configured"}, status_code=400)

    form = await request.form()
    model = (form.get("printer_model") or "").strip()
    color = (form.get("color") or "K").strip().upper()
    if not model:
        return JSONResponse(
            {"ok": False, "error": "printer_model_required"}, status_code=400)
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
    user = (f"Printer model: {model}\n"
            f"Toner slot colour: {color_word}\n"
            "Return the OEM cartridge (not a compatible / generic).")

    try:
        resp = llm_client.chat(system, user)
    except llm_client.LLMError as e:
        return JSONResponse(
            {"ok": False, "error": str(e)[:200]}, status_code=500)

    # Best-effort parse. Some models still wrap JSON in markdown even
    # when told not to — strip code fences before json.loads().
    raw = resp.text.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:].lstrip("\n")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Return the raw text so the operator can see what the LLM said
        return JSONResponse(
            {"ok": False, "error": "llm_response_not_json", "raw": raw[:400]},
            status_code=500)

    return JSONResponse({
        "ok": True,
        "provider": resp.provider,
        "model": resp.model,
        "sku":          _clean(data.get("sku")),
        "description":  _clean(data.get("description")),
        "manufacturer": _clean(data.get("manufacturer")),
        "yield_pages":  _as_int(data.get("yield_pages")),
    })


def _clean(v):
    if v is None:
        return ""
    return str(v).strip()


def _as_int(v):
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


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
    # v0.17.1: fetch_all_printer_supplies returns None on BI-DB errors
    # (asleep Azure SQL, wrong creds). Guard against iterating None.
    for r in rows or ():
        if r.get("id") == printer_id or r.get("printer_id") == printer_id:
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
            "all_suppliers": suppliers.list_suppliers(),
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
            "supplier_id":      form.get(f"supplier_id_{color}") or "",
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
