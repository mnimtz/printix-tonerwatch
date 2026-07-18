"""Printix Partner API — reseller/MSP-level tenant management.

https://printix.bitbucket.io — the documented surface is small: an
OAuth2 client_credentials exchange, then four read/create endpoints
under a partner's own tenant list. No update/delete/cancel endpoint
is publicly documented — this client doesn't pretend one exists.

* ``auth.printix.net`` (or ``auth.testenv.printix.net`` for a test
  partner account) — ``POST /oauth/token`` with
  ``grant_type=client_credentials`` returns a ~10-minute access
  token. Cached in-memory per process; re-fetched on demand rather
  than using the refresh_token grant, since a fresh client_credentials
  exchange is just as cheap and needs no extra state.
* ``api.printix.net`` — list/create/get tenants + get billing info,
  all under ``/public/partners/{partner_id}/tenants``.

Config lives in the ``settings`` table under the ``printix_partner``
key, client_secret Fernet-encrypted at rest — same shape as
``entra_sso``/``llm_client``/``mail_client``.

v0.24.40: this integration was built before Marcus had partner API
credentials (access was requested from Printix, pending at the time
of writing) — every HTTP call here is verified against the
*documented* request/response shapes only, never against a live
tenant. ``enabled`` defaults to off specifically so it stays inert
until he can test it against the real API.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx
from sqlalchemy import func, insert, select, update

from . import crypto, db


logger = logging.getLogger(__name__)

SETTINGS_KEY = "printix_partner"

_AUTH_HOSTS = {
    "production": "https://auth.printix.net",
    "test":       "https://auth.testenv.printix.net",
}
_DEFAULT_API_HOST = "https://api.printix.net"

# Access tokens are short-lived (docs show expires_in: 599, ~10 min).
# Re-fetch a bit before actual expiry so a slow request never straddles
# the boundary. Keyed by (auth_host, client_id, partner_id) so a config
# change never serves a stale token from a previous account.
_TOKEN_EXPIRY_BUFFER_SECONDS = 30
_token_cache: dict[tuple[str, str, str], tuple[str, float]] = {}


class PrintixPartnerError(Exception):
    """Raised on any request failure — caller shows the message as-is,
    this is written to already be readable (see _readable_auth_error /
    _readable_api_error)."""


# ---------------------------------------------------------------------------
# Config
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
    environment = raw.get("environment") or "production"
    if environment not in _AUTH_HOSTS:
        environment = "production"
    return {
        "enabled":            bool(raw.get("enabled", False)),
        "environment":        environment,
        "partner_id":         raw.get("partner_id", ""),
        "client_id":          raw.get("client_id", ""),
        "client_secret":      raw.get("client_secret", ""),
        "client_secret_present": bool(raw.get("client_secret")),
        "api_host":           raw.get("api_host") or _DEFAULT_API_HOST,
        "auth_host":          _AUTH_HOSTS[environment],
    }


def save_config(cfg: dict[str, Any]) -> None:
    environment = cfg.get("environment") or "production"
    if environment not in _AUTH_HOSTS:
        environment = "production"
    payload: dict[str, Any] = {
        "enabled":     bool(cfg.get("enabled")),
        "environment": environment,
        "partner_id":  (cfg.get("partner_id") or "").strip(),
        "client_id":   (cfg.get("client_id") or "").strip(),
        "api_host":    (cfg.get("api_host") or "").strip() or _DEFAULT_API_HOST,
    }
    secret = (cfg.get("client_secret") or "").strip()
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
    # A saved config (new host/credentials) invalidates any cached
    # token from before the change.
    _token_cache.clear()


def is_enabled() -> bool:
    """Gates the nav item — the admin's on/off switch, independent of
    whether credentials are actually filled in yet (an enabled-but-
    unconfigured state shows a friendly setup prompt on the page
    itself rather than hiding the nav entirely)."""
    return load_config()["enabled"]


def is_configured() -> bool:
    cfg = load_config()
    return bool(cfg["partner_id"] and cfg["client_id"] and cfg["client_secret"])


# ---------------------------------------------------------------------------
# OAuth2 client_credentials
# ---------------------------------------------------------------------------

def _get_access_token(cfg: dict[str, Any], *, timeout: float) -> str:
    if not (cfg["partner_id"] and cfg["client_id"] and cfg["client_secret"]):
        raise PrintixPartnerError(
            "Printix partner API is not configured — set partner ID, "
            "client ID and client secret in Settings → Printix Partner.")

    cache_key = (cfg["auth_host"], cfg["client_id"], cfg["partner_id"])
    cached = _token_cache.get(cache_key)
    if cached and cached[1] > time.time():
        return cached[0]

    url = cfg["auth_host"].rstrip("/") + "/oauth/token"
    try:
        r = httpx.post(
            url,
            data={"grant_type": "client_credentials",
                  "client_id": cfg["client_id"],
                  "client_secret": cfg["client_secret"]},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=timeout,
        )
    except httpx.HTTPError as e:
        raise PrintixPartnerError(
            f"could not reach {cfg['auth_host']}: "
            f"{e.__class__.__name__}: {e}") from e

    if r.status_code >= 400:
        raise PrintixPartnerError(_readable_auth_error(r))

    try:
        data = r.json()
    except ValueError as e:
        raise PrintixPartnerError(
            f"token response wasn't JSON: {e}") from e

    token = data.get("access_token") or ""
    if not token:
        raise PrintixPartnerError(
            "token response had no access_token field")
    expires_in = data.get("expires_in")
    try:
        expires_in = int(expires_in)
    except (TypeError, ValueError):
        expires_in = 300
    expires_at = time.time() + max(30, expires_in - _TOKEN_EXPIRY_BUFFER_SECONDS)
    _token_cache[cache_key] = (token, expires_at)
    return token


def _readable_auth_error(r: httpx.Response) -> str:
    if r.status_code in (400, 401):
        return ("authentication rejected (HTTP %d) — check client ID and "
                "client secret in Settings → Printix Partner: %s"
                % (r.status_code, r.text[:200]))
    return f"HTTP {r.status_code} from {r.request.url}: {r.text[:200]}"


# ---------------------------------------------------------------------------
# Tenants
# ---------------------------------------------------------------------------

def _headers(cfg: dict[str, Any], *, timeout: float) -> dict[str, str]:
    token = _get_access_token(cfg, timeout=timeout)
    return {"Authorization": f"Bearer {token}",
            "Content-Type": "application/json"}


def _base_url(cfg: dict[str, Any]) -> str:
    return (f"{cfg['api_host'].rstrip('/')}/public/partners/"
            f"{cfg['partner_id']}/tenants")


def _extract_tenant_id(tenant: dict[str, Any]) -> dict[str, Any]:
    """The API is HAL-style — a tenant object carries no plain 'id'
    field, only ``_links.self.href`` ending in the tenant's UUID.
    Inject a plain ``tenant_id`` so routes/templates never need to
    know about HAL link parsing."""
    href = (((tenant.get("_links") or {}).get("self") or {}).get("href") or "")
    tenant["tenant_id"] = href.rstrip("/").rsplit("/", 1)[-1] if href else ""
    return tenant


def _readable_api_error(r: httpx.Response, *, context: str) -> str:
    if r.status_code == 401:
        return f"{context}: authentication rejected (HTTP 401) — access token may be stale"
    if r.status_code == 404:
        return f"{context}: not found (HTTP 404)"
    if r.status_code == 409:
        return f"{context}: conflict (HTTP 409) — tenant_domain is probably already taken: {r.text[:200]}"
    return f"{context}: HTTP {r.status_code}: {r.text[:200]}"


def list_tenants(cfg: dict[str, Any] | None = None,
                 *, timeout: float = 20.0) -> list[dict[str, Any]]:
    cfg = cfg or load_config()
    try:
        r = httpx.get(_base_url(cfg), headers=_headers(cfg, timeout=timeout),
                      timeout=timeout)
    except httpx.HTTPError as e:
        raise PrintixPartnerError(
            f"could not reach {cfg['api_host']}: {e.__class__.__name__}: {e}") from e
    if r.status_code >= 400:
        raise PrintixPartnerError(_readable_api_error(r, context="listing tenants"))
    try:
        data = r.json()
    except ValueError as e:
        raise PrintixPartnerError(f"tenant list response wasn't JSON: {e}") from e
    return [_extract_tenant_id(t) for t in (data.get("tenants") or [])]


def create_tenant(tenant_name: str, tenant_domain: str,
                  initial_user: dict[str, Any] | None = None,
                  cfg: dict[str, Any] | None = None,
                  *, timeout: float = 20.0) -> dict[str, Any]:
    cfg = cfg or load_config()
    body: dict[str, Any] = {
        "tenant_name": tenant_name,
        "tenant_domain": tenant_domain,
    }
    if initial_user and (initial_user.get("email") or "").strip():
        body["initial_user"] = {
            "email": initial_user["email"].strip(),
            "name": (initial_user.get("name") or "").strip(),
            "create_as_admin": bool(initial_user.get("create_as_admin")),
        }
    try:
        r = httpx.post(_base_url(cfg), headers=_headers(cfg, timeout=timeout),
                       json=body, timeout=timeout)
    except httpx.HTTPError as e:
        raise PrintixPartnerError(
            f"could not reach {cfg['api_host']}: {e.__class__.__name__}: {e}") from e
    if r.status_code >= 400:
        raise PrintixPartnerError(_readable_api_error(r, context="creating tenant"))
    try:
        return _extract_tenant_id(r.json())
    except ValueError as e:
        raise PrintixPartnerError(f"create-tenant response wasn't JSON: {e}") from e


def get_tenant(tenant_id: str, cfg: dict[str, Any] | None = None,
               *, timeout: float = 20.0) -> dict[str, Any]:
    cfg = cfg or load_config()
    url = f"{_base_url(cfg)}/{tenant_id}"
    try:
        r = httpx.get(url, headers=_headers(cfg, timeout=timeout), timeout=timeout)
    except httpx.HTTPError as e:
        raise PrintixPartnerError(
            f"could not reach {cfg['api_host']}: {e.__class__.__name__}: {e}") from e
    if r.status_code >= 400:
        raise PrintixPartnerError(_readable_api_error(r, context="fetching tenant"))
    try:
        tenant = r.json()
    except ValueError as e:
        raise PrintixPartnerError(f"tenant response wasn't JSON: {e}") from e
    tenant["tenant_id"] = tenant_id
    return tenant


def get_billing_info(tenant_id: str, cfg: dict[str, Any] | None = None,
                     *, timeout: float = 20.0) -> dict[str, Any]:
    cfg = cfg or load_config()
    url = f"{_base_url(cfg)}/{tenant_id}/billing-info"
    try:
        r = httpx.get(url, headers=_headers(cfg, timeout=timeout), timeout=timeout)
    except httpx.HTTPError as e:
        raise PrintixPartnerError(
            f"could not reach {cfg['api_host']}: {e.__class__.__name__}: {e}") from e
    if r.status_code >= 400:
        raise PrintixPartnerError(_readable_api_error(r, context="fetching billing info"))
    try:
        return r.json()
    except ValueError as e:
        raise PrintixPartnerError(f"billing-info response wasn't JSON: {e}") from e


def test_connection(cfg: dict[str, Any] | None = None,
                    *, timeout: float = 20.0) -> int:
    """Exercise the full auth + list flow, return how many tenants
    came back. Raises PrintixPartnerError with a readable message on
    any failure — used by the Settings 'test connection' button."""
    cfg = cfg or load_config()
    tenants = list_tenants(cfg, timeout=timeout)
    return len(tenants)
