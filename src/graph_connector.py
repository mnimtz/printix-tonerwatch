"""Microsoft 365 Copilot Connector (Graph external connections).

Pushes every printer TonerWatch monitors into Microsoft's Semantic
Index so Copilot for Microsoft 365 can answer questions like
"which printers at Acme are critical?" or "what's the serial of
the marketing MFP?" without a human ever opening TonerWatch.

Flow the operator has to do ONCE in Azure:

1. Register an Azure AD app (can reuse the Entra SSO app or use a
   dedicated one — the required permissions are different).
2. Grant it the *application* permission
   ``ExternalConnection.ReadWrite.OwnedBy`` on Microsoft Graph.
   Admin consent required.
3. Save the tenant ID, client ID and a fresh client secret in
   /settings → Copilot Connector.

Then a one-click "Initialise connection" call from TonerWatch:

* POST /external/connections            — creates the connection
* POST /external/connections/{id}/schema — declares the field shape
* PUT  /external/connections/{id}/items/{itemId} × N — pushes rows

After that, the daily scheduled sync keeps the index fresh.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Iterable

import httpx
from sqlalchemy import func, insert, select, update

from . import bi_client, crypto, db, printer_info


logger = logging.getLogger(__name__)

SETTINGS_KEY = "graph_connector"


def _safe_int(v, default: int, lo: int, hi: int) -> int:
    """v0.17.2: form values arrive as strings — clamp instead of crash."""
    try:
        i = int(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, i))

# Standard identifiers we use for the connection.
DEFAULT_CONNECTION_ID   = "tonerwatch-printers"
DEFAULT_CONNECTION_NAME = "Printix TonerWatch printers"
DEFAULT_CONNECTION_DESC = "Printer fleet monitored by Printix TonerWatch."

_TOKEN_ENDPOINT = ("https://login.microsoftonline.com/{tenant}"
                   "/oauth2/v2.0/token")
_GRAPH_BASE = "https://graph.microsoft.com/v1.0"


class GraphError(Exception):
    """Raised on any Graph-side failure — caller logs + surfaces."""


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
    return {
        "enabled":       bool(raw.get("enabled")),
        "tenant_id":     raw.get("tenant_id", ""),
        "client_id":     raw.get("client_id", ""),
        "client_secret": raw.get("client_secret", ""),
        "connection_id": raw.get("connection_id") or DEFAULT_CONNECTION_ID,
        "connection_name": raw.get("connection_name") or DEFAULT_CONNECTION_NAME,
        "connection_desc": raw.get("connection_desc") or DEFAULT_CONNECTION_DESC,
        "interval_hours": int(raw.get("interval_hours") or 24),
        "client_secret_present": bool(raw.get("client_secret")),
        "last_sync_at":  raw.get("last_sync_at", ""),
        "last_sync_count": int(raw.get("last_sync_count") or 0),
        "last_sync_error": raw.get("last_sync_error", ""),
    }


def save_config(cfg: dict[str, Any]) -> None:
    payload: dict[str, Any] = {
        "enabled":       bool(cfg.get("enabled")),
        "tenant_id":     (cfg.get("tenant_id") or "").strip(),
        "client_id":     (cfg.get("client_id") or "").strip(),
        "connection_id": (cfg.get("connection_id") or DEFAULT_CONNECTION_ID).strip(),
        "connection_name": (cfg.get("connection_name") or DEFAULT_CONNECTION_NAME).strip(),
        "connection_desc": (cfg.get("connection_desc") or DEFAULT_CONNECTION_DESC).strip(),
        "interval_hours": _safe_int(cfg.get("interval_hours"), 24, 1, 720),
    }
    secret = cfg.get("client_secret") or ""
    if secret:
        payload["client_secret_enc"] = crypto.encrypt(secret)
    else:
        existing = load_config()
        if existing.get("client_secret"):
            payload["client_secret_enc"] = crypto.encrypt(existing["client_secret"])
    # Preserve sync-log entries — they're informational
    existing = load_config()
    for k in ("last_sync_at", "last_sync_count", "last_sync_error"):
        if existing.get(k):
            payload[k] = existing[k]
    _write_settings(payload)


def _record_sync(count: int = 0, error: str = "") -> None:
    import datetime as _dt
    with db.get_conn() as conn:
        row = conn.execute(
            select(db.settings.c.value_json)
            .where(db.settings.c.key == SETTINGS_KEY)
        ).first()
    raw = json.loads(row[0]) if row else {}
    raw["last_sync_at"] = _dt.datetime.now(_dt.timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC")
    raw["last_sync_count"] = int(count)
    raw["last_sync_error"] = error or ""
    _write_settings(raw)


def _write_settings(payload: dict[str, Any]) -> None:
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
    cfg = load_config()
    return bool(cfg["enabled"] and cfg["tenant_id"]
                and cfg["client_id"] and cfg["client_secret"])


# ---------------------------------------------------------------------------
# Token — client credentials flow
# ---------------------------------------------------------------------------

_token_cache: dict[str, Any] = {}


def _get_token(cfg: dict[str, Any]) -> str:
    """Application (client credentials) access token for Graph.
    In-process cached until 60 s before expiry so we don't hit /token
    on every push."""
    import time
    now = time.time()
    cached = _token_cache.get("token")
    if cached and _token_cache.get("expires_at", 0) - now > 60:
        return cached
    url = _TOKEN_ENDPOINT.format(tenant=cfg["tenant_id"])
    try:
        r = httpx.post(url, data={
            "grant_type":    "client_credentials",
            "client_id":     cfg["client_id"],
            "client_secret": cfg["client_secret"],
            "scope":         "https://graph.microsoft.com/.default",
        }, timeout=15.0)
    except httpx.HTTPError as e:
        raise GraphError(f"token request failed: {e}") from e
    if r.status_code != 200:
        raise GraphError(f"token error HTTP {r.status_code}: {r.text[:200]}")
    data = r.json()
    _token_cache["token"] = data["access_token"]
    _token_cache["expires_at"] = now + int(data.get("expires_in", 3600))
    return _token_cache["token"]


def _graph_call(cfg: dict[str, Any], method: str, path: str,
                json_body: dict | None = None,
                expected_status: Iterable[int] = (200, 201, 202, 204)) -> dict | None:
    """Wrap httpx with Graph headers + normalized error handling."""
    url = f"{_GRAPH_BASE}{path}"
    headers = {
        "Authorization": f"Bearer {_get_token(cfg)}",
        "Content-Type":  "application/json",
    }
    try:
        r = httpx.request(method, url, headers=headers, json=json_body,
                          timeout=30.0)
    except httpx.HTTPError as e:
        raise GraphError(f"{method} {path}: {e}") from e
    if r.status_code not in expected_status:
        raise GraphError(
            f"{method} {path}: HTTP {r.status_code}: {r.text[:300]}")
    if r.text:
        try:
            return r.json()
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# Connection + schema
# ---------------------------------------------------------------------------

# The schema Copilot uses to index our printer items. Every field
# here needs the right combination of isSearchable / isQueryable /
# isRetrievable so Copilot can search AND display each column.
SCHEMA_DEFINITION = {
    "baseType": "microsoft.graph.externalItem",
    "properties": [
        {"name": "printerName",  "type": "String",
         "isSearchable": True, "isQueryable": True, "isRetrievable": True,
         "labels": ["title"]},
        {"name": "customerName", "type": "String",
         "isSearchable": True, "isQueryable": True, "isRetrievable": True},
        {"name": "model",        "type": "String",
         "isSearchable": True, "isQueryable": True, "isRetrievable": True},
        {"name": "location",     "type": "String",
         "isSearchable": True, "isQueryable": True, "isRetrievable": True},
        {"name": "serialNumber", "type": "String",
         "isSearchable": True, "isQueryable": True, "isRetrievable": True},
        {"name": "groupName",    "type": "String",
         "isSearchable": True, "isQueryable": True, "isRetrievable": True},
        {"name": "worstSeverity","type": "String",
         "isSearchable": False, "isQueryable": True, "isRetrievable": True},
        {"name": "assetTag",     "type": "String",
         "isSearchable": True, "isQueryable": True, "isRetrievable": True},
        {"name": "vendor",       "type": "String",
         "isSearchable": True, "isQueryable": True, "isRetrievable": True},
    ],
}


def ensure_connection(cfg: dict[str, Any]) -> str:
    """Create the connection if it doesn't exist, then push the
    schema. Idempotent — safe to call every time."""
    connection_id = cfg["connection_id"]
    # Try to fetch — if 404, create.
    try:
        _graph_call(cfg, "GET", f"/external/connections/{connection_id}",
                    expected_status=(200,))
    except GraphError as e:
        if "HTTP 404" not in str(e):
            raise
        _graph_call(cfg, "POST", "/external/connections", json_body={
            "id":          connection_id,
            "name":        cfg["connection_name"],
            "description": cfg["connection_desc"],
        }, expected_status=(201,))

    # Register the schema. Graph will 202 if it's already being
    # processed; treat both as OK.
    _graph_call(cfg, "PATCH",
                f"/external/connections/{connection_id}/schema",
                json_body=SCHEMA_DEFINITION,
                expected_status=(200, 201, 202, 204))
    return connection_id


# ---------------------------------------------------------------------------
# Items — push one printer as an ExternalItem
# ---------------------------------------------------------------------------

def build_item(row: dict[str, Any], public_base_url: str = "") -> dict[str, Any]:
    """Turn one enriched printer row into the ExternalItem shape.

    Uses (customer_id, printer_id) as the stable Graph item id so
    re-syncing updates existing items instead of duplicating."""
    item_id = f"{row.get('customer_id', 0)}--{row.get('id', '') or row.get('printer_id', '')}"
    # Sanitise: Graph item IDs must be url-safe
    item_id = "".join(c if c.isalnum() or c in "-_." else "-" for c in item_id)

    detail_url = ""
    if public_base_url:
        detail_url = (public_base_url.rstrip("/")
                      + f"/toner/{row.get('customer_id')}/"
                      + f"{row.get('id') or row.get('printer_id')}/info")

    return {
        "id": item_id,
        "acl": [
            # Grant read to everyone in the tenant — Copilot honours
            # tenant-scoped ACLs. Refine later via groups.
            {"type": "everyone", "value": "everyone", "accessType": "grant"},
        ],
        "properties": {
            "printerName":  str(row.get("printer_name") or "(unnamed)"),
            "customerName": str(row.get("customer_name") or ""),
            "model":        str(row.get("model") or ""),
            "location":     str(row.get("location") or ""),
            "serialNumber": str(row.get("serial_number") or ""),
            "groupName":    str(row.get("group_name") or ""),
            "worstSeverity": str(row.get("worst_severity") or "UNKNOWN"),
            "assetTag":     str(row.get("asset_tag") or ""),
            "vendor":       str(row.get("vendor") or ""),
        },
        "content": {
            "type": "text",
            "value": _build_search_text(row),
        },
        "activities": [],
    }


def _build_search_text(row: dict[str, Any]) -> str:
    """A short natural-language blurb Copilot indexes for full-text
    search. Everything relevant a human would type stays in here."""
    parts = []
    name = row.get("printer_name") or "(unnamed printer)"
    parts.append(name)
    if row.get("model"):    parts.append(f"model {row['model']}")
    if row.get("vendor"):   parts.append(f"vendor {row['vendor']}")
    if row.get("location"): parts.append(f"location {row['location']}")
    if row.get("serial_number"): parts.append(f"serial {row['serial_number']}")
    if row.get("group_name"): parts.append(f"group {row['group_name']}")
    if row.get("asset_tag"): parts.append(f"asset {row['asset_tag']}")
    if row.get("customer_name"): parts.append(f"customer {row['customer_name']}")
    if row.get("worst_severity") in ("CRITICAL", "WARN"):
        parts.append(f"status {row['worst_severity']}")
    return " · ".join(parts)


def push_item(cfg: dict[str, Any], item: dict[str, Any]) -> None:
    _graph_call(cfg, "PUT",
                f"/external/connections/{cfg['connection_id']}/items/{item['id']}",
                json_body=item,
                expected_status=(200, 201, 202, 204))


def delete_item(cfg: dict[str, Any], item_id: str) -> None:
    _graph_call(cfg, "DELETE",
                f"/external/connections/{cfg['connection_id']}/items/{item_id}",
                expected_status=(200, 204, 404))


# ---------------------------------------------------------------------------
# Sync all printers
# ---------------------------------------------------------------------------

def sync_all_printers(*, public_base_url: str = "") -> tuple[int, str]:
    """Bulk-sync every active customer's printers to Graph. Returns
    ``(pushed_count, error_str)`` — error is empty on full success.

    Called from the manual "Sync now" button and from the daily
    scheduler tick. Safe to call while Graph is initialising — the
    ensure_connection() call handles both new and existing state.
    """
    cfg = load_config()
    if not is_configured():
        return 0, "not_configured"
    try:
        ensure_connection(cfg)
    except GraphError as e:
        _record_sync(error=f"ensure_connection: {str(e)[:200]}")
        return 0, str(e)[:200]

    # Enumerate every active customer's printers, enrich + push
    with db.get_conn() as conn:
        customers = [db._row_to_dict(r) for r in conn.execute(
            select(db.customers).where(db.customers.c.active == 1)
        ).all()]

    pushed = 0
    errors: list[str] = []
    for c in customers:
        if not (c.get("sql_server") and c.get("sql_database")
                and c.get("sql_username")):
            continue
        try:
            bi_c = bi_client.customer_for_bi(c)
            rows = bi_client.fetch_all_printer_supplies(bi_c) or []
        except Exception as e:  # noqa: BLE001
            errors.append(f"{c['name']}: fetch failed ({str(e)[:80]})")
            continue

        info_map = printer_info.list_info_for_customer(c["id"])
        for p in rows:
            enriched = printer_info.enrich(p, info_map.get(p["id"]))
            enriched["customer_id"]   = c["id"]
            enriched["customer_name"] = c["name"]
            enriched["worst_severity"] = _worst(enriched.get("supplies") or [],
                                                 int(c["warn_pct"] or 20),
                                                 int(c["critical_pct"] or 5))
            item = build_item(enriched, public_base_url=public_base_url)
            try:
                push_item(cfg, item)
                pushed += 1
            except GraphError as e:
                errors.append(f"{c['name']}/{item['id']}: {str(e)[:80]}")

    err_msg = "; ".join(errors[:3]) if errors else ""
    _record_sync(count=pushed, error=err_msg)
    if errors:
        logger.warning("graph sync: pushed %d, %d errors — %s",
                       pushed, len(errors), err_msg)
    else:
        logger.info("graph sync: pushed %d items", pushed)
    return pushed, err_msg


def _worst(supplies: list[dict], warn: int, crit: int) -> str:
    order = {"CRITICAL": 3, "WARN": 2, "OK": 1, "UNKNOWN": 0}
    worst = "UNKNOWN"
    for s in supplies:
        level = s.get("level")
        if level is None:
            continue
        sev = "CRITICAL" if level <= crit else ("WARN" if level <= warn else "OK")
        if order[sev] > order[worst]:
            worst = sev
    return worst
