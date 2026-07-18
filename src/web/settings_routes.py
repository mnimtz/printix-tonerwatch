"""Instance-wide settings — mail provider, alert cadence, test-mail button."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .. import (auth, azure_mgmt, backup, bi_client, db, entra_sso,
                 graph_connector, llm_client, mail_client, runner_config,
                 toner_alerts)
from ..db import customers as customers_tbl


router = APIRouter()
logger = logging.getLogger(__name__)


def _short_hash(value: str) -> str:
    """8-hex-char fingerprint for correlating log lines across requests
    without ever writing the actual device_code / access_token to
    logs — short-lived credentials, but no reason to log them in full."""
    return hashlib.sha256(value.encode()).hexdigest()[:8]


# v0.24.23: the device-code exchange's access_token is a full Entra JWT
# (often 2-3 KB) — stashing it in the signed session cookie pushed the
# cookie's total size over the ~4096-byte limit browsers enforce per
# cookie whenever the rest of the session (CSRF token, language,
# user_id) was already a few KB, causing the browser to silently drop
# the Set-Cookie response and lose the device_code stashed moments
# earlier (confirmed via DevTools: Chrome flagged the Set-Cookie as
# "Malformed" and kept sending the stale cookie). Kept server-side
# instead, keyed by admin id, with a short TTL — it's only ever read
# by the SAME admin's own follow-up click (existing_found →
# replace/rotate/grant) within the same setup session.
_ENTRA_SETUP_TOKEN_TTL = 15 * 60  # matches the device-code flow's own expiry
_entra_setup_tokens: dict[int, tuple[str, float]] = {}


def _stash_setup_token(admin_id: int, access_token: str) -> None:
    _entra_setup_tokens[admin_id] = (access_token, time.monotonic())


def _take_setup_token(admin_id: int, *, pop: bool) -> str:
    entry = _entra_setup_tokens.get(admin_id)
    if not entry:
        return ""
    token, stored_at = entry
    if time.monotonic() - stored_at > _ENTRA_SETUP_TOKEN_TTL:
        _entra_setup_tokens.pop(admin_id, None)
        return ""
    if pop:
        _entra_setup_tokens.pop(admin_id, None)
    return token


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
            "entra_secret_status": entra_sso.secret_expiry_status(),
            "llm":    llm_client.load_config(),
            "graph":  graph_connector.load_config(),
            "runner": runner_config.load_config(),
            "info":   request.query_params.get("info", ""),
            "error":  request.query_params.get("error", ""),
        },
    )


@router.get("/settings/database", response_class=HTMLResponse,
            include_in_schema=False)
async def settings_database_page(request: Request):
    """v0.23.0 — dedicated Database Setup page. Shows what backend
    the running process is actually talking to, lets the admin test
    an Azure SQL configuration WITHOUT flipping the live engine,
    and hands back a copy-ready DATABASE_URL for Azure App Service."""
    admin = auth.require_admin(request)
    return request.app.state.templates.TemplateResponse(
        "settings/database.html",
        {"request": request, "lang": request.state.lang,
         "user": admin, "active": db.describe_active_backend(),
         "info":  request.query_params.get("info", ""),
         "error": request.query_params.get("error", "")},
    )


@router.post("/settings/database/test", include_in_schema=False)
async def settings_database_test(request: Request):
    """Test-connect against an Azure SQL config supplied via the form.
    Returns JSON so the settings page can update its status inline.
    Never persists anything."""
    auth.require_admin(request)
    form = await request.form()
    server   = (form.get("server") or "").strip()
    database = (form.get("database") or "").strip()
    username = (form.get("username") or "").strip()
    password = (form.get("password") or "").strip()
    if not (server and database and username and password):
        return JSONResponse(
            {"ok": False, "error": "server/database/username/password required"},
            status_code=400)
    url = db.build_azure_sql_url(server, database, username, password)
    ok, detail = db.try_connect(url, timeout=8.0)
    if not ok:
        return JSONResponse(
            {"ok": False, "error": detail,
             "database_url_masked": db._mask_password(url)},
            status_code=400)
    return JSONResponse(
        {"ok": True, "detail": "SELECT 1 succeeded",
         "database_url": url,
         "database_url_masked": db._mask_password(url)})


@router.get("/settings/database/automation_status", include_in_schema=False)
async def settings_database_automation_status(request: Request):
    """v0.24.3 — can this process switch its own DATABASE_URL via its
    Managed Identity? Never touches app settings, just probes IMDS.
    ``resource_group`` is optional: App Service doesn't expose its own
    resource group name until AZURE_RESOURCE_GROUP is set (which is
    exactly what's missing in the one-time-setup case), so the admin
    can type it in and the bootstrap snippet fills it in live."""
    auth.require_admin(request)
    result = azure_mgmt.probe()
    if not result.get("ok") and result.get("hint") == "one_time_setup_needed":
        rg = (request.query_params.get("resource_group") or "").strip()
        result["bootstrap"] = azure_mgmt.bootstrap_instructions(resource_group=rg)
    return JSONResponse(result)


@router.post("/settings/database/switch", include_in_schema=False)
async def settings_database_switch(request: Request):
    """v0.24.3 — the automated cutover: re-validate the connection with
    a fresh SELECT 1 (never trust a URL that only came from the client),
    then hand off to azure_mgmt to merge DATABASE_URL into the live
    App Service settings and restart. Admin-only, audited."""
    admin = auth.require_admin(request)
    form = await request.form()
    url = (form.get("database_url") or "").strip()
    if not url:
        return JSONResponse({"ok": False, "error": "database_url required"},
                             status_code=400)
    ok, detail = db.try_connect(url, timeout=8.0)
    if not ok:
        return JSONResponse(
            {"ok": False, "error": f"pre-flight check failed: {detail}"},
            status_code=400)
    result = azure_mgmt.switch_database_url(url)
    if result.get("ok"):
        db.audit(admin["id"], "settings.database_switched",
                  meta_json=json.dumps({"database_url_masked": db._mask_password(url)}))
    return JSONResponse(result, status_code=(200 if result.get("ok") else 400))


@router.get("/settings/mail/graph/auth_probe", include_in_schema=False)
async def settings_mail_graph_auth_probe(request: Request):
    """v0.23.3 — non-destructive Graph client-credentials sanity check.
    Runs one POST /oauth2/v2.0/token, reports success or an actionable
    AADSTS diagnosis. Admin-only. Never sends mail."""
    auth.require_admin(request)
    result = mail_client.graph_auth_probe()
    return JSONResponse(result, status_code=(200 if result.get("ok") else 400))


@router.get("/settings/mail/graph/mailboxes", include_in_schema=False)
async def settings_mail_graph_mailboxes(request: Request):
    """v0.22.0 — populate the sender-mailbox dropdown for the Graph
    mail provider from the Entra app registration's User.Read.All
    permission. Returns [] silently if Graph is unreachable, permission
    is missing, or Entra isn't configured — the UI falls back to a
    free-text UPN input in that case."""
    auth.require_admin(request)
    cfg = mail_client.load_config()
    mailboxes = mail_client.list_graph_mailboxes(cfg)
    return JSONResponse({"ok": True,
                          "count": len(mailboxes),
                          "mailboxes": mailboxes[:500]})


@router.post("/settings/mail", include_in_schema=False)
async def settings_mail_save(request: Request):
    admin = auth.require_admin(request)
    form = await request.form()

    provider = (form.get("provider") or "disabled").strip().lower()
    if provider not in ("disabled", "resend", "smtp", "graph"):
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
        "graph_mailbox_upn": (form.get("graph_mailbox_upn") or "").strip().lower(),
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
        logger.warning("[entra-autosetup] start: Microsoft rejected the "
                        "device-code request for admin=%s: %s",
                        admin["id"], d["error"])
        return JSONResponse({"ok": False, "error": d["error"]}, status_code=400)
    # Session-stash for the poll endpoint. Device codes are single-use;
    # the admin's browser session owns this until they finish or it
    # expires (15 min default).
    try:
        # v0.24.23: one-time cleanup — older sessions (pre-v0.24.23)
        # may still carry a leftover entra_setup_access_token that used
        # to live in the cookie itself (see _stash_setup_token above).
        # That's what bloated the cookie past the ~4KB browser limit
        # and caused the very bug this endpoint exists to recover
        # from — drop it unconditionally so a stale cookie doesn't
        # keep re-triggering the same failure on every new attempt.
        request.session.pop("entra_setup_access_token", None)
        request.session["entra_setup_device_code"] = d["device_code"]
        request.session["entra_setup_tenant"] = tenant
    except AssertionError:
        logger.warning("[entra-autosetup] start: request.session raised "
                        "AssertionError for admin=%s — SessionMiddleware "
                        "not active on this request?", admin["id"])
        return JSONResponse({"ok": False, "error": "session unavailable"},
                            status_code=500)
    # v0.24.21: diagnostic breadcrumb — the next /poll for this admin
    # should log the SAME hash. If it logs "missing" or a DIFFERENT
    # hash instead, the session isn't round-tripping between requests
    # (cookie not being set/sent, or a second /start overwrote it).
    logger.info("[entra-autosetup] start: admin=%s device_code_hash=%s "
                "stashed in session, expires_in=%ss",
                admin["id"], _short_hash(d["device_code"]), d.get("expires_in"))
    db.audit(admin["id"], "settings.entra_autosetup_started",
             target_type="settings", target_id="entra_sso",
             meta_json=json.dumps({"user_code_len": len(d["user_code"]),
                                    "device_code_hash": _short_hash(d["device_code"])}))
    return JSONResponse({
        "ok": True,
        "user_code":        d["user_code"],
        "verification_uri": d["verification_uri"],
        "expires_in":       d["expires_in"],
        "interval":         d["interval"],
    })


@router.post("/settings/entra/autosetup/rotate_secret", include_in_schema=False)
async def entra_autosetup_rotate_secret(request: Request):
    """v0.23.4 — mint a new client_secret on the EXISTING app
    registration (rather than creating a whole new orphan app the way
    /replace does). Uses the access_token from the device-code poll
    that put us on the "existing_found" panel, and the object_id
    from the form (populated by the JS from the existing[0] result).
    tenant_id, redirect_uri, provisioning flags are preserved."""
    admin = auth.require_admin(request)
    # v0.24.9: not popped here — so the same device-code token can also
    # back the "Grant Mail.Send" button below without forcing a second
    # device-code login just because the admin clicked rotate first.
    access_token = _take_setup_token(admin["id"], pop=False)
    if not access_token:
        return JSONResponse({"ok": False, "status": "error",
                              "error": "no_access_token_in_session"},
                             status_code=400)
    form = await request.form()
    object_id = (form.get("object_id") or "").strip()
    client_id = (form.get("client_id") or "").strip()
    if not object_id:
        return JSONResponse({"ok": False, "status": "error",
                              "error": "object_id required"},
                             status_code=400)
    try:
        rot = entra_sso.rotate_client_secret(access_token, object_id)
    except entra_sso.EntraSSOError as e:
        db.audit(admin["id"], "settings.entra_secret_rotate_failed",
                 target_type="settings", target_id="entra_sso",
                 meta_json=json.dumps({"error": str(e)[:300]}))
        return JSONResponse({"ok": False, "status": "error",
                              "error": str(e)[:400]}, status_code=500)
    # Merge into existing config: replace client_secret (+ its expiry)
    # and — v0.24.25 — client_id. A secret only works paired with the
    # SAME app it was minted on; when several "Printix TonerWatch"
    # registrations exist in the tenant (find_existing_apps returned
    # more than one), the object_id the secret was rotated on isn't
    # guaranteed to be the one the currently-stored client_id refers
    # to. Keeping the old client_id here silently paired a fresh,
    # valid secret with the WRONG app — every token request then fails
    # with AADSTS7000215 even though nothing looks broken in the UI.
    cfg = entra_sso.load_config()
    cfg["client_secret"] = rot["client_secret"]
    cfg["secret_expires_at"] = rot["secret_expires_at"]
    if client_id:
        cfg["client_id"] = client_id
    entra_sso.save_config(cfg)
    db.audit(admin["id"], "settings.entra_secret_rotated",
             target_type="settings", target_id="entra_sso",
             meta_json=json.dumps({"object_id": object_id,
                                    "client_id": client_id,
                                    "expires_at": rot["secret_expires_at"]}))
    return JSONResponse({"ok": True, "status": "done",
                          "secret_expires_at": rot["secret_expires_at"]})


@router.post("/settings/entra/autosetup/grant_mail_permissions",
             include_in_schema=False)
async def entra_autosetup_grant_mail_permissions(request: Request):
    """v0.24.9 — the automated version of the manual "3 Schritte"
    Mail.Send instructions, for an app that was registered before this
    existed (or where the very first auto-grant attempt failed).
    Same access_token + object_id pattern as rotate_secret. A 403-style
    EntraSSOError here means the signed-in admin isn't a tenant Global
    Administrator — caller falls back to the manual Azure Portal steps,
    which this doesn't disable."""
    admin = auth.require_admin(request)
    access_token = _take_setup_token(admin["id"], pop=False)
    if not access_token:
        return JSONResponse({"ok": False, "status": "error",
                              "error": "no_access_token_in_session"},
                             status_code=400)
    form = await request.form()
    object_id = (form.get("object_id") or "").strip()
    client_id = (form.get("client_id") or "").strip() or entra_sso.load_config().get("client_id", "")
    if not object_id or not client_id:
        return JSONResponse({"ok": False, "status": "error",
                              "error": "object_id and client_id required"},
                             status_code=400)
    try:
        result = entra_sso.grant_mail_send_permissions(access_token, object_id, client_id)
    except entra_sso.EntraSSOError as e:
        db.audit(admin["id"], "settings.entra_mail_permissions_grant_failed",
                 target_type="settings", target_id="entra_sso",
                 meta_json=json.dumps({"error": str(e)[:300]}))
        return JSONResponse({"ok": False, "status": "error",
                              "error": str(e)[:400]}, status_code=500)
    db.audit(admin["id"], "settings.entra_mail_permissions_granted",
             target_type="settings", target_id="entra_sso",
             meta_json=json.dumps(result))
    return JSONResponse({"ok": True, "status": "done", **result})


@router.post("/settings/entra/autosetup/replace", include_in_schema=False)
async def entra_autosetup_replace(request: Request):
    """v0.17.2: admin saw existing apps in the "existing_found"
    branch and clicked "create new anyway". Reuses the access-token
    from the session so no second device-code login is needed."""
    admin = auth.require_admin(request)
    access_token = _take_setup_token(admin["id"], pop=True)
    if not access_token:
        return JSONResponse({"ok": False, "status": "error",
                              "error": "no_access_token_in_session"},
                             status_code=400)
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
             meta_json=json.dumps({"tenant_id": result["tenant_id"],
                                    "client_id": result["client_id"],
                                    "replaced_existing": True}))
    return JSONResponse({"ok": True, "status": "done",
                          "tenant_id": result["tenant_id"],
                          "client_id": result["client_id"],
                          "admin_consent": result["admin_consent"],
                          "secret_expires_at": result["secret_expires_at"]})


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
        session_error = None
    except AssertionError:
        device_code = ""
        tenant = "common"
        session_error = "AssertionError reading request.session"
    if not device_code:
        # v0.24.21: this is OUR OWN session check, not something
        # Microsoft returned — if this fires on the very first poll
        # after a fresh /start, the session cookie set by /start's
        # response isn't being read back on this request. Logged at
        # WARNING (not INFO) since it's the exact failure this
        # diagnostic exists to catch.
        logger.warning("[entra-autosetup] poll: admin=%s no device_code in "
                        "session (session_error=%s) — either /start never "
                        "ran for this session, a later /start already "
                        "consumed/replaced it, or the session cookie isn't "
                        "round-tripping between requests",
                        admin["id"], session_error)
        # v0.24.22: mirror the warning into the audit log — Azure's Log
        # Stream for custom containers has proven unreliable to actually
        # show this (nothing appeared across several real attempts), but
        # the DB write is unconditional and already surfaced on
        # /settings/entra/diagnose without needing Kudu at all.
        db.audit(admin["id"], "settings.entra_autosetup_poll_no_session",
                 target_type="settings", target_id="entra_sso",
                 meta_json=json.dumps({"session_error": session_error}))
        return JSONResponse({"ok": False,
                              "status": "error",
                              "error": "no_device_code_in_session"},
                             status_code=400)

    logger.info("[entra-autosetup] poll: admin=%s device_code_hash=%s "
                "found in session, polling Microsoft",
                admin["id"], _short_hash(device_code))
    poll = entra_sso.poll_device_code_token(device_code, tenant=tenant)
    status = poll.get("status")
    if status != "success":
        if status == "error":
            logger.warning("[entra-autosetup] poll: admin=%s Microsoft "
                            "returned status=error: %s",
                            admin["id"], poll.get("error", ""))
        return JSONResponse({"ok": True, "status": status,
                              "error": poll.get("error", "")})
    logger.info("[entra-autosetup] poll: admin=%s device-code exchange "
                "succeeded, proceeding to auto-registration", admin["id"])

    # Success — clear the device_code from the session so a stale
    # code can't be re-used, then run the auto-registration.
    try:
        request.session.pop("entra_setup_device_code", None)
        request.session.pop("entra_setup_tenant", None)
    except AssertionError:
        pass

    access_token = poll["access_token"]
    redirect_uri = f"{str(request.base_url).rstrip('/')}/auth/entra/callback"

    # v0.17.2: refuse to blindly create a second/third/fourth App
    # Registration named "Printix TonerWatch". If one exists, hand
    # the decision back to the admin — usually they want to reuse
    # the existing app (secret rotation → mint fresh) rather than
    # accumulate orphan registrations.
    if not (form_flag := (request.query_params.get("confirm_replace") == "1")):
        existing = entra_sso.find_existing_apps(access_token, "Printix TonerWatch")
        if existing:
            # Stash the access_token so the admin's next action
            # (replace / reuse / cancel) doesn't need to redo the
            # device-code flow. Server-side (see _stash_setup_token) —
            # not the session cookie, which is how it got lost before.
            _stash_setup_token(admin["id"], access_token)
            db.audit(admin["id"], "settings.entra_autosetup_existing_found",
                     target_type="settings", target_id="entra_sso",
                     meta_json=json.dumps({"count": len(existing)}))
            return JSONResponse({
                "ok": True, "status": "existing_found",
                "existing": [{
                    "id":               a.get("id"),
                    "appId":            a.get("appId"),
                    "displayName":      a.get("displayName"),
                    "createdDateTime":  a.get("createdDateTime"),
                    "redirectUris":     (a.get("web") or {}).get("redirectUris") or [],
                } for a in existing[:10]],
            })

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
    try:
        history_days = int(form.get("toner_history_raw_retention_days") or 90)
    except ValueError:
        history_days = 90
    runner_config.save_config(alert_min, refresh_min, history_days)
    db.audit(admin["id"], "settings.runner_updated",
             target_type="settings", target_id="runner",
             meta_json=json.dumps({"alert_min": alert_min,
                                    "refresh_min": refresh_min,
                                    "history_days": history_days}))
    return RedirectResponse("/settings?info=runner_saved#runner",
                            status_code=303)


@router.get("/settings/entra/diagnose", response_class=HTMLResponse,
            include_in_schema=False)
async def entra_diagnose(request: Request):
    """v0.18.4 — surface every fact the SSO flow depends on so an
    admin can figure out WHY the login loop happens without needing
    Azure Log Stream. Shows: current entra_sso config (secret
    masked), the last 30 audit rows that mention SSO/entra, the
    local user accounts that would match a given SSO email, and
    the runtime redirect_uri the app would send to Microsoft."""
    admin = auth.require_admin(request)
    cfg = entra_sso.load_config()
    from sqlalchemy import select as _sel, or_ as _or_
    with db.get_conn() as conn:
        rows = conn.execute(
            _sel(db.audit_log.c.created_at, db.audit_log.c.user_id,
                 db.audit_log.c.action, db.audit_log.c.target_type,
                 db.audit_log.c.target_id, db.audit_log.c.meta_json)
            .where(_or_(
                db.audit_log.c.action.like("settings.entra%"),
                db.audit_log.c.action.like("user.login.entra%"),
                db.audit_log.c.action.like("auth.sso%"),
            ))
            .order_by(db.audit_log.c.created_at.desc())
            .limit(30)
        ).all()
        events = [{"created_at": r.created_at, "user_id": r.user_id,
                    "action": r.action, "target": (r.target_type or "") + "/" + (r.target_id or ""),
                    "meta": r.meta_json or ""} for r in rows]

        # Which local users could match an incoming SSO login?
        user_rows = conn.execute(
            _sel(db.users.c.id, db.users.c.email,
                 db.users.c.role, db.users.c.active,
                 db.users.c.entra_oid)
            .order_by(db.users.c.email)
        ).all()
        users_summary = [{"id": r.id, "email": r.email, "role": r.role,
                          "active": bool(r.active),
                          "entra_oid": (r.entra_oid or "")[:8] + "…" if r.entra_oid else ""}
                         for r in user_rows]

    base = str(request.base_url).rstrip("/")
    derived_redirect = f"{base}/auth/entra/callback"
    return request.app.state.templates.TemplateResponse(
        "settings/entra_diagnose.html",
        {
            "request": request, "lang": request.state.lang, "user": admin,
            "cfg": {
                "enabled":              cfg["enabled"],
                "tenant_id":            cfg["tenant_id"],
                "client_id":            cfg["client_id"],
                "client_secret_masked": ("*" * 8 + "(stored)"
                                          if cfg["client_secret"] else "(missing)"),
                "redirect_uri":         cfg["redirect_uri"],
                "allow_auto_provision": cfg["allow_auto_provision"],
                "auto_provision_domain": cfg["auto_provision_domain"],
                "default_role":         cfg["default_role"],
                "is_configured":        entra_sso.is_configured(),
            },
            "derived_redirect": derived_redirect,
            "base_url":         base,
            "events":           events,
            "users_summary":    users_summary,
        },
    )


@router.post("/settings/entra/toggle", include_in_schema=False)
async def settings_entra_toggle(request: Request):
    """v0.18.3: flip only the `enabled` flag without touching the
    credentials. Fixes the "I configured SSO but the login button
    doesn't show up" trap: credentials are stored, but `enabled=False`
    hides the button. The manual-config form's Enable checkbox
    silently overwrites the auto-setup's `enabled=True` when the
    admin re-saves it without ticking the box; this endpoint gives
    them a one-click way back in."""
    admin = auth.require_admin(request)
    cfg = entra_sso.load_config()
    cfg["enabled"] = not cfg["enabled"]
    entra_sso.save_config(cfg)
    db.audit(admin["id"], "settings.entra_toggled",
             target_type="settings", target_id="entra_sso",
             meta_json=json.dumps({"enabled": cfg["enabled"]}))
    return RedirectResponse("/settings?info=entra_toggled#entra",
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


@router.post("/settings/llm/models", include_in_schema=False)
async def settings_llm_models(request: Request):
    """v0.19.0 — probe the LLM provider for its model list so the
    admin can pick from a real dropdown instead of typing a model
    identifier from memory (and getting a 404 at first use).

    Called via JS from settings/index.html after the admin picks a
    provider and enters an api_key. Uses the freshly-typed key even
    if the settings haven't been saved yet — otherwise the admin
    would have to save+refresh+edit to see the list."""
    auth.require_admin(request)
    form = await request.form()
    provider = (form.get("provider") or "").strip().lower()
    api_key  = (form.get("api_key") or "").strip()
    endpoint = (form.get("endpoint") or "").strip()
    azure_api_version = (form.get("azure_api_version") or "2024-06-01").strip()

    # If the admin left api_key blank (e.g. "keep existing"), fall
    # back to whatever's stored so they don't have to re-enter it.
    if not api_key:
        stored = llm_client.load_config()
        if stored.get("provider") == provider and stored.get("api_key"):
            api_key = stored["api_key"]
        if not endpoint and stored.get("endpoint"):
            endpoint = stored["endpoint"]

    try:
        models = llm_client.list_models(
            provider, api_key=api_key, endpoint=endpoint,
            azure_api_version=azure_api_version)
    except llm_client.ModelListError as e:
        return JSONResponse({"ok": False, "error": str(e)[:300]},
                             status_code=400)
    except Exception as e:  # noqa: BLE001
        return JSONResponse(
            {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"},
            status_code=500)
    return JSONResponse({"ok": True, "provider": provider,
                          "models": models})


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
    """v0.23.1 — a Gemini config with an unusual model name (e.g.
    just 'flash') used to return HTML 500 because httpx/JSON-parse
    errors slipped through the narrow LLMError catch. Broaden to
    Exception so ALL failures come back as a readable
    ?error=llm_test_... on /settings#llm."""
    import logging as _logging
    _log = _logging.getLogger("tonerwatch.llm")
    auth.require_admin(request)
    try:
        r = llm_client.chat(
            "You are a laconic assistant. Reply with exactly one word.",
            "Say 'hello'.")
    except llm_client.LLMError as e:
        return RedirectResponse(
            f"/settings?error=llm_test_{str(e)[:200].replace('&','')}#llm",
            status_code=303)
    except Exception as e:  # noqa: BLE001
        _log.exception("[LLM test] unexpected exception")
        return RedirectResponse(
            f"/settings?error=llm_test_{type(e).__name__}%3A%20"
            f"{str(e)[:180].replace('&','')}#llm",
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
