"""Customer management — list, create, edit, delete, test-connection."""

from __future__ import annotations

import json

from fastapi import APIRouter, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import delete, func, insert, select, update

from .. import auth, bi_client, crypto, db, savings_report, toner_alerts
from ..db import audit_log, customer_access, customers, users
from . import i18n


router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_int(val: str | None, *, default: int, lo: int, hi: int) -> int:
    try:
        n = int(val or "")
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _bool_form(val: str | None) -> int:
    return 1 if (val or "").lower() in ("1", "on", "true", "yes") else 0


def _customer_row(customer_id: int) -> dict | None:
    with db.get_conn() as conn:
        row = conn.execute(
            select(customers).where(customers.c.id == customer_id)
        ).first()
    return db._row_to_dict(row)


def _customer_form_from_row(row: dict | None) -> dict:
    """Return a dict the edit template can render — masked password."""
    if row is None:
        return {
            "id": None, "name": "", "tenant_url": "",
            "customer_number": "", "address": "", "notes": "",
            "sql_server": "", "sql_database": "", "sql_port": 1433,
            "sql_username": "",
            "sql_password_present": False,
            "alert_recipients_csv": "", "alert_min_level": "WARN",
            "order_recipients_csv": "",
            "warn_pct": 20, "critical_pct": 5,
            "timezone": "Europe/Berlin",
            "quiet_hours_start": "", "quiet_hours_end": "",
            "digest_mode": 0, "auto_order_mode": "off",
            "auto_order_daily_cap": 10,
            "active": 1,
        }
    return {
        **row,
        "sql_password_present": bool(row["sql_password_enc"]),
    }


def _visible_customers_for(user: dict) -> list[dict]:
    with db.get_conn() as conn:
        if user["role"] == "admin":
            rows = conn.execute(
                select(customers).order_by(customers.c.name)
            ).all()
        else:
            rows = conn.execute(
                select(customers)
                .select_from(
                    customers.join(
                        customer_access,
                        customers.c.id == customer_access.c.customer_id,
                    )
                )
                .where(customer_access.c.user_id == user["id"])
                .order_by(customers.c.name)
            ).all()
    return [db._row_to_dict(r) for r in rows]


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------

@router.get("/customers", response_class=HTMLResponse, include_in_schema=False)
async def customers_list(request: Request):
    user = auth.require_user(request)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        "customers/list.html",
        {
            "request": request,
            "lang": request.state.lang,
            "user": user,
            "customers": _visible_customers_for(user),
        },
    )


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------

@router.get("/customers/new", response_class=HTMLResponse, include_in_schema=False)
async def customer_new_form(request: Request):
    user = auth.require_admin(request)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        "customers/edit.html",
        {
            "request": request,
            "lang": request.state.lang,
            "user": user,
            "form": _customer_form_from_row(None),
            "is_new": True,
            "error": None,
        },
    )


@router.post("/customers/new", include_in_schema=False)
async def customer_new_submit(request: Request):
    user = auth.require_admin(request)
    form = await request.form()

    name = (form.get("name") or "").strip()
    if not name:
        templates = request.app.state.templates
        return templates.TemplateResponse(
            "customers/edit.html",
            {
                "request": request,
                "lang": request.state.lang,
                "user": user,
                "form": {**_customer_form_from_row(None), **dict(form)},
                "is_new": True,
                "error": i18n.t("customer.error.name_required", request.state.lang),
            },
            status_code=400,
        )

    values = _values_from_form(form, is_new=True)
    values["name"] = name
    values["created_by_user_id"] = user["id"]

    with db.get_conn() as conn:
        result = conn.execute(insert(customers).values(**values))
        new_id = result.inserted_primary_key[0]
    db.audit(user["id"], "customer.created",
             target_type="customer", target_id=str(new_id),
             meta_json=json.dumps({"name": name}))

    return RedirectResponse(f"/customers/{new_id}", status_code=303)


# ---------------------------------------------------------------------------
# Edit
# ---------------------------------------------------------------------------

@router.get("/customers/{customer_id}/edit",
            response_class=HTMLResponse, include_in_schema=False)
async def customer_edit_form(customer_id: int, request: Request):
    user = auth.require_admin(request)
    row = _customer_row(customer_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        "customers/edit.html",
        {
            "request": request,
            "lang": request.state.lang,
            "user": user,
            "form": _customer_form_from_row(row),
            "is_new": False,
            "error": None,
        },
    )


@router.post("/customers/{customer_id}/edit", include_in_schema=False)
async def customer_edit_submit(customer_id: int, request: Request):
    user = auth.require_admin(request)
    row = _customer_row(customer_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    form = await request.form()

    name = (form.get("name") or "").strip()
    if not name:
        templates = request.app.state.templates
        return templates.TemplateResponse(
            "customers/edit.html",
            {
                "request": request,
                "lang": request.state.lang,
                "user": user,
                "form": {**_customer_form_from_row(row), **dict(form)},
                "is_new": False,
                "error": i18n.t("customer.error.name_required", request.state.lang),
            },
            status_code=400,
        )

    values = _values_from_form(form, is_new=False, existing=row)
    values["name"] = name
    values["updated_at"] = func.current_timestamp()

    with db.get_conn() as conn:
        conn.execute(
            update(customers).where(customers.c.id == customer_id).values(**values)
        )
    db.audit(user["id"], "customer.updated",
             target_type="customer", target_id=str(customer_id))

    return RedirectResponse(f"/customers/{customer_id}", status_code=303)


def _values_from_form(form, *, is_new: bool, existing: dict | None = None) -> dict:
    """Extract writeable customer columns from the submitted form.

    Passwords are treated as write-only: an empty submission means
    "keep the value already stored", not "clear the field".
    """
    values: dict = {
        "tenant_url":          (form.get("tenant_url") or "").strip(),
        "customer_number":     (form.get("customer_number") or "").strip(),
        "address":             (form.get("address") or "").strip(),
        "notes":               (form.get("notes") or "").strip(),
        "sql_server":          (form.get("sql_server") or "").strip(),
        "sql_database":        (form.get("sql_database") or "").strip(),
        "sql_port":     _parse_int(form.get("sql_port"), default=1433, lo=1, hi=65535),
        "sql_username":        (form.get("sql_username") or "").strip(),
        "alert_recipients_csv": (form.get("alert_recipients_csv") or "").strip(),
        "alert_min_level":     (form.get("alert_min_level") or "WARN").upper(),
        "order_recipients_csv": (form.get("order_recipients_csv") or "").strip(),
        "warn_pct":     _parse_int(form.get("warn_pct"),      default=20, lo=1, hi=99),
        "critical_pct": _parse_int(form.get("critical_pct"),  default=5,  lo=0, hi=98),
        "timezone":            (form.get("timezone") or "Europe/Berlin").strip(),
        "quiet_hours_start":   (form.get("quiet_hours_start") or "").strip(),
        "quiet_hours_end":     (form.get("quiet_hours_end") or "").strip(),
        "digest_mode":         _bool_form(form.get("digest_mode")),
        "auto_order_mode":     (form.get("auto_order_mode") or "off").lower(),
        "auto_order_daily_cap": _parse_int(form.get("auto_order_daily_cap"),
                                             default=10, lo=1, hi=100),
        "active":              _bool_form(form.get("active")),
    }

    # Normalise CHECK-constrained columns
    if values["alert_min_level"] not in ("INFO", "WARN", "CRITICAL"):
        values["alert_min_level"] = "WARN"
    if values["auto_order_mode"] not in ("off", "draft", "autonomous"):
        values["auto_order_mode"] = "off"

    # Password: encrypt when provided, otherwise keep existing.
    password = form.get("sql_password") or ""
    if password:
        values["sql_password_enc"] = crypto.encrypt(password)
    elif is_new:
        values["sql_password_enc"] = ""

    return values


# ---------------------------------------------------------------------------
# Detail (basic — P2 replaces with a toner-rich view)
# ---------------------------------------------------------------------------

@router.get("/customers/{customer_id}", response_class=HTMLResponse,
            include_in_schema=False)
async def customer_detail(customer_id: int, request: Request):
    user = auth.require_customer_access(request, customer_id)
    row = _customer_row(customer_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)

    with db.get_conn() as conn:
        access_rows = conn.execute(
            select(users.c.id, users.c.email, users.c.name, users.c.role,
                   customer_access.c.access_level)
            .select_from(
                users.join(
                    customer_access,
                    users.c.id == customer_access.c.user_id,
                )
            )
            .where(customer_access.c.customer_id == customer_id)
            .order_by(users.c.name)
        ).all()
        recent_events = conn.execute(
            select(audit_log.c.action, audit_log.c.created_at,
                   audit_log.c.meta_json, audit_log.c.user_id)
            .where(audit_log.c.target_type == "customer")
            .where(audit_log.c.target_id == str(customer_id))
            .order_by(audit_log.c.created_at.desc())
            .limit(10)
        ).all()

    templates = request.app.state.templates
    return templates.TemplateResponse(
        "customers/detail.html",
        {
            "request": request,
            "lang": request.state.lang,
            "user": user,
            "customer": _customer_form_from_row(row),
            "access": [dict(r._mapping) for r in access_rows],
            "recent_events": [dict(r._mapping) for r in recent_events],
            "anomalies": toner_alerts.list_recent_anomalies(customer_id),
        },
    )


# ---------------------------------------------------------------------------
# Savings report
# ---------------------------------------------------------------------------

@router.get("/customers/{customer_id}/savings", response_class=HTMLResponse,
            include_in_schema=False)
async def customer_savings_report(customer_id: int, request: Request):
    """v0.24.4 — Sparpotential-Report: real numbers from order history +
    stored supply pricing (never estimated), plus an optional AI
    narrative that phrases those exact numbers for a sales conversation.
    Same access model as the customer detail page."""
    user = auth.require_customer_access(request, customer_id)
    row = _customer_row(customer_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)

    facts = savings_report.compute_savings_facts(customer_id)
    narrative, narrative_error = savings_report.generate_savings_narrative(
        row["name"], facts, lang=request.state.lang)

    templates = request.app.state.templates
    return templates.TemplateResponse(
        "customers/savings_report.html",
        {
            "request": request,
            "lang": request.state.lang,
            "user": user,
            "customer": _customer_form_from_row(row),
            "facts": facts,
            "narrative": narrative,
            "narrative_error": narrative_error,
        },
    )


# ---------------------------------------------------------------------------
# Delete (soft — active=0; hard delete requires DB access)
# ---------------------------------------------------------------------------

@router.post("/customers/{customer_id}/delete", include_in_schema=False)
async def customer_delete(customer_id: int, request: Request):
    user = auth.require_admin(request)
    row = _customer_row(customer_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    with db.get_conn() as conn:
        conn.execute(
            update(customers).where(customers.c.id == customer_id).values(active=0)
        )
    db.audit(user["id"], "customer.deactivated",
             target_type="customer", target_id=str(customer_id))
    return RedirectResponse("/customers", status_code=303)


# ---------------------------------------------------------------------------
# Test-connection (POST; returns JSON so the form can render inline)
# ---------------------------------------------------------------------------

@router.post("/customers/test-connection", include_in_schema=False)
async def test_connection(request: Request):
    """Try the BI credentials submitted in the current edit form.

    Deliberately does NOT require an existing customer row — we run
    against the values in the request body so an admin can validate
    before saving a new customer.
    """
    auth.require_admin(request)
    form = await request.form()

    server = (form.get("sql_server") or "").strip()
    database = (form.get("sql_database") or "").strip()
    username = (form.get("sql_username") or "").strip()
    password = form.get("sql_password") or ""
    port = _parse_int(form.get("sql_port"), default=1433, lo=1, hi=65535)

    # v0.17.1 (security): if the admin ticked "use the stored password"
    # (password field left empty on an existing customer), the ENTIRE
    # connection identity must come from the stored row. Otherwise a
    # rogue admin session could point sql_server at their own SQL
    # host, exfiltrating every stored BI password by trying to auth
    # against it. Fresh passwords typed into the form use the form's
    # host normally.
    if not password and (form.get("customer_id") or "").isdigit():
        row = _customer_row(int(form["customer_id"]))
        if row and row["sql_password_enc"]:
            password = crypto.decrypt(row["sql_password_enc"])
            # Override attacker-supplied identity with the stored one
            server = row["sql_server"] or server
            database = row["sql_database"] or database
            username = row["sql_username"] or username
            if row.get("sql_port"):
                port = int(row["sql_port"])

    result = bi_client.test_connection(server, database, username, password,
                                       port=port)
    return JSONResponse(
        {"ok": result.ok, "message": result.message,
         "server_version": result.server_version},
        status_code=200,
    )
