"""Instance-wide settings — mail provider, alert cadence, test-mail button."""

from __future__ import annotations

import json
import os
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .. import (auth, backup, bi_client, db, entra_sso, graph_connector,
                 llm_client, mail_client, runner_config, toner_alerts)
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
            "llm":    llm_client.load_config(),
            "graph":  graph_connector.load_config(),
            "runner": runner_config.load_config(),
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


@router.post("/settings/entra/autosetup/start", include_in_schema=False)
async def entra_autosetup_start(request: Request):
    """Kick off the Entra device-code flow. Returns the user_code
    the admin must type at microsoft.com/devicelogin plus the
    verification URI. Stashes the device_code + start-time in the
    session so the poll endpoint can complete the exchange."""
    admin = auth.require_admin(request)
    tenant = "common"  # multi-tenant device-code — Microsoft resolves
    d = entra_sso.start_device_code_flow(tenant=tenant)
    if "error" in d:
        return JSONResponse({"ok": False, "error": d["error"]}, status_code=400)
    # Session-stash for the poll endpoint. Device codes are single-use;
    # the admin's browser session owns this until they finish or it
    # expires (15 min default).
    try:
        request.session["entra_setup_device_code"] = d["device_code"]
        request.session["entra_setup_tenant"] = tenant
    except AssertionError:
        return JSONResponse({"ok": False, "error": "session unavailable"},
                            status_code=500)
    db.audit(admin["id"], "settings.entra_autosetup_started",
             target_type="settings", target_id="entra_sso",
             meta_json=json.dumps({"user_code_len": len(d["user_code"])}))
    return JSONResponse({
        "ok": True,
        "user_code":        d["user_code"],
        "verification_uri": d["verification_uri"],
        "expires_in":       d["expires_in"],
        "interval":         d["interval"],
    })


@router.post("/settings/entra/autosetup/poll", include_in_schema=False)
async def entra_autosetup_poll(request: Request):
    """Poll Microsoft's token endpoint once. If the admin has
    completed device-code auth, exchange the token, auto-register
    the App Registration + secret + consent, save the config, and
    return ``{status: "done"}``. Otherwise pending/expired/error."""
    admin = auth.require_admin(request)
    try:
        device_code = request.session.get("entra_setup_device_code", "")
        tenant = request.session.get("entra_setup_tenant", "common")
    except AssertionError:
        device_code = ""
        tenant = "common"
    if not device_code:
        return JSONResponse({"ok": False,
                              "status": "error",
                              "error": "no_device_code_in_session"},
                             status_code=400)

    poll = entra_sso.poll_device_code_token(device_code, tenant=tenant)
    status = poll.get("status")
    if status != "success":
        return JSONResponse({"ok": True, "status": status,
                              "error": poll.get("error", "")})

    # Success — clear the device_code from the session so a stale
    # code can't be re-used, then run the auto-registration.
    try:
        request.session.pop("entra_setup_device_code", None)
        request.session.pop("entra_setup_tenant", None)
    except AssertionError:
        pass

    access_token = poll["access_token"]
    redirect_uri = f"{str(request.base_url).rstrip('/')}/auth/entra/callback"
    try:
        result = entra_sso.auto_register_app(
            access_token, redirect_uri=redirect_uri,
            app_name="Printix TonerWatch")
    except entra_sso.EntraSSOError as e:
        db.audit(admin["id"], "settings.entra_autosetup_failed",
                 target_type="settings", target_id="entra_sso",
                 meta_json=json.dumps({"error": str(e)[:300]}))
        return JSONResponse({"ok": False, "status": "error",
                              "error": str(e)[:400]}, status_code=500)

    entra_sso.apply_auto_setup_result(result, redirect_uri=redirect_uri)
    db.audit(admin["id"], "settings.entra_autosetup_done",
             target_type="settings", target_id="entra_sso",
             meta_json=json.dumps({
                 "tenant_id": result["tenant_id"],
                 "client_id": result["client_id"],
                 "admin_consent": result["admin_consent"],
                 "secret_expires_at": result["secret_expires_at"]}))
    return JSONResponse({
        "ok": True,
        "status": "done",
        "tenant_id": result["tenant_id"],
        "client_id": result["client_id"],
        "admin_consent": result["admin_consent"],
        "secret_expires_at": result["secret_expires_at"],
    })


@router.post("/settings/runner", include_in_schema=False)
async def settings_runner_save(request: Request):
    admin = auth.require_admin(request)
    form = await request.form()
    try:
        alert_min = int(form.get("alert_interval_minutes") or 15)
    except ValueError:
        alert_min = 15
    try:
        refresh_min = int(form.get("refresh_interval_minutes") or 5)
    except ValueError:
        refresh_min = 5
    runner_config.save_config(alert_min, refresh_min)
    db.audit(admin["id"], "settings.runner_updated",
             target_type="settings", target_id="runner",
             meta_json=json.dumps({"alert_min": alert_min,
                                    "refresh_min": refresh_min}))
    return RedirectResponse("/settings?info=runner_saved#runner",
                            status_code=303)


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


@router.post("/settings/llm", include_in_schema=False)
async def settings_llm_save(request: Request):
    admin = auth.require_admin(request)
    form = await request.form()
    cfg = {
        "provider":          form.get("provider") or "disabled",
        "model":             form.get("model") or "",
        "api_key":           form.get("api_key") or "",
        "endpoint":          form.get("endpoint") or "",
        "azure_api_version": form.get("azure_api_version") or "2024-06-01",
        "temperature":       form.get("temperature") or 0.2,
        "max_tokens":        form.get("max_tokens") or 512,
    }
    llm_client.save_config(cfg)
    db.audit(admin["id"], "settings.llm_updated",
             target_type="settings", target_id="llm",
             meta_json=json.dumps({"provider": cfg["provider"],
                                    "model": cfg["model"]}))
    return RedirectResponse("/settings?info=llm_saved#llm", status_code=303)


@router.post("/settings/llm/test", include_in_schema=False)
async def settings_llm_test(request: Request):
    auth.require_admin(request)
    try:
        r = llm_client.chat(
            "You are a laconic assistant. Reply with exactly one word.",
            "Say 'hello'.")
    except llm_client.LLMError as e:
        return RedirectResponse(
            f"/settings?error=llm_test_{str(e)[:120].replace('&','')}#llm",
            status_code=303)
    return RedirectResponse(
        f"/settings?info=llm_test_ok_{r.provider}#llm", status_code=303)


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
