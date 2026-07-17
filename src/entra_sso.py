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

# Microsoft's standard authority + scopes.
# ``User.Read`` unlocks Graph /v1.0/me — we fetch the profile from
# there rather than trusting the ID-token claims, because different
# Entra tenant configurations emit different subsets of claims
# (v4.x of mysecureprint learned this the hard way — `oid` and
# `email` are frequently absent depending on tenant policy).
# ``offline_access`` also gets a refresh_token so future syncs can
# renew silently without prompting the user again.
_AUTHORITY_TMPL = "https://login.microsoftonline.com/{tenant}"
_SCOPES: list[str] = ["User.Read"]
# NOTE: MSAL adds `openid`, `profile`, and `offline_access` itself and
# actively rejects any request that lists them explicitly (raises
# ValueError "You cannot use any scope value that is reserved"). This
# was in the initial code and only surfaced in v0.18.6 once we started
# showing every SSO exception as a readable error instead of a 500.

# Fallback authority for multi-tenant apps that accept sign-ins
# from any Entra tenant. Used when the operator leaves tenant_id
# empty — Microsoft handles home-tenant discovery on their side.
_MULTI_TENANT_AUTHORITY = "https://login.microsoftonline.com/common"

# ─── Auto-setup constants (v0.16) ────────────────────────────────
# Device-code + Graph endpoints for the "one-click Azure App
# Registration" flow. The admin never touches Azure Portal — they
# authenticate once at microsoft.com/devicelogin, and TonerWatch
# creates the App Registration + secret + admin consent on their
# behalf via Graph API.
_DEVICE_CODE_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/devicecode"
_TOKEN_URL       = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
_GRAPH_URL       = "https://graph.microsoft.com/v1.0"

# First-party Microsoft app for Graph CLI — supports device-code
# flow + dynamic consent for Graph API permissions. Same one the
# `mgc` CLI and Microsoft.Graph PowerShell SDK use.
_GRAPH_CLI_CLIENT_ID = "14d82eec-204b-4c2f-b7e8-296a70dab67e"

# Microsoft Graph's own AppId — used to look up its ServicePrincipal
# when we grant tenant-wide consent for delegated scopes.
_MSGRAPH_APP_ID = "00000003-0000-0000-c000-000000000000"

# Scopes we need for the device-code login that then performs
# auto-registration. All Application.ReadWrite.All is enough for
# create/update apps + secrets. DelegatedPermissionGrant.ReadWrite.All
# lets us grant tenant-wide consent so end-users skip the consent
# screen on first sign-in.
_AUTOSETUP_SCOPES = (
    "https://graph.microsoft.com/Application.ReadWrite.All "
    "https://graph.microsoft.com/DelegatedPermissionGrant.ReadWrite.All "
    "offline_access openid profile"
)

# Delegated permission IDs on Microsoft Graph.
# Docs: https://learn.microsoft.com/en-us/graph/permissions-reference
_GRAPH_SCOPE_OPENID   = "37f7f235-527c-4136-accd-4a02d197296e"
_GRAPH_SCOPE_PROFILE  = "14dad69e-099b-42c9-810b-d002981feec1"
_GRAPH_SCOPE_EMAIL    = "64a6cdd6-aab1-4aaf-94b8-3cc8405e90d0"
_GRAPH_SCOPE_USERREAD = "e1fe6dd8-ba31-4d61-89e7-88639da4683d"

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
        # v0.24.8 — Graph's endDateTime for the current secret, so the
        # settings page can warn before it expires instead of Marcus
        # finding out via a broken login.
        "secret_expires_at": raw.get("secret_expires_at", ""),
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
        # A fresh secret value with no accompanying expiry means the
        # caller doesn't know it (e.g. hand-typed in the form) —
        # clear the stale one rather than keep showing an expiry that
        # no longer matches the stored secret.
        payload["secret_expires_at"] = cfg.get("secret_expires_at", "")
    else:
        existing = load_config()
        if existing.get("client_secret"):
            payload["client_secret_enc"] = crypto.encrypt(existing["client_secret"])
            payload["secret_expires_at"] = existing.get("secret_expires_at", "")

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


def secret_expiry_status(warn_within_days: int = 30) -> dict[str, Any]:
    """v0.24.8 — how much runway is left on the stored client_secret,
    for a warning banner on the settings page. ``known`` is False for
    secrets minted before this field existed, or hand-entered ones —
    there's nothing to warn about because there's nothing to compare."""
    cfg = load_config()
    raw = (cfg.get("secret_expires_at") or "").strip()
    if not raw or not cfg.get("client_secret"):
        return {"known": False, "expires_at": "", "days_left": None, "warn": False}
    import datetime as _dt
    try:
        expires = _dt.datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=_dt.timezone.utc)
    except ValueError:
        return {"known": False, "expires_at": raw, "days_left": None, "warn": False}
    days_left = (expires - _dt.datetime.now(_dt.timezone.utc)).days
    return {"known": True, "expires_at": raw, "days_left": days_left,
            "warn": days_left <= warn_within_days}


# ---------------------------------------------------------------------------
# MSAL client + URL builders
# ---------------------------------------------------------------------------

def _msal_app(cfg: dict[str, Any]):
    """Lazy MSAL import — the package is ~4 MB and never needed if
    SSO isn't configured. Uses `common` when tenant_id is empty so
    an admin can enable multi-tenant sign-in without picking a
    specific home tenant.

    v0.18.5 — wrap ImportError + ConfidentialClientApplication
    construction so callers can catch a single EntraSSOError instead
    of chasing MSAL-side exceptions."""
    try:
        import msal
    except ImportError as e:
        raise EntraSSOError(
            "MSAL package not installed — the container image is missing "
            "the `msal` Python package. Rebuild the image or `pip install "
            "msal` in the environment.") from e
    tenant = (cfg.get("tenant_id") or "").strip()
    authority = (_AUTHORITY_TMPL.format(tenant=tenant)
                 if tenant else _MULTI_TENANT_AUTHORITY)
    try:
        return msal.ConfidentialClientApplication(
            client_id=cfg["client_id"],
            client_credential=cfg["client_secret"],
            authority=authority,
        )
    except EntraSSOError:
        raise
    except Exception as e:  # noqa: BLE001
        # MSAL raises ValueError on bad tenant IDs, but the constructor
        # ALSO fetches the tenant's openid metadata over HTTPS — network
        # errors, TLS handshake failures, "tenant does not exist" (404
        # from AAD) all surface here. One catch, one clear message.
        raise EntraSSOError(
            f"MSAL rejected the config or couldn't reach Microsoft — "
            f"tenant_id={tenant!r}, {type(e).__name__}: {str(e)[:200]}"
        ) from e


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


def _aadsts_hint(error_text: str) -> str:
    """v0.24.2 — same actionable-hint mapping mail_client.py already
    applies to Graph token failures, now also applied to the SSO
    LOGIN callback. A broken client_secret blocks both mail-sending
    AND signing in — the login error page should point at the same
    fix (Settings → Entra ID → Reconfigure → 🔑 Rotate secret only)
    instead of leaving the operator to piece it together from a raw
    AADSTS code."""
    if "AADSTS7000215" in error_text or "AADSTS7000222" in error_text:
        return (" — the stored client_secret is wrong or expired. Fix: "
                "Settings → Entra ID → Reconfigure → 🔑 Rotate secret only "
                "(keeps the app, mints a fresh secret — no Azure Portal "
                "needed). Local email+password login still works in the "
                "meantime.")
    if "AADSTS700016" in error_text:
        return " — client_id not found in this tenant. Check Settings → Entra ID → Diagnose."
    if "AADSTS90002" in error_text:
        return " — tenant_id doesn't exist. Check Settings → Entra ID → Diagnose."
    if "AADSTS50011" in error_text:
        return (" — the redirect_uri doesn't match what's registered on "
                "the Azure app. Check Settings → Entra ID → Diagnose for "
                "the exact URI this server sends.")
    return ""


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
    # v0.17.1: constant-time compare (theoretical timing attack; state
    # is 24 random bytes so real exploit is impractical, but hmac
    # keeps the code style consistent with token compares elsewhere).
    import hmac as _hmac
    if not expected_state or not _hmac.compare_digest(
            str(expected_state), str(state)):
        raise EntraSSOError("state mismatch — possible CSRF")

    redirect_uri = cfg["redirect_uri"] or _default_redirect(request)
    app = _msal_app(cfg)
    result = app.acquire_token_by_authorization_code(
        code=code, scopes=_SCOPES, redirect_uri=redirect_uri)
    if "error" in result:
        # v0.24.2: keep desc short enough that desc + the AADSTS hint
        # both survive the [:500] truncation the /login redirect
        # applies — a long raw AADSTS message used to push the
        # actually-useful hint text off the end.
        desc = result.get("error_description", "")[:180]
        raise EntraSSOError(
            f"{result.get('error')}: {desc}{_aadsts_hint(desc)}")

    # v0.15: fetch the profile from Graph /me rather than trusting
    # id_token_claims. Different tenants emit different claim
    # subsets (some suppress `oid`, some emit `email` only for
    # accounts with a validated email, etc.). Graph /me is
    # consistent — id, mail (or userPrincipalName as fallback),
    # displayName. If Graph fails, fall back to whatever claims
    # ARE present so a barebones tenant can still sign in.
    access_token = result.get("access_token") or ""
    email = ""
    oid = ""
    name = ""
    if access_token:
        try:
            import httpx as _httpx
            r = _httpx.get(
                "https://graph.microsoft.com/v1.0/me",
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=10.0)
            if r.status_code == 200:
                me = r.json()
                oid = (me.get("id") or "").strip()
                email = (me.get("mail")
                         or me.get("userPrincipalName") or "").strip().lower()
                name = (me.get("displayName") or "").strip()
        except Exception as e:  # noqa: BLE001
            logger.info("Graph /me fetch failed, falling back to claims: %s", e)

    if not (email and oid):
        # Fallback: parse claims (old behaviour) — better than nothing
        id_claims = result.get("id_token_claims") or {}
        email = email or (id_claims.get("email")
                          or id_claims.get("preferred_username") or "").strip().lower()
        oid = oid or (id_claims.get("oid") or id_claims.get("sub") or "").strip()
        name = name or (id_claims.get("name") or "").strip()

    if not email or not oid:
        raise EntraSSOError(
            "SSO succeeded but Microsoft returned no email/oid — "
            "check the App Registration has 'User.Read' permission "
            "granted (admin consent may be required).")

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


# ---------------------------------------------------------------------------
# Auto-setup: device-code login → Graph API creates App Registration
# ---------------------------------------------------------------------------
#
# One-click Azure onboarding. The admin never opens the Azure Portal.
# They click "Auto-configure", get a short code, sign in once at
# microsoft.com/devicelogin, and TonerWatch does the rest via
# Microsoft Graph:
#
#   1. Fetch tenant_id via /organization
#   2. POST /applications — create the App Registration with the
#      correct redirect URI + single-tenant audience + openid /
#      profile / email / User.Read scopes
#   3. POST /applications/{obj_id}/addPassword — mint a fresh client
#      secret (Microsoft caps at 24 months)
#   4. Create the ServicePrincipal + oauth2PermissionGrant so end
#      users never see the consent screen
#   5. Save all of the above into the entra_sso settings row
#
# Required scopes on the admin's device-code login:
#   * Application.ReadWrite.All            (create app + secret)
#   * DelegatedPermissionGrant.ReadWrite.All (tenant-wide consent)
#
# The admin must have the "Application Administrator" or a higher
# role in Entra. Regular users can't grant tenant-wide consent.


def start_device_code_flow(tenant: str = "common") -> dict[str, Any]:
    """POST /devicecode. Returns a dict with user_code, verification_uri,
    device_code, expires_in, interval, plus an ``error`` key when
    Microsoft rejected the request (tenant blocks device flow,
    unknown tenant, etc.)."""
    import httpx as _httpx
    try:
        r = _httpx.post(
            _DEVICE_CODE_URL.format(tenant=tenant),
            data={"client_id": _GRAPH_CLI_CLIENT_ID,
                  "scope":     _AUTOSETUP_SCOPES},
            timeout=15.0)
    except _httpx.HTTPError as e:
        return {"error": f"network error: {e}"}
    if r.status_code != 200:
        try:
            payload = r.json()
            msg = (payload.get("error_description")
                   or payload.get("error") or f"HTTP {r.status_code}")
        except Exception:
            msg = f"HTTP {r.status_code}: {r.text[:200]}"
        return {"error": msg}
    d = r.json()
    return {
        "device_code":      d.get("device_code", ""),
        "user_code":        d.get("user_code", ""),
        "verification_uri": d.get("verification_uri", ""),
        "expires_in":       int(d.get("expires_in", 900)),
        "interval":         int(d.get("interval", 5)),
        "message":          d.get("message", ""),
    }


def poll_device_code_token(device_code: str,
                            tenant: str = "common") -> dict[str, Any]:
    """One poll cycle. Returns status ∈ {pending, success, expired, error}."""
    import httpx as _httpx
    try:
        r = _httpx.post(
            _TOKEN_URL.format(tenant=tenant),
            data={"client_id":  _GRAPH_CLI_CLIENT_ID,
                  "device_code": device_code,
                  "grant_type": "urn:ietf:params:oauth:grant-type:device_code"},
            timeout=15.0)
    except _httpx.HTTPError as e:
        return {"status": "error", "error": str(e)}
    data = r.json() if r.text else {}
    if r.status_code == 200:
        token = data.get("access_token", "")
        if token:
            return {"status": "success", "access_token": token}
        return {"status": "error", "error": "no access_token in response"}
    err = data.get("error", "")
    if err in ("authorization_pending", "slow_down"):
        return {"status": "pending"}
    if err == "expired_token":
        return {"status": "expired"}
    return {"status": "error",
            "error": data.get("error_description", err or "unknown")}


def _graph(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}",
            "Content-Type":  "application/json"}


def find_existing_apps(access_token: str, app_name: str) -> list[dict[str, Any]]:
    """v0.17.2: check whether an App Registration with our display
    name already exists in the target tenant. Every Auto-Setup click
    would otherwise create a new App + a new secret; over time an
    admin who plays with the button leaves a trail of orphans.

    Returns a (possibly empty) list of dicts with id / appId /
    displayName / createdDateTime. The caller decides whether to
    warn the admin, offer reuse, or proceed with a fresh app.
    """
    import httpx as _httpx
    from urllib.parse import quote
    h = _graph(access_token)
    # $filter is case-sensitive on displayName. escaped for OData:
    #   displayName eq 'Printix TonerWatch'
    escaped = app_name.replace("'", "''")
    url = (f"{_GRAPH_URL}/applications"
           f"?$filter=displayName eq '{quote(escaped)}'"
           f"&$select=id,appId,displayName,createdDateTime,web"
           f"&$top=25")
    try:
        r = _httpx.get(url, headers=h, timeout=10.0)
    except _httpx.HTTPError:
        return []
    if r.status_code != 200:
        return []
    return list(r.json().get("value", []))


def _add_password_best_effort(h: dict, object_id: str,
                               display_name: str) -> dict[str, Any]:
    """v0.24.8 — mint a client secret with the longest lifetime the
    tenant will allow, instead of leaving ``endDateTime`` unspecified.

    An unspecified ``endDateTime`` gets whatever the tenant's default
    is — some tenants apply a short (as low as 6 month) default via
    "Application authentication methods" governance policy, which is
    almost certainly why AADSTS7000215 ("invalid client secret") kept
    recurring: the secret wasn't wrong, it had quietly expired.
    Requesting 24 months explicitly gets the longest lifetime on
    tenants without such a policy; on tenants that enforce a shorter
    cap, Graph rejects the out-of-policy request outright (it does
    NOT silently clamp), so this retries once at 6 months — the most
    common enforced ceiling — and finally falls back to the
    unspecified default so secret creation never fails outright just
    because we asked for too long a lifetime."""
    import datetime as _dt
    import httpx as _httpx

    def _mint(end_date_time: str | None) -> "_httpx.Response":
        cred: dict[str, Any] = {"displayName": display_name}
        if end_date_time:
            cred["endDateTime"] = end_date_time
        return _httpx.post(
            f"{_GRAPH_URL}/applications/{object_id}/addPassword",
            headers=h, json={"passwordCredential": cred}, timeout=15.0)

    now = _dt.datetime.now(_dt.timezone.utc)
    attempts = [
        (now + _dt.timedelta(days=730)).strftime("%Y-%m-%dT%H:%M:%SZ"),  # 24mo
        (now + _dt.timedelta(days=183)).strftime("%Y-%m-%dT%H:%M:%SZ"),  # 6mo
        None,  # tenant default — always succeeds if the app itself is valid
    ]
    last_error = ""
    for end_date_time in attempts:
        try:
            r = _mint(end_date_time)
        except _httpx.HTTPError as e:
            raise EntraSSOError(f"Graph addPassword: {e}") from e
        if r.status_code in (200, 201):
            return r.json()
        last_error = f"HTTP {r.status_code}: {r.text[:200]}"
    raise EntraSSOError(f"Secret creation failed: {last_error}")


def rotate_client_secret(access_token: str, object_id: str,
                          app_name: str = "TonerWatch (rotated)"
                          ) -> dict[str, Any]:
    """v0.23.4 — mint a fresh client_secret on an EXISTING App
    registration. Doesn't touch redirect URIs, consent, or
    permissions — the whole point is that the app's identity + its
    granted permissions carry over, only the secret changes. Used by
    the "🔑 Rotate secret only" button when Auto-Setup's
    existing_found panel fires."""
    h = _graph(access_token)
    try:
        sec = _add_password_best_effort(h, object_id, app_name)
    except EntraSSOError as e:
        raise EntraSSOError(f"Secret rotation failed: {e}") from e
    return {
        "client_secret":     sec.get("secretText", ""),
        "secret_expires_at": sec.get("endDateTime", ""),
        "object_id":         object_id,
    }


def auto_register_app(
    access_token: str, redirect_uri: str,
    app_name: str = "Printix TonerWatch",
) -> dict[str, Any]:
    """Create App Registration + secret + tenant-wide consent.

    Returns a dict with tenant_id, client_id, client_secret,
    secret_expires_at, admin_consent status. Raises EntraSSOError
    on any Graph-side failure worth surfacing to the admin.
    """
    import httpx as _httpx
    h = _graph(access_token)

    # 1. Discover tenant_id
    tenant_id = ""
    try:
        r = _httpx.get(f"{_GRAPH_URL}/organization", headers=h, timeout=10.0)
        if r.status_code == 200:
            orgs = r.json().get("value", [])
            if orgs:
                tenant_id = orgs[0].get("id", "")
    except _httpx.HTTPError as e:
        logger.warning("could not resolve tenant_id: %s", e)

    # 2. Create App Registration.
    # signInAudience=AzureADMyOrg → single-tenant (safest default).
    # The four OIDC scopes cover everything the sign-in flow needs.
    app_body = {
        "displayName": app_name,
        "signInAudience": "AzureADMyOrg",
        "web": {
            "redirectUris": [redirect_uri],
            "implicitGrantSettings": {"enableIdTokenIssuance": True},
        },
        "requiredResourceAccess": [{
            "resourceAppId": _MSGRAPH_APP_ID,
            "resourceAccess": [
                {"id": _GRAPH_SCOPE_OPENID,   "type": "Scope"},
                {"id": _GRAPH_SCOPE_PROFILE,  "type": "Scope"},
                {"id": _GRAPH_SCOPE_EMAIL,    "type": "Scope"},
                {"id": _GRAPH_SCOPE_USERREAD, "type": "Scope"},
            ],
        }],
    }
    try:
        r = _httpx.post(f"{_GRAPH_URL}/applications",
                        headers=h, json=app_body, timeout=20.0)
    except _httpx.HTTPError as e:
        raise EntraSSOError(f"Graph /applications: {e}") from e
    if r.status_code not in (200, 201):
        raise EntraSSOError(
            f"App creation failed: HTTP {r.status_code}: {r.text[:300]}")
    app = r.json()
    client_id = app["appId"]
    obj_id = app["id"]

    # 3. Create client secret — longest lifetime the tenant allows
    # (see _add_password_best_effort), not whatever the platform
    # default happens to be.
    sec = _add_password_best_effort(h, obj_id, "TonerWatch auto-generated")
    client_secret = sec.get("secretText", "")
    secret_expires_at = sec.get("endDateTime", "")

    # 4. Tenant-wide consent → users don't see the consent screen.
    consent_status = _grant_tenant_consent(h, client_id)

    logger.info(
        "Entra SSO app auto-registered: %s (client_id=%s, tenant=%s, "
        "secret_expires=%s, admin_consent=%s)",
        app_name, client_id, tenant_id, secret_expires_at, consent_status)

    return {
        "tenant_id":         tenant_id,
        "client_id":         client_id,
        "client_secret":     client_secret,
        "secret_expires_at": secret_expires_at,
        "object_id":         obj_id,
        "admin_consent":     consent_status,
    }


def _grant_tenant_consent(headers: dict[str, str], client_id: str,
                          scopes: str = "openid profile email User.Read") -> str:
    """Create ServicePrincipal for our new app + oauth2PermissionGrant
    for the four Graph scopes on behalf of the whole tenant.

    Returns "granted" | "sp_failed" | "no_msgraph_sp" | "grant_failed"
    | "partial". Never raises — the SSO app is usable without tenant
    consent (users just click through on first login), so we report
    the outcome for the UI rather than failing the whole setup.
    """
    import httpx as _httpx
    # 1. Create ServicePrincipal for the new app
    try:
        r = _httpx.post(f"{_GRAPH_URL}/servicePrincipals",
                        headers=headers, json={"appId": client_id},
                        timeout=15.0)
    except _httpx.HTTPError:
        return "sp_failed"
    if r.status_code not in (200, 201):
        logger.warning("SP creation failed: %s %s",
                       r.status_code, r.text[:200])
        return "sp_failed"
    our_sp_id = r.json().get("id", "")
    if not our_sp_id:
        return "sp_failed"

    # 2. Look up Microsoft Graph's own ServicePrincipal id (target
    # for the grant — Graph API is a resource that we're granting
    # scopes ON).
    try:
        r = _httpx.get(
            f"{_GRAPH_URL}/servicePrincipals(appId='{_MSGRAPH_APP_ID}')",
            headers=headers, timeout=10.0)
    except _httpx.HTTPError:
        return "no_msgraph_sp"
    if r.status_code != 200:
        return "no_msgraph_sp"
    msgraph_sp_id = r.json().get("id", "")

    # 3. Create the tenant-wide grant.
    try:
        r = _httpx.post(
            f"{_GRAPH_URL}/oauth2PermissionGrants",
            headers=headers,
            json={"clientId":    our_sp_id,
                  "consentType": "AllPrincipals",
                  "resourceId":  msgraph_sp_id,
                  "scope":       scopes},
            timeout=15.0)
    except _httpx.HTTPError:
        return "grant_failed"
    if r.status_code not in (200, 201):
        logger.warning("consent grant failed: %s %s",
                       r.status_code, r.text[:200])
        return "grant_failed"
    return "granted"


def apply_auto_setup_result(result: dict[str, Any], *,
                             redirect_uri: str) -> None:
    """Persist the auto-registration result into the entra_sso settings
    row so the SSO login flow can use it immediately. Enables SSO by
    default — the admin just clicked "Auto-configure", they clearly
    want it on."""
    save_config({
        "enabled":       True,
        "tenant_id":     result.get("tenant_id", ""),
        "client_id":     result.get("client_id", ""),
        "client_secret": result.get("client_secret", ""),
        "secret_expires_at": result.get("secret_expires_at", ""),
        "redirect_uri":  redirect_uri,
        # Auto-setup doesn't imply auto-provisioning — the admin has
        # to explicitly opt in to that (creates local users on first
        # SSO login, which some tenants deliberately don't want).
        "allow_auto_provision": False,
        "auto_provision_domain": "",
        "default_role":  "technician",
    })
