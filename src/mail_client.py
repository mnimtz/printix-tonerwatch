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

from . import crypto, db


logger = logging.getLogger(__name__)

SETTINGS_KEY = "mail"


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
    # Decrypt the two secret fields on read
    if raw.get("resend_api_key_enc"):
        raw["resend_api_key"] = crypto.decrypt(raw["resend_api_key_enc"])
    if raw.get("smtp_password_enc"):
        raw["smtp_password"] = crypto.decrypt(raw["smtp_password_enc"])
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
        "smtp_port":      int(cfg.get("smtp_port") or 587),
        "smtp_username":  (cfg.get("smtp_username") or "").strip(),
        "smtp_starttls":  bool(cfg.get("smtp_starttls", True)),
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
