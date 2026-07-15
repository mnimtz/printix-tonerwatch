"""Instance-wide settings — mail provider, alert cadence, test-mail button."""

from __future__ import annotations

import json
import os
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .. import auth, backup, bi_client, db, entra_sso, mail_client, toner_alerts
from ..db import customers as customers_tbl


router = APIRouter()


# ---------------------------------------------------------------------------
# Settings page
# ---------------------------------------------------------------------------

@router.get("/settings", response_class=HTMLResponse, include_in_schema=False)
async def settings_page(request: Request):
    user = auth.require_admin(request)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        "settings/index.html",
        {
            "request": request,
            "lang": request.state.lang,
            "user": user,
            "mail":   mail_client.load_config(),
            "backup": backup.load_config(),
            "entra":  entra_sso.load_config(),
            "info":   request.query_params.get("info", ""),
            "error":  request.query_params.get("error", ""),
        },
    )


@router.post("/settings/mail", include_in_schema=False)
async def settings_mail_save(request: Request):
    admin = auth.require_admin(request)
    form = await request.form()

    provider = (form.get("provider") or "disabled").strip().lower()
    if provider not in ("disabled", "resend", "smtp"):
        provider = "disabled"

    cfg: dict[str, Any] = {
        "provider":       provider,
        "from_email":     (form.get("from_email") or "").strip(),
        "from_name":      (form.get("from_name") or "").strip(),
        "resend_api_key": form.get("resend_api_key") or "",
        "smtp_host":      (form.get("smtp_host") or "").strip(),
        "smtp_port":      int(form.get("smtp_port") or 587),
        "smtp_username":  (form.get("smtp_username") or "").strip(),
        "smtp_password":  form.get("smtp_password") or "",
        "smtp_starttls":  bool(form.get("smtp_starttls")),
    }
    mail_client.save_config(cfg)
    db.audit(admin["id"], "settings.mail_updated",
             target_type="settings", target_id=mail_client.SETTINGS_KEY,
             meta_json=json.dumps({"provider": provider,
                                   "from_email": cfg["from_email"]}))
    return RedirectResponse("/settings?info=mail_saved", status_code=303)


@router.post("/settings/entra", include_in_schema=False)
async def settings_entra_save(request: Request):
    admin = auth.require_admin(request)
    form = await request.form()
    cfg = {
        "enabled":            bool(form.get("enabled")),
        "tenant_id":          form.get("tenant_id") or "",
        "client_id":          form.get("client_id") or "",
        "client_secret":      form.get("client_secret") or "",
        "redirect_uri":       form.get("redirect_uri") or "",
        "allow_auto_provision": bool(form.get("allow_auto_provision")),
        "auto_provision_domain": form.get("auto_provision_domain") or "",
        "default_role":       form.get("default_role") or "technician",
    }
    entra_sso.save_config(cfg)
    db.audit(admin["id"], "settings.entra_updated",
             target_type="settings", target_id="entra_sso",
             meta_json=json.dumps({"enabled": cfg["enabled"],
                                    "tenant_id": cfg["tenant_id"]}))
    return RedirectResponse("/settings?info=entra_saved#entra", status_code=303)


@router.post("/settings/mail/test", include_in_schema=False)
async def settings_mail_test(request: Request):
    admin = auth.require_admin(request)
    to = (admin["email"] or "").strip()
    if not to:
        return RedirectResponse("/settings?error=no_admin_email", status_code=303)
    subject = "TonerWatch — Test-Mail"
    html = ("""<!doctype html><html><body style="font-family:Arial;">
        <h2 style="color:#002854;">TonerWatch: Test-Mail</h2>
        <p>If you see this in your inbox, your mail configuration works.</p>
        </body></html>""")
    text = "TonerWatch: If you see this, your mail configuration works."
    try:
        mail_client.send([to], subject, html, text)
        db.audit(admin["id"], "settings.mail_test_ok",
                 target_type="settings", target_id="mail")
        return RedirectResponse("/settings?info=test_sent", status_code=303)
    except mail_client.MailSendError as e:
        return RedirectResponse(
            "/settings?error=" + str(e)[:200].replace("&", ""),
            status_code=303)


# ---------------------------------------------------------------------------
# Per-customer "run alert eval now" (admin action from the customer page)
# ---------------------------------------------------------------------------

@router.post("/customers/{customer_id}/alerts/run", include_in_schema=False)
async def alerts_run_now(customer_id: int, request: Request):
    admin = auth.require_admin(request)
    from sqlalchemy import select
    with db.get_conn() as conn:
        row = conn.execute(
            select(customers_tbl).where(customers_tbl.c.id == customer_id)
        ).first()
    if row is None:
        return JSONResponse({"ok": False, "error": "customer_not_found"},
                            status_code=404)
    customer = db._row_to_dict(row)
    summary = toner_alerts.evaluate_and_notify(customer, force_refresh=True)
    db.audit(admin["id"], "alert.manual_run",
             target_type="customer", target_id=str(customer_id),
             meta_json=json.dumps(summary))
    return JSONResponse({"ok": True, **summary})
