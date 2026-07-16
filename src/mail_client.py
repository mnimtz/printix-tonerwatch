"""Unified mail sender — Resend HTTP API (preferred) or SMTP fallback.

Central mail config lives in the ``settings`` table under key ``mail`` as
a JSON blob::

    {
      "provider": "resend" | "smtp" | "disabled",
      "from_email": "toner@msp.example",
      "from_name": "MSP Printix TonerWatch",
      "resend_api_key_enc": "gAAAAA…",           # Fernet-encrypted
      "smtp_host": "smtp.example.com",
      "smtp_port": 587,
      "smtp_username": "…",
      "smtp_password_enc": "gAAAAA…",            # Fernet-encrypted
      "smtp_starttls": true
    }

Resend was picked as the default because Azure App Service blocks
outbound port 25 (SMTP) by default — HTTPS to api.resend.com goes through
without any allow-list dance. On-prem operators fall back to SMTP.
"""

from __future__ import annotations

import json
import logging
import os
import smtplib
import urllib.error
import urllib.request
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from typing import Iterable

import httpx

from . import crypto, db


logger = logging.getLogger(__name__)

SETTINGS_KEY = "mail"


def _safe_int(v, default: int, lo: int, hi: int) -> int:
    """v0.17.2: Admin form fields land here as strings. `int("abc")`
    would 500 the /settings/mail save. Clamp instead."""
    try:
        i = int(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, i))


class MailSendError(Exception):
    """Raised on any provider-side failure — caller logs + records."""


# ---------------------------------------------------------------------------
# Config persistence
# ---------------------------------------------------------------------------

def load_config() -> dict:
    """Return the current mail config, or a disabled-stub if nothing is
    saved yet. Encrypted fields are DECRYPTED here — never persist the
    return value back to the DB unchanged; it holds plaintext secrets.
    """
    from sqlalchemy import select
    with db.get_conn() as conn:
        row = conn.execute(
            select(db.settings.c.value_json)
            .where(db.settings.c.key == SETTINGS_KEY)
        ).first()
    raw = json.loads(row[0]) if row else {}
    # v0.17.1: guard both decrypts. Rotated Fernet key / restored-from-
    # backup instance shouldn't take down the mail runner every 15 min.
    # Silent fallback to empty means the send will fail cleanly (no
    # provider creds) rather than raising CryptoError up the stack.
    if raw.get("resend_api_key_enc"):
        try:
            raw["resend_api_key"] = crypto.decrypt(raw["resend_api_key_enc"])
        except crypto.CryptoError:
            raw["resend_api_key"] = ""
    if raw.get("smtp_password_enc"):
        try:
            raw["smtp_password"] = crypto.decrypt(raw["smtp_password_enc"])
        except crypto.CryptoError:
            raw["smtp_password"] = ""
    return {
        "provider":       raw.get("provider", "disabled"),
        "from_email":     raw.get("from_email", ""),
        "from_name":      raw.get("from_name", "Printix TonerWatch"),
        "resend_api_key": raw.get("resend_api_key", ""),
        "smtp_host":      raw.get("smtp_host", ""),
        "smtp_port":      int(raw.get("smtp_port") or 587),
        "smtp_username":  raw.get("smtp_username", ""),
        "smtp_password":  raw.get("smtp_password", ""),
        "smtp_starttls":  bool(raw.get("smtp_starttls", True)),
        # v0.22.0 — Graph provider: reuse Entra SSO's tenant / client_id /
        # client_secret via entra_sso.load_config(); mailbox_upn is which
        # mailbox to send AS (any user in the tenant that the app has
        # Mail.Send.Shared or Mail.Send application permission on).
        "graph_mailbox_upn": raw.get("graph_mailbox_upn", ""),
        # Marker so the settings page knows secrets ARE stored without
        # exposing them in the form value
        "resend_api_key_present": bool(raw.get("resend_api_key")),
        "smtp_password_present":  bool(raw.get("smtp_password")),
    }


def save_config(cfg: dict) -> None:
    """Persist mail config. Encrypts secret fields at rest via Fernet."""
    from sqlalchemy import func, insert, update
    payload: dict = {
        "provider":       (cfg.get("provider") or "disabled"),
        "from_email":     (cfg.get("from_email") or "").strip(),
        "from_name":      (cfg.get("from_name") or "").strip() or "Printix TonerWatch",
        "smtp_host":      (cfg.get("smtp_host") or "").strip(),
        "smtp_port":      _safe_int(cfg.get("smtp_port"), 587, 1, 65535),
        "smtp_username":  (cfg.get("smtp_username") or "").strip(),
        "smtp_starttls":  bool(cfg.get("smtp_starttls", True)),
        "graph_mailbox_upn": (cfg.get("graph_mailbox_upn") or "").strip().lower(),
    }
    # Only re-encrypt when a fresh secret was submitted; empty means
    # "keep the currently stored one" (fetch and re-encrypt).
    existing = load_config()

    resend_key = cfg.get("resend_api_key") or ""
    if resend_key:
        payload["resend_api_key_enc"] = crypto.encrypt(resend_key)
    elif existing.get("resend_api_key"):
        payload["resend_api_key_enc"] = crypto.encrypt(existing["resend_api_key"])

    smtp_pw = cfg.get("smtp_password") or ""
    if smtp_pw:
        payload["smtp_password_enc"] = crypto.encrypt(smtp_pw)
    elif existing.get("smtp_password"):
        payload["smtp_password_enc"] = crypto.encrypt(existing["smtp_password"])

    value_json = json.dumps(payload, ensure_ascii=False)
    with db.get_conn() as conn:
        row = conn.execute(
            db.settings.select().where(db.settings.c.key == SETTINGS_KEY)
        ).first()
        if row is None:
            conn.execute(insert(db.settings).values(
                key=SETTINGS_KEY, value_json=value_json))
        else:
            conn.execute(update(db.settings)
                         .where(db.settings.c.key == SETTINGS_KEY)
                         .values(value_json=value_json,
                                 updated_at=func.current_timestamp()))


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------

def send(recipients: Iterable[str], subject: str,
         html_body: str, text_body: str = "",
         config: dict | None = None) -> str:
    """Send an email. Returns the provider message-id (or empty string)."""
    recipients = [r.strip() for r in recipients if (r or "").strip()]
    if not recipients:
        raise MailSendError("no recipients")
    cfg = config or load_config()
    provider = cfg["provider"]

    if provider == "disabled":
        raise MailSendError("mail is disabled — configure a provider in Settings")
    if not cfg["from_email"] or "@" not in cfg["from_email"]:
        raise MailSendError("invalid or missing sender address")

    if provider == "resend":
        return _send_resend(recipients, subject, html_body, text_body, cfg)
    if provider == "smtp":
        return _send_smtp(recipients, subject, html_body, text_body, cfg)
    if provider == "graph":
        return _send_graph(recipients, subject, html_body, text_body, cfg)
    raise MailSendError(f"unknown provider: {provider}")


def _send_resend(recipients: list[str], subject: str,
                 html_body: str, text_body: str, cfg: dict) -> str:
    api_key = cfg.get("resend_api_key") or ""
    if not api_key:
        raise MailSendError("Resend API key not configured")
    from_header = formataddr((cfg["from_name"], cfg["from_email"]))
    payload = {
        "from":    from_header,
        "to":      recipients,
        "subject": subject,
        "html":    html_body,
    }
    if text_body:
        payload["text"] = text_body

    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type":  "application/json",
            "Accept":        "application/json",
            "User-Agent":    f"printix-tonerwatch/{os.environ.get('APP_VERSION', 'dev')}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            try:
                data = json.loads(body)
            except Exception:
                data = {}
            msg_id = data.get("id", "")
            logger.info("mail: resend OK to %d recipient(s), id=%s",
                        len(recipients), msg_id or "?")
            return msg_id
    except urllib.error.HTTPError as he:
        try:
            err = he.read().decode("utf-8", "replace")[:400]
        except Exception:
            err = ""
        raise MailSendError(f"Resend HTTP {he.code}: {err}") from he
    except urllib.error.URLError as ue:
        raise MailSendError(f"network error: {ue.reason}") from ue


def _send_smtp(recipients: list[str], subject: str,
               html_body: str, text_body: str, cfg: dict) -> str:
    host = cfg["smtp_host"]
    port = cfg["smtp_port"]
    user = cfg["smtp_username"]
    pwd  = cfg["smtp_password"]
    if not host:
        raise MailSendError("SMTP host not configured")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = formataddr((cfg["from_name"], cfg["from_email"]))
    msg["To"]      = ", ".join(recipients)
    if text_body:
        msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        with smtplib.SMTP(host, port, timeout=15) as s:
            if cfg["smtp_starttls"]:
                s.starttls()
            if user:
                s.login(user, pwd)
            s.sendmail(cfg["from_email"], recipients, msg.as_string())
        logger.info("mail: smtp OK to %d recipient(s)", len(recipients))
        return "smtp"   # SMTP doesn't return an ID
    except smtplib.SMTPException as e:
        raise MailSendError(f"SMTP error: {e}") from e
    except OSError as e:
        raise MailSendError(f"network error: {e}") from e


# ---------------------------------------------------------------------------
# Microsoft Graph — v0.22.0
# ---------------------------------------------------------------------------

def _send_graph(recipients: list[str], subject: str,
                html_body: str, text_body: str,
                cfg: dict) -> str:
    """Send via Microsoft Graph /users/{upn}/sendMail using an
    application-permission access token minted from the Entra SSO
    client_id + client_secret. Reuses the existing Entra registration
    — no separate app needed — as long as the admin adds ``Mail.Send``
    (application) permission + admin consent.

    Uses ``graph_mailbox_upn`` from the mail config as the sender
    mailbox; recipients from the caller. Silent no-op recipient
    normalisation (trim, drop empties)."""
    from . import entra_sso as _sso
    entra = _sso.load_config()
    tenant_id     = (entra.get("tenant_id") or "").strip()
    client_id     = (entra.get("client_id") or "").strip()
    client_secret = (entra.get("client_secret") or "").strip()
    mailbox_upn   = (cfg.get("graph_mailbox_upn") or cfg.get("from_email") or "").strip()
    if not (tenant_id and client_id and client_secret):
        raise MailSendError(
            "Graph mail requires Entra SSO to be configured "
            "(tenant_id, client_id, client_secret).")
    if not mailbox_upn:
        raise MailSendError("Graph mail requires a sender mailbox UPN "
                             "(set 'graph_mailbox_upn' or from_email).")
    to_recipients = [{"emailAddress": {"address": r.strip()}}
                      for r in recipients if r and r.strip()]
    if not to_recipients:
        raise MailSendError("no recipients")

    # 1. Client-credentials access token
    try:
        r = httpx.post(
            f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token",
            data={
                "client_id":     client_id,
                "client_secret": client_secret,
                "scope":         "https://graph.microsoft.com/.default",
                "grant_type":    "client_credentials",
            }, timeout=15.0)
    except httpx.HTTPError as e:
        raise MailSendError(f"Graph token endpoint: {e}") from e
    if r.status_code >= 400:
        # v0.23.3 — the four "silent misconfig" token errors get a
        # tailored hint. Anything else falls back to the raw error text.
        raw = r.text or ""
        hint = ""
        if "AADSTS7000215" in raw:
            hint = (" — the stored client_secret is wrong. Two common "
                    "causes: (a) you pasted the Secret ID instead of "
                    "the Secret VALUE (Azure shows both on the "
                    "Certificates & secrets page — the Value is the "
                    "long string, only visible ONCE right after "
                    "creation); (b) the Auto-Setup-generated secret "
                    "expired. Fix: click 'Reconfigure' on Settings → "
                    "Entra ID to mint a fresh secret via Auto-Setup.")
        elif "AADSTS700016" in raw:
            hint = (" — the client_id doesn't exist in this tenant. "
                    "Check tenant_id + client_id under Settings → "
                    "Entra ID → Diagnose.")
        elif "AADSTS90002" in raw:
            hint = (" — tenant not found. Check tenant_id.")
        elif "AADSTS7000222" in raw:
            hint = (" — the client secret expired. Rotate it in Azure "
                    "Portal → Certificates & secrets, or click "
                    "'Reconfigure' on Settings → Entra ID.")
        raise MailSendError(
            f"Graph token HTTP {r.status_code}: {raw[:300]}{hint}")
    token = r.json().get("access_token", "")
    if not token:
        raise MailSendError("Graph token response had no access_token")

    # 2. sendMail as the configured mailbox. Content-type=HTML always;
    # Graph handles the multipart wrapping.
    body = {
        "message": {
            "subject": subject,
            "body": {"contentType": "HTML", "content": html_body},
            "toRecipients": to_recipients,
            "from": {"emailAddress": {
                "address": cfg.get("from_email") or mailbox_upn,
                "name":    cfg.get("from_name") or "Printix TonerWatch"}},
        },
        "saveToSentItems": True,
    }
    from urllib.parse import quote as _q
    url = f"https://graph.microsoft.com/v1.0/users/{_q(mailbox_upn)}/sendMail"
    try:
        r = httpx.post(url, json=body,
                        headers={"Authorization": f"Bearer {token}",
                                 "Content-Type":  "application/json"},
                        timeout=20.0)
    except httpx.HTTPError as e:
        raise MailSendError(f"Graph sendMail: {e}") from e
    if r.status_code not in (200, 202):
        # v0.23.1 — the two "silent misconfig" Graph errors get a
        # tailored hint so the admin knows exactly which portal step is
        # missing. Anything else falls back to the raw error text.
        raw = r.text or ""
        hint = ""
        if r.status_code == 403 and "ErrorAccessDenied" in raw:
            hint = (" — the Entra app registration is missing the "
                    "**Mail.Send** application permission (Microsoft "
                    "Graph, Application, not Delegated) or it hasn't "
                    "been admin-consented. Add + consent it in Azure "
                    "Portal → Microsoft Entra ID → App registrations "
                    "→ your app → API permissions.")
        elif r.status_code == 404 and "MailboxNotEnabledForRESTAPI" in raw:
            hint = (f" — the mailbox {mailbox_upn!r} isn't licensed "
                    "for Exchange Online or is on a plan without "
                    "REST API access.")
        elif r.status_code == 404:
            hint = (f" — mailbox {mailbox_upn!r} not found in this "
                    "tenant. Click 'list mailboxes' to see valid UPNs.")
        elif r.status_code == 401:
            hint = " — token was rejected; check client_secret hasn't expired."
        raise MailSendError(
            f"Graph sendMail HTTP {r.status_code}: {raw[:300]}{hint}")
    logger.info("mail: graph OK to %d recipient(s) via %s",
                 len(to_recipients), mailbox_upn)
    # sendMail returns 202 Accepted with no body / no message-id.
    return f"graph:{mailbox_upn}"


def graph_auth_probe() -> dict:
    """v0.23.3 — do JUST the client-credentials token step, no mail.
    Returns::

        {"ok": True,  "token_len": 1234, "expires_in": 3599}
        {"ok": False, "status": 401, "error": "AADSTS7000215: …", "hint": "…"}

    Lets the Settings page verify tenant_id + client_id +
    client_secret without needing Mail.Send permission first."""
    from . import entra_sso as _sso
    entra = _sso.load_config()
    tenant_id     = (entra.get("tenant_id") or "").strip()
    client_id     = (entra.get("client_id") or "").strip()
    client_secret = (entra.get("client_secret") or "").strip()
    if not (tenant_id and client_id and client_secret):
        return {"ok": False, "status": 0,
                "error": "Entra ID isn't configured yet — set it up "
                          "under Settings → Entra ID first.",
                "hint": "auto_setup_required"}
    try:
        r = httpx.post(
            f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token",
            data={"client_id": client_id, "client_secret": client_secret,
                  "scope": "https://graph.microsoft.com/.default",
                  "grant_type": "client_credentials"}, timeout=15.0)
    except httpx.HTTPError as e:
        return {"ok": False, "status": 0,
                "error": f"{type(e).__name__}: {e}",
                "hint": "network"}
    if r.status_code >= 400:
        raw = r.text or ""
        # Parse-friendly hint the JS can key off to render an actionable
        # button ("Open Reconfigure" vs "Open Diagnose").
        hint = "unknown"
        if "AADSTS7000215" in raw or "AADSTS7000222" in raw:
            hint = "secret_wrong_or_expired"
        elif "AADSTS700016" in raw:
            hint = "client_id_wrong"
        elif "AADSTS90002" in raw:
            hint = "tenant_wrong"
        return {"ok": False, "status": r.status_code,
                "error": raw[:400], "hint": hint}
    token = r.json().get("access_token", "")
    if not token:
        return {"ok": False, "status": r.status_code,
                "error": "response had no access_token", "hint": "unknown"}
    return {"ok": True, "token_len": len(token),
            "expires_in": r.json().get("expires_in", 0)}


def list_graph_mailboxes(cfg: dict | None = None) -> list[dict]:
    """v0.22.0 — list Users the Entra app registration can see. Used
    by the Settings UI to populate the sender-mailbox dropdown so the
    admin picks from a real list instead of typing a UPN by hand.

    Requires ``User.Read.All`` (application) permission on the Entra
    app; if it's not granted, returns an empty list rather than
    surfacing the raw Graph error."""
    from . import entra_sso as _sso
    entra = _sso.load_config()
    tenant_id     = (entra.get("tenant_id") or "").strip()
    client_id     = (entra.get("client_id") or "").strip()
    client_secret = (entra.get("client_secret") or "").strip()
    if not (tenant_id and client_id and client_secret):
        return []
    try:
        r = httpx.post(
            f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token",
            data={"client_id": client_id, "client_secret": client_secret,
                  "scope": "https://graph.microsoft.com/.default",
                  "grant_type": "client_credentials"}, timeout=15.0)
        if r.status_code >= 400:
            return []
        token = r.json().get("access_token", "")
        if not token:
            return []
        r = httpx.get(
            "https://graph.microsoft.com/v1.0/users"
            "?$select=id,displayName,userPrincipalName,mail"
            "&$top=999",
            headers={"Authorization": f"Bearer {token}"}, timeout=20.0)
        if r.status_code >= 400:
            return []
        data = r.json().get("value", [])
        return [{"upn": (u.get("userPrincipalName") or "").lower(),
                  "display_name": u.get("displayName") or "",
                  "mail": u.get("mail") or ""}
                for u in data if u.get("userPrincipalName")]
    except httpx.HTTPError:
        return []
