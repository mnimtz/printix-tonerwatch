"""Microsoft Entra ID (Azure AD) single sign-on.

Flow (OAuth2 authorization code):

1. ``/auth/entra/login`` — build the authorize URL via MSAL and
   redirect the browser to Microsoft. A random `state` is stored
   in the session so we can verify the callback wasn't forged.
2. Microsoft calls back to ``/auth/entra/callback?code=…&state=…``.
   We hand the code to MSAL, receive the token bundle, extract the
   ID-token claims (email + `oid`), and match to a user row:
     - `entra_oid` match — the user has signed in via SSO before → log in
     - `email` match — first SSO login for an existing local user →
       link (`entra_oid` gets stored) and log in
     - neither — depending on the "allow auto-provisioning" flag, we
       either create a new `technician` user with no password (SSO
       only) or return a friendly "no local account" error

Config lives in the `settings` table under the ``entra_sso`` key,
client_secret Fernet-encrypted at rest.

The MSAL package is imported lazily inside functions so operators
who don't configure SSO never pay the parse-time penalty.
"""

from __future__ import annotations

import json
import logging
import secrets
from typing import Any

from sqlalchemy import func, insert, select, update

from . import crypto, db


logger = logging.getLogger(__name__)

SETTINGS_KEY = "entra_sso"

# Microsoft's standard authority + scopes for OIDC. We only need
# openid + email + profile — no access to Graph or other resources.
_AUTHORITY_TMPL = "https://login.microsoftonline.com/{tenant}"
_SCOPES: list[str] = ["User.Read"]   # implicit openid+profile+email

# Session key for the state token (guards against CSRF on the
# callback) and the redirect-target after successful login.
_SESSION_STATE_KEY = "entra_state"
_SESSION_NEXT_KEY = "entra_next"


class EntraSSOError(Exception):
    """Raised on config / callback errors — caller logs + shows message."""


# ---------------------------------------------------------------------------
# Config persistence
# ---------------------------------------------------------------------------

def load_config() -> dict[str, Any]:
    with db.get_conn() as conn:
        row = conn.execute(
            select(db.settings.c.value_json)
            .where(db.settings.c.key == SETTINGS_KEY)
        ).first()
    raw = json.loads(row[0]) if row else {}
    if raw.get("client_secret_enc"):
        try:
            raw["client_secret"] = crypto.decrypt(raw["client_secret_enc"])
        except crypto.CryptoError:
            raw["client_secret"] = ""
    return {
        "enabled":            bool(raw.get("enabled")),
        "tenant_id":          raw.get("tenant_id", ""),
        "client_id":          raw.get("client_id", ""),
        "client_secret":      raw.get("client_secret", ""),
        "redirect_uri":       raw.get("redirect_uri", ""),
        "allow_auto_provision": bool(raw.get("allow_auto_provision", False)),
        "auto_provision_domain": raw.get("auto_provision_domain", ""),
        "default_role":       raw.get("default_role", "technician"),
        # Present-flag for the UI so the secret field can show "stored"
        "client_secret_present": bool(raw.get("client_secret")),
    }


def save_config(cfg: dict[str, Any]) -> None:
    payload: dict[str, Any] = {
        "enabled":              bool(cfg.get("enabled")),
        "tenant_id":            (cfg.get("tenant_id") or "").strip(),
        "client_id":            (cfg.get("client_id") or "").strip(),
        "redirect_uri":         (cfg.get("redirect_uri") or "").strip(),
        "allow_auto_provision": bool(cfg.get("allow_auto_provision")),
        "auto_provision_domain": (cfg.get("auto_provision_domain") or "").strip().lower(),
        "default_role":         (cfg.get("default_role") or "technician").lower(),
    }
    if payload["default_role"] not in ("admin", "technician"):
        payload["default_role"] = "technician"

    secret = cfg.get("client_secret") or ""
    if secret:
        payload["client_secret_enc"] = crypto.encrypt(secret)
    else:
        existing = load_config()
        if existing.get("client_secret"):
            payload["client_secret_enc"] = crypto.encrypt(existing["client_secret"])

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


def is_configured() -> bool:
    """SSO is offered on /login only when it's actually usable."""
    cfg = load_config()
    return bool(cfg["enabled"] and cfg["tenant_id"]
                and cfg["client_id"] and cfg["client_secret"])


# ---------------------------------------------------------------------------
# MSAL client + URL builders
# ---------------------------------------------------------------------------

def _msal_app(cfg: dict[str, Any]):
    """Lazy MSAL import — the package is ~4 MB and never needed if
    SSO isn't configured."""
    import msal
    return msal.ConfidentialClientApplication(
        client_id=cfg["client_id"],
        client_credential=cfg["client_secret"],
        authority=_AUTHORITY_TMPL.format(tenant=cfg["tenant_id"]),
    )


def build_auth_url(request, next_url: str = "/") -> str:
    """Compose the /authorize URL + stash CSRF state + intended redirect."""
    cfg = load_config()
    if not is_configured():
        raise EntraSSOError("SSO not configured")

    state = secrets.token_urlsafe(24)
    try:
        request.session[_SESSION_STATE_KEY] = state
        request.session[_SESSION_NEXT_KEY] = next_url if next_url.startswith("/") else "/"
    except AssertionError:
        # Session middleware missing — shouldn't happen in practice
        raise EntraSSOError("session unavailable")

    redirect_uri = cfg["redirect_uri"] or _default_redirect(request)
    app = _msal_app(cfg)
    return app.get_authorization_request_url(
        scopes=_SCOPES, state=state, redirect_uri=redirect_uri)


def _default_redirect(request) -> str:
    """When redirect_uri isn't explicitly configured, build one from
    the incoming request. Matches what an admin would enter into the
    Azure App Registration."""
    base = str(request.base_url).rstrip("/")
    return f"{base}/auth/entra/callback"


def handle_callback(request, code: str, state: str) -> dict[str, Any]:
    """Exchange the code for a token bundle + return the user claims.

    Raises :class:`EntraSSOError` if state doesn't match, MSAL rejects
    the code, or the token doesn't carry the claims we need.
    """
    cfg = load_config()
    if not is_configured():
        raise EntraSSOError("SSO not configured")

    try:
        expected_state = request.session.pop(_SESSION_STATE_KEY, None)
    except AssertionError:
        expected_state = None
    if not expected_state or expected_state != state:
        raise EntraSSOError("state mismatch — possible CSRF")

    redirect_uri = cfg["redirect_uri"] or _default_redirect(request)
    app = _msal_app(cfg)
    result = app.acquire_token_by_authorization_code(
        code=code, scopes=_SCOPES, redirect_uri=redirect_uri)
    if "error" in result:
        raise EntraSSOError(
            f"{result.get('error')}: {result.get('error_description', '')[:200]}")

    id_claims = result.get("id_token_claims") or {}
    email = (id_claims.get("email")
             or id_claims.get("preferred_username") or "").strip().lower()
    oid = (id_claims.get("oid") or "").strip()
    name = (id_claims.get("name") or "").strip()
    if not email or not oid:
        raise EntraSSOError("token missing email/oid claim")

    return {"email": email, "oid": oid, "name": name}


# ---------------------------------------------------------------------------
# User provisioning
# ---------------------------------------------------------------------------

def resolve_or_create_user(claims: dict[str, Any]) -> dict[str, Any] | None:
    """Return the local user row for these SSO claims, creating one
    when auto-provisioning is enabled. Returns None when the operator
    disabled auto-provisioning and no matching user exists — the
    callback handler then shows a friendly "no local account" error.

    Provisioning rules:
      1. entra_oid match → user found → return (and update last_login)
      2. email match → link (set entra_oid on the row) + return
      3. no match + allow_auto_provision + domain matches (or empty)
         → create technician user, no password → return
      4. no match + auto-provisioning disabled → None
    """
    cfg = load_config()
    email = claims["email"]
    oid = claims["oid"]
    name = claims.get("name") or ""

    with db.get_conn() as conn:
        # 1. entra_oid match
        row = conn.execute(
            select(db.users).where(db.users.c.entra_oid == oid)
        ).first()
        if row is not None:
            conn.execute(
                update(db.users)
                .where(db.users.c.id == row.id)
                .values(last_login_at=func.current_timestamp())
            )
            return db._row_to_dict(row)

        # 2. email match → link
        row = conn.execute(
            select(db.users).where(func.lower(db.users.c.email) == email)
        ).first()
        if row is not None:
            conn.execute(
                update(db.users)
                .where(db.users.c.id == row.id)
                .values(entra_oid=oid,
                        last_login_at=func.current_timestamp())
            )
            fresh = conn.execute(
                select(db.users).where(db.users.c.id == row.id)
            ).first()
            return db._row_to_dict(fresh)

        # 3. auto-provisioning
        if not cfg.get("allow_auto_provision"):
            return None
        domain_filter = (cfg.get("auto_provision_domain") or "").lower()
        if domain_filter and not email.endswith("@" + domain_filter):
            return None
        role = cfg.get("default_role", "technician")
        if role not in ("admin", "technician"):
            role = "technician"
        # Empty password_hash — Local login blocked by design.
        result = conn.execute(insert(db.users).values(
            email=email, password_hash="", name=name or email.split("@")[0],
            role=role, entra_oid=oid, active=1,
            last_login_at=func.current_timestamp(),
        ))
        new_id = int(result.inserted_primary_key[0])
        row = conn.execute(
            select(db.users).where(db.users.c.id == new_id)
        ).first()
        return db._row_to_dict(row)
