"""Azure self-management — v0.24.3.

Lets a running App Service switch its own ``DATABASE_URL`` to an Azure
SQL Database and restart itself, with no Azure credential ever stored
anywhere in the app. The site authenticates as *itself* via a
System-Assigned Managed Identity: Azure's Instance Metadata Service
(IMDS, reachable only from inside the running compute resource) mints
short-lived ARM tokens on demand.

Everything here is a no-op / clean failure outside Azure App Service
(local dev, tests, CI) — IMDS simply isn't reachable there.

Safety: ``PUT .../config/appsettings`` REPLACES the entire app-settings
collection. :func:`switch_database_url` always fetches the full current
set first and merges into it — a partial/blind PUT would silently wipe
``FERNET_KEY`` and every other secret (mail, LLM, Entra client_secret,
existing SQL passwords), permanently bricking anything encrypted with
the lost key.
"""

from __future__ import annotations

import os

import httpx

_IMDS_TOKEN_URL = "http://169.254.169.254/metadata/identity/oauth2/token"
_ARM_BASE = "https://management.azure.com"
_ARM_API_VERSION = "2023-12-01"
_ARM_RESOURCE = "https://management.azure.com/"


def _site_identity() -> tuple[str, str, str] | None:
    """``(subscription_id, resource_group, site_name)`` if all three
    are known, else ``None``. ``WEBSITE_SITE_NAME`` is auto-injected by
    App Service; the other two are set by the Bicep template (v0.24.3+)
    or the one-time az CLI bootstrap for pre-existing deployments."""
    sub = os.environ.get("AZURE_SUBSCRIPTION_ID", "").strip()
    rg = os.environ.get("AZURE_RESOURCE_GROUP", "").strip()
    site = os.environ.get("WEBSITE_SITE_NAME", "").strip()
    if sub and rg and site:
        return sub, rg, site
    return None


def _get_msi_token(timeout: float = 5.0) -> str:
    r = httpx.get(
        _IMDS_TOKEN_URL,
        params={"api-version": "2019-08-01", "resource": _ARM_RESOURCE},
        headers={"Metadata": "true"},
        timeout=timeout,
    )
    r.raise_for_status()
    token = r.json().get("access_token", "")
    if not token:
        raise RuntimeError("IMDS returned no access_token")
    return token


def probe() -> dict:
    """Non-destructive readiness check for the settings UI: does this
    process know its own Azure coordinates, and can it mint an ARM
    token for itself? Never touches app settings."""
    site = os.environ.get("WEBSITE_SITE_NAME", "").strip()
    if not site:
        # Not Azure App Service at all (local dev, CI, another host) —
        # the az CLI bootstrap wouldn't even have a site name to target.
        return {"ok": False, "stage": "unreachable", "hint": None,
                "error": "WEBSITE_SITE_NAME not set — this process isn't "
                         "running on Azure App Service."}
    ids = _site_identity()
    if ids is None:
        return {"ok": False, "stage": "config", "hint": "one_time_setup_needed",
                "error": ("AZURE_SUBSCRIPTION_ID / AZURE_RESOURCE_GROUP not set "
                          "— this deployment predates v0.24.3. Run the "
                          "one-time az CLI setup below.")}
    try:
        _get_msi_token(timeout=4.0)
    except httpx.HTTPStatusError as e:
        return {"ok": False, "stage": "identity", "hint": "one_time_setup_needed",
                "error": f"IMDS rejected the token request ({e.response.status_code}) "
                         "— this App Service has no System-Assigned Managed "
                         f"Identity yet: {e.response.text[:300]}"}
    except Exception as e:  # noqa: BLE001 — IMDS unreachable (not on Azure at all)
        return {"ok": False, "stage": "unreachable", "hint": None,
                "error": f"Instance Metadata Service unreachable ({type(e).__name__}: {e}) "
                         "— this process isn't running on Azure App Service."}
    sub, rg, site = ids
    return {"ok": True, "subscription_id": sub, "resource_group": rg, "site_name": site}


def _site_url(sub: str, rg: str, site: str, path: str) -> str:
    return (f"{_ARM_BASE}/subscriptions/{sub}/resourceGroups/{rg}"
            f"/providers/Microsoft.Web/sites/{site}{path}"
            f"?api-version={_ARM_API_VERSION}")


def get_app_settings(token: str, sub: str, rg: str, site: str,
                      timeout: float = 15.0) -> dict:
    """``POST .../config/appsettings/list`` — a POST despite the name,
    because the response can contain secret values. Returns the full
    ``{name: value}`` map exactly as Azure stores it."""
    r = httpx.post(
        _site_url(sub, rg, site, "/config/appsettings/list"),
        headers={"Authorization": f"Bearer {token}"},
        timeout=timeout,
    )
    r.raise_for_status()
    return r.json().get("properties", {}) or {}


def _put_app_settings(token: str, sub: str, rg: str, site: str,
                       settings: dict, timeout: float = 20.0) -> None:
    r = httpx.put(
        _site_url(sub, rg, site, "/config/appsettings"),
        headers={"Authorization": f"Bearer {token}"},
        json={"properties": settings},
        timeout=timeout,
    )
    r.raise_for_status()


def _restart(token: str, sub: str, rg: str, site: str, timeout: float = 20.0) -> None:
    r = httpx.post(
        _site_url(sub, rg, site, "/restart"),
        headers={"Authorization": f"Bearer {token}"},
        timeout=timeout,
    )
    r.raise_for_status()


def switch_database_url(new_database_url: str) -> dict:
    """The full automated cutover: mint a token, fetch the FULL current
    app-settings collection, merge in the new ``DATABASE_URL`` (every
    other key — ``FERNET_KEY`` above all — passes through untouched),
    PUT the merged set back, then restart the site so the new engine
    picks it up on boot. Mirrors the manual Portal flow it replaces;
    does not touch this process's own live engine."""
    ids = _site_identity()
    if ids is None:
        return {"ok": False, "error": "not_configured",
                "detail": "AZURE_SUBSCRIPTION_ID / AZURE_RESOURCE_GROUP / "
                          "WEBSITE_SITE_NAME missing — run the one-time az CLI "
                          "setup first."}
    sub, rg, site = ids
    try:
        token = _get_msi_token()
        current = get_app_settings(token, sub, rg, site)
        merged = dict(current)
        merged["DATABASE_URL"] = new_database_url
        _put_app_settings(token, sub, rg, site, merged)
        _restart(token, sub, rg, site)
    except httpx.HTTPStatusError as e:
        return {"ok": False, "error": "arm_error",
                "detail": f"{e.response.status_code}: {e.response.text[:400]}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "unexpected", "detail": f"{type(e).__name__}: {e}"}
    return {"ok": True, "settings_count": len(merged), "site_name": site}


_BOOTSTRAP_TEMPLATE = """\
# One-time setup for an App Service deployed BEFORE v0.24.3 — Bicep only
# grants the Managed Identity on a fresh or redeployed stack, so an
# already-running app needs this once. Run from any machine with the
# Azure CLI logged in (az login). Idempotent — safe to re-run.

az webapp identity assign --name {site} --resource-group {rg}

az role assignment create \\
  --assignee-object-id "$(az webapp identity show --name {site} --resource-group {rg} --query principalId -o tsv)" \\
  --assignee-principal-type ServicePrincipal \\
  --role "Website Contributor" \\
  --scope "$(az webapp show --name {site} --resource-group {rg} --query id -o tsv)"

az webapp config appsettings set --name {site} --resource-group {rg} \\
  --settings AZURE_SUBSCRIPTION_ID="$(az account show --query id -o tsv)" AZURE_RESOURCE_GROUP="{rg}"
"""


def bootstrap_instructions(site_name: str = "", resource_group: str = "") -> str:
    """One-time az CLI snippet for a pre-v0.24.3 deployment. After this
    runs once, :func:`probe` reports ready and the switch button works
    without any further manual step."""
    site = site_name or os.environ.get("WEBSITE_SITE_NAME", "<app-name>")
    rg = resource_group or os.environ.get("AZURE_RESOURCE_GROUP", "<resource-group>")
    return _BOOTSTRAP_TEMPLATE.format(site=site, rg=rg)
