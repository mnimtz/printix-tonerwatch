"""Printix BI-DB (Azure SQL) client — read-only queries for supply / status data.

Every Printix tenant has an Analytics database on Azure SQL. The customer's
BI credentials (server/database/username/password) live in TonerWatch's own
`customers` table, Fernet-encrypted at rest. Callers decrypt the password
before invoking anything here — this module never touches the keyring.

The module is deliberately small and defensive:

* **Short-lived in-memory cache** per (customer_id, printer_id). Azure SQL
  auto-pauses after idle, so a cold first query can take 15–25 seconds;
  identical calls right after (e.g. opening a detail view twice) should
  reuse the cached result.
* **Timeouts and exceptions are swallowed** and return ``None``. Toner data
  is a nice-to-have — a temporary BI-DB outage must NEVER 500 the request
  handler that renders the toner grid.
* **Test-connection** helper used by the customer edit form (unchanged
  from earlier — kept here so all BI-DB touchpoints live in one file).
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Test-connection (used by /customers/test-connection)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ConnectionResult:
    ok: bool
    message: str
    server_version: str = ""


def test_connection(server: str, database: str, username: str,
                    password: str, *, port: int = 1433,
                    timeout: int = 5) -> ConnectionResult:
    """Open a socket to the BI-DB and run `SELECT @@VERSION`."""
    if not server or not database or not username:
        return ConnectionResult(False,
                                "server, database and username are required")
    try:
        import pymssql  # type: ignore
    except Exception as exc:  # pragma: no cover
        return ConnectionResult(False, f"pymssql not available: {exc}")

    conn: Any = None
    try:
        conn = pymssql.connect(
            server=server, user=username, password=password or "",
            database=database, port=port,
            timeout=timeout, login_timeout=timeout,
        )
        cur = conn.cursor()
        cur.execute("SELECT @@VERSION")
        version = (cur.fetchone() or ("",))[0]
        cur.close()
        first = version.splitlines()[0].strip() if version else ""
        return ConnectionResult(True, "Connection successful", first[:100])
    except Exception as exc:
        msg = str(exc).strip().splitlines()[0][:200]
        return ConnectionResult(False, msg or exc.__class__.__name__)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Marker parsing
# ---------------------------------------------------------------------------

# Printix stores per-marker fill levels in `device_readings.additional_readings`
# as a JSON blob keyed by SNMP-style names. Map them to short colour codes.
_MARKER_TO_COLOR = {
    "MARKER_BLACK":   "K",
    "MARKER_CYAN":    "C",
    "MARKER_MAGENTA": "M",
    "MARKER_YELLOW":  "Y",
}
_COLOR_ORDER = ["K", "C", "M", "Y"]


def _parse_markers(raw: Optional[str]) -> list[dict]:
    """Extract MARKER_* percentages from the additional_readings JSON."""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(data, dict):
        return []

    supplies: list[dict] = []
    for marker_key, color in _MARKER_TO_COLOR.items():
        val = data.get(marker_key)
        if val is None:
            continue
        try:
            percent = int(str(val).strip())
        except (ValueError, TypeError):
            continue
        if 0 <= percent <= 100:
            supplies.append({"color": color, "level": percent})
    supplies.sort(key=lambda s: _COLOR_ORDER.index(s["color"]))
    return supplies


def _parse_error_states(raw: Any) -> list[str]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return [str(x) for x in data if x]
    except (ValueError, TypeError):
        pass
    return []


# ---------------------------------------------------------------------------
# Credentials guard
# ---------------------------------------------------------------------------

def _has_creds(customer: dict) -> bool:
    return bool(
        customer.get("sql_server")
        and customer.get("sql_database")
        and customer.get("sql_username")
        # Empty password is legal for some Windows-auth setups
    )


def customer_for_bi(customer: dict) -> dict:
    """Return a copy of the customer dict with ``sql_password`` decrypted
    from ``sql_password_enc``. Idempotent — if ``sql_password`` is already
    set, the customer is returned unchanged. Empty password stays empty.

    Callers use this helper to decouple bi_client from the Fernet keyring:
    the query functions only ever see plaintext passwords, and route
    handlers do the one-line decrypt at the boundary.
    """
    if customer.get("sql_password"):
        return customer
    enc = customer.get("sql_password_enc") or ""
    if not enc:
        return {**customer, "sql_password": ""}
    from . import crypto
    try:
        return {**customer, "sql_password": crypto.decrypt(enc)}
    except crypto.CryptoError:
        # A rotated Fernet key or corrupted ciphertext should not take
        # down the toner grid — surface as an empty password (queries
        # will fail auth and be caught by the query try/except).
        logger.warning("customer_for_bi: could not decrypt sql_password_enc "
                       "for customer %s", customer.get("id"))
        return {**customer, "sql_password": ""}


def _connect(customer: dict, *, login_timeout: int = 30, timeout: int = 60):
    """Open a pymssql connection. Raises on failure — callers wrap in try/except.
    """
    import pymssql  # noqa: WPS433 — lazy import
    port_raw = customer.get("sql_port")
    try:
        port = int(port_raw) if port_raw else 1433
    except (TypeError, ValueError):
        port = 1433
    return pymssql.connect(
        server=customer["sql_server"],
        user=customer["sql_username"],
        password=customer.get("sql_password") or "",
        database=customer["sql_database"],
        port=port,
        tds_version="7.4",
        login_timeout=login_timeout,
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# Single-printer supplies (with cache)
# ---------------------------------------------------------------------------

_PRINTER_SUPPLIES_CACHE: dict[tuple[int, str], tuple[float, list, float]] = {}
_PRINTER_SUPPLIES_TTL_SEC = 300   # 5 min for successful reads
_PRINTER_SUPPLIES_NEG_TTL = 60    # 1 min for empty reads (silent printers)
_CACHE_LOCK = threading.Lock()


def _safe_value(v):
    """v0.23.12 — coerce anything into something Jinja can render
    without crashing. bytes get UTF-8-decoded (or ``<N bytes>``);
    datetimes/decimals/etc. get str()'d; None + primitives pass
    through."""
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, (bytes, bytearray)):
        try:
            return bytes(v).decode("utf-8")
        except UnicodeDecodeError:
            return f"<{len(v)} bytes>"
    try:
        return str(v)
    except Exception:  # noqa: BLE001
        return "<unrepresentable>"


def fetch_printer_raw(customer: dict, printer_id: str) -> Optional[dict]:
    """v0.23.7 — return the FULL row of dbo.printers for one printer id,
    plus every column name Printix BI exposes.
    v0.23.12 — all values are pre-sanitised via _safe_value so the
    template can't fall over on bytes / datetime / decimal columns."""
    with _connect(customer) as conn:
        cur = conn.cursor()
        try:
            cur.execute("SELECT * FROM dbo.printers WHERE id = %s",
                         (printer_id,))
        except Exception as exc:  # noqa: BLE001
            logger.warning("fetch_printer_raw failed: %s", exc)
            return None
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in (cur.description or [])]
        row_dict = (dict(row) if isinstance(row, dict)
                    else dict(zip(cols, row)))
        return {"columns": cols,
                # v0.23.13 — key renamed from 'values' → 'row' because
                # Jinja resolves `raw.values` to dict.values() (the
                # bound method), not our key. UndefinedError on .get().
                "row": {k: _safe_value(v) for k, v in row_dict.items()}}


def fetch_printers_raw(customer: dict, limit: int = 10,
                        name_filter: str = "") -> Optional[dict]:
    """v0.23.8 — SELECT TOP N * FROM dbo.printers so the operator can
    dump the FULL schema (columns + first N rows). Returns::

        {"columns": ["id", "name", ...],
         "rows":    [{"id": "…", "name": "…", ...}, ...]}

    v0.23.9 — ``name_filter`` narrows by LIKE '%…%' on the name
    column so the operator can find a specific device (e.g.
    "mobileprint", "anywhere") among hundreds of printers."""
    with _connect(customer) as conn:
        cur = conn.cursor()
        # Build the SQL in one place. TOP N inline is safe (we cast to
        # int); the name filter uses a parameter so it's SQL-safe.
        sql_parts = [f"SELECT TOP {int(limit)} * FROM dbo.printers "
                      "WHERE meta_status = 'ACTIVE'"]
        params: tuple = ()
        if name_filter:
            sql_parts.append(" AND name LIKE %s")
            params = (f"%{name_filter}%",)
        sql_parts.append(" ORDER BY name")
        try:
            cur.execute("".join(sql_parts), params)
        except Exception as exc:  # noqa: BLE001
            logger.warning("fetch_printers_raw failed: %s", exc)
            return None
        cols = [d[0] for d in (cur.description or [])]
        rows_raw = cur.fetchall()
        rows = []
        for r in rows_raw:
            if isinstance(r, dict):
                base = {k: r.get(k) for k in cols}
            else:
                base = dict(zip(cols, r))
            # v0.23.12 — same sanitisation as fetch_printer_raw so the
            # JSON dump doesn't stumble on datetime/bytes/decimal even
            # with default=str; keeps the payload cleaner too.
            rows.append({k: _safe_value(v) for k, v in base.items()})
        return {"columns": cols, "rows": rows}


def list_all_tables(customer: dict) -> Optional[list[dict]]:
    """v0.24.41 — schema discovery. TonerWatch has only ever queried
    dbo.printers and dbo.device_readings; nobody has looked at what
    ELSE Printix's BI-DB exposes per tenant (e.g. a users or
    print-jobs table). Returns every table with an approximate row
    count (via sys.partitions, index_id 0/1 = heap/clustered — cheap,
    no full scan) so an operator can tell at a glance which tables
    actually hold data worth exploring further."""
    with _connect(customer) as conn:
        cur = conn.cursor()
        try:
            cur.execute("""
                SELECT s.name AS table_schema, t.name AS table_name,
                       SUM(p.rows) AS approx_row_count
                  FROM sys.tables t
                  JOIN sys.schemas s ON t.schema_id = s.schema_id
                  JOIN sys.partitions p ON t.object_id = p.object_id
                 WHERE p.index_id IN (0, 1)
                 GROUP BY s.name, t.name
                 ORDER BY s.name, t.name
            """)
        except Exception as exc:  # noqa: BLE001
            logger.warning("list_all_tables failed: %s", exc)
            return None
        cols = [d[0] for d in (cur.description or [])]
        rows = cur.fetchall()
        return [({k: r.get(k) for k in cols} if isinstance(r, dict)
                 else dict(zip(cols, r))) for r in rows]


_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_]+$")


def list_table_columns(customer: dict, table_schema: str,
                       table_name: str) -> Optional[list[dict]]:
    """Column names + types for one table — values are passed as query
    parameters (never interpolated), safe against injection regardless
    of caller discipline."""
    with _connect(customer) as conn:
        cur = conn.cursor()
        try:
            cur.execute(
                "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE "
                "FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s "
                "ORDER BY ORDINAL_POSITION",
                (table_schema, table_name),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("list_table_columns failed: %s", exc)
            return None
        cols = [d[0] for d in (cur.description or [])]
        rows = cur.fetchall()
        return [({k: r.get(k) for k in cols} if isinstance(r, dict)
                 else dict(zip(cols, r))) for r in rows]


def fetch_table_sample(customer: dict, table_schema: str, table_name: str,
                       limit: int = 5) -> Optional[dict]:
    """TOP N * FROM an arbitrary table. Table/schema names can't be
    passed as SQL parameters (they're identifiers, not values) — the
    caller MUST validate (table_schema, table_name) against a
    same-request list_all_tables() result before calling this, but as
    defense in depth this also rejects anything that isn't a bare
    alphanumeric/underscore identifier, closing the injection route
    even if a caller ever forgets that check."""
    if not (_IDENTIFIER_RE.match(table_schema) and _IDENTIFIER_RE.match(table_name)):
        raise ValueError(f"unsafe identifier: {table_schema}.{table_name}")
    with _connect(customer) as conn:
        cur = conn.cursor()
        try:
            cur.execute(
                f"SELECT TOP {int(limit)} * FROM [{table_schema}].[{table_name}]")
        except Exception as exc:  # noqa: BLE001
            logger.warning("fetch_table_sample failed: %s", exc)
            return None
        cols = [d[0] for d in (cur.description or [])]
        rows_raw = cur.fetchall()
        rows = []
        for r in rows_raw:
            base = ({k: r.get(k) for k in cols} if isinstance(r, dict)
                    else dict(zip(cols, r)))
            rows.append({k: _safe_value(v) for k, v in base.items()})
        return {"columns": cols, "rows": rows}


def list_printer_ids(customer: dict, limit: int = 30) -> list[dict]:
    """v0.23.7 — return (id, name) pairs so the diagnose view can offer
    a picker."""
    with _connect(customer) as conn:
        cur = conn.cursor()
        try:
            cur.execute(
                "SELECT TOP %d id, name FROM dbo.printers "
                "WHERE meta_status = 'ACTIVE' ORDER BY name" % int(limit))
        except Exception:  # noqa: BLE001
            return []
        rows = cur.fetchall()
        out = []
        for r in rows:
            if isinstance(r, dict):
                out.append({"id": str(r.get("id", "")), "name": r.get("name") or ""})
            else:
                out.append({"id": str(r[0]), "name": r[1] or ""})
        return out


def fetch_printer_supplies(customer: dict, printer_id: str) -> Optional[list[dict]]:
    """Latest CMYK levels for one printer — cached for 5 min.

    Returns ``[{"color":"K","level":63}, ...]`` or ``None`` on error / no data.
    """
    if not _has_creds(customer) or not printer_id:
        return None

    key = (int(customer["id"]), str(printer_id))
    now = time.time()
    with _CACHE_LOCK:
        entry = _PRINTER_SUPPLIES_CACHE.get(key)
        if entry and (now - entry[0]) < entry[2]:
            return entry[1] or None

    result = _query_printer_supplies(customer, printer_id)
    ttl = _PRINTER_SUPPLIES_TTL_SEC if result else _PRINTER_SUPPLIES_NEG_TTL
    with _CACHE_LOCK:
        _PRINTER_SUPPLIES_CACHE[key] = (now, result or [], ttl)
    return result


def _query_printer_supplies(customer: dict,
                            printer_id: str) -> Optional[list[dict]]:
    try:
        import pymssql  # noqa
    except ImportError:
        logger.debug("pymssql not available — skipping BI query")
        return None
    conn = None
    try:
        conn = _connect(customer, login_timeout=8, timeout=8)
        cur = conn.cursor(as_dict=True)
        cur.execute(
            """SELECT TOP 1 additional_readings
                 FROM dbo.device_readings
                WHERE printer_id = %s
                  AND additional_readings IS NOT NULL
             ORDER BY received_time DESC""",
            (printer_id,),
        )
        row = cur.fetchone()
        return _parse_markers(row["additional_readings"]) if row else None
    except Exception as exc:  # noqa: BLE001
        logger.info("fetch_printer_supplies failed (printer=%s): %s",
                    printer_id, str(exc)[:200])
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# All-printers snapshot (used by /toner grid + alerts runner)
# ---------------------------------------------------------------------------

_ALL_SUPPLIES_CACHE: dict[int, tuple[float, list]] = {}
_ALL_SUPPLIES_TTL_SEC = 600  # 10 min


def fetch_all_printer_supplies_cached_only(customer: dict) -> Optional[list[dict]]:
    """Only read from the cache — return ``None`` if nothing is stored."""
    if not _has_creds(customer):
        return None
    key = int(customer["id"])
    now = time.time()
    with _CACHE_LOCK:
        entry = _ALL_SUPPLIES_CACHE.get(key)
        if entry and (now - entry[0]) < _ALL_SUPPLIES_TTL_SEC:
            return entry[1]
    return None


def fetch_all_printer_supplies(customer: dict, *, force: bool = False) -> Optional[list[dict]]:
    """Latest reading for every active printer in the customer's tenant.

    Returns::

        [{
          "printer_id":     "…-uuid-…",
          "printer_name":   "Kyocera-EG",
          "location":       "3rd floor",
          "model":          "Kyocera ECOSYS M2540dn",
          "vendor":         "Kyocera",
          "supplies":       [{"color":"K","level":26}, …],
          "error_states":   ["LOW_TONER"],
          "reported_state": "IDLE",
          "received_time":  datetime,
        }, …]

    Cached for 10 minutes per customer. Returns ``None`` on error or when
    the customer has no BI credentials.

    ``force=True`` — see fetch_registered_users' docstring in this
    module (v0.24.48): always re-queries, but never blanks the cache
    before the new result lands.
    """
    if not _has_creds(customer):
        return None

    key = int(customer["id"])
    now = time.time()
    if not force:
        with _CACHE_LOCK:
            entry = _ALL_SUPPLIES_CACHE.get(key)
            if entry and (now - entry[0]) < _ALL_SUPPLIES_TTL_SEC:
                return entry[1]

    result = _query_all_supplies(customer)
    if result is not None:
        with _CACHE_LOCK:
            _ALL_SUPPLIES_CACHE[key] = (now, result)
    return result


def _query_all_supplies(customer: dict) -> Optional[list[dict]]:
    try:
        import pymssql  # noqa
    except ImportError:
        return None

    conn = None
    try:
        conn = _connect(customer, login_timeout=30, timeout=60)
        cur = conn.cursor(as_dict=True)

        # Two-stage query — a global JOIN over device_readings (potentially
        # hundreds of thousands of rows) times out; per-printer TOP 1 hits
        # the (printer_id, received_time) index cleanly instead.
        # v0.8.0: pull the serial_number too — Printix populates it via SNMP.
        # Fall back to empty string if the column isn't in this tenant's
        # schema (older Printix BI dumps don't expose it), so the caller
        # never sees a missing key.
        try:
            # v0.23.13 — pull `type` too (NETWORK / ANYWHERE …) so the
            # hide-Anywhere filter finally has the real marker.
            cur.execute("""SELECT id AS printer_id, name AS printer_name, location,
                                  model_name AS model, vendor_name AS vendor,
                                  serial_number, type AS printer_type
                             FROM dbo.printers
                            WHERE meta_status = 'ACTIVE'""")
            printers = cur.fetchall()
        except Exception:
            # Legacy schema without serial_number / type columns.
            try:
                cur.execute("""SELECT id AS printer_id, name AS printer_name, location,
                                      model_name AS model, vendor_name AS vendor,
                                      type AS printer_type
                                 FROM dbo.printers
                                WHERE meta_status = 'ACTIVE'""")
                printers = cur.fetchall()
                for p in printers:
                    p["serial_number"] = ""
            except Exception:
                cur.execute("""SELECT id AS printer_id, name AS printer_name, location,
                                      model_name AS model, vendor_name AS vendor
                                 FROM dbo.printers
                                WHERE meta_status = 'ACTIVE'""")
                printers = cur.fetchall()
                for p in printers:
                    p["serial_number"] = ""
                    p["printer_type"] = ""

        rows: list[dict] = []
        for p in printers:
            pid = p["printer_id"]
            try:
                cur.execute("""SELECT TOP 1 additional_readings,
                                      detected_error_states,
                                      printer_reported_state,
                                      received_time
                                 FROM dbo.device_readings
                                WHERE printer_id = %s
                             ORDER BY received_time DESC""", (pid,))
                reading = cur.fetchone()
            except Exception:
                reading = None
            pid_str = str(pid)
            rows.append({
                # v0.14.1: "id" is a first-class alias for "printer_id".
                # Consumers in toner_routes / printer_info / graph_connector
                # / templates read one or the other — keeping both means we
                # never fall off the KeyError cliff again.
                "id":            pid_str,
                "printer_id":    pid_str,
                "printer_name":  p.get("printer_name") or "",
                "location":      p.get("location") or "",
                "model":         p.get("model") or "",
                "vendor":        p.get("vendor") or "",
                "type":          p.get("printer_type") or "",
                "serial_number": p.get("serial_number") or "",
                "supplies":       _parse_markers(reading.get("additional_readings") if reading else None),
                "error_states":   _parse_error_states(reading.get("detected_error_states") if reading else None),
                "reported_state": (reading.get("printer_reported_state") if reading else "") or "",
                "received_time":  reading.get("received_time") if reading else None,
            })
    except Exception as exc:  # noqa: BLE001
        logger.info("fetch_all_printer_supplies failed for customer %s: %s",
                    customer.get("id"), str(exc)[:200])
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return rows


# ---------------------------------------------------------------------------
# Registered vs. genuinely active Printix users — v0.24.42, corrected v0.24.46
# ---------------------------------------------------------------------------
#
# v0.24.46: "active" originally meant dbo.users.meta_status = 'ACTIVE',
# which only means the account exists and isn't disabled — Marcus
# found (his own test tenant, and confirmed again against Printix's
# own Partner Portal "Active users" graph for a real customer: 131
# registered accounts, 2-3 shown as genuinely active) that this
# massively overcounts real usage. Split into two concepts now:
#
# * "registered" — dbo.users, meta_status = 'ACTIVE' (what the old
#   "active users" used to mean; kept because license/seat counting
#   still cares about it).
# * "active" — users who actually submitted a print job recently
#   (dbo.jobs.tenant_user_id, joined back to dbo.users for name/email/
#   department), which is what genuinely reflects usage — and lines up
#   with what Printix's own partner portal tracks.

_REGISTERED_USERS_CACHE: dict[int, tuple[float, list]] = {}
_REGISTERED_USERS_TTL_SEC = 600  # 10 min — same cadence as the printer-supply cache

_ACTIVE_USERS_CACHE: dict[int, tuple[float, list]] = {}
_ACTIVE_USERS_TTL_SEC = 600
_ACTIVE_USERS_WINDOW_DAYS = 30


def fetch_registered_users_cached_only(customer: dict) -> Optional[list[dict]]:
    """Only read from the cache — return ``None`` if nothing is stored
    or the entry is stale. Used by the dashboard/customer-list/reports,
    which must never block on a live BI-DB round trip."""
    if not _has_creds(customer):
        return None
    key = int(customer["id"])
    now = time.time()
    with _CACHE_LOCK:
        entry = _REGISTERED_USERS_CACHE.get(key)
        if entry and (now - entry[0]) < _REGISTERED_USERS_TTL_SEC:
            return entry[1]
    return None


def fetch_registered_users(customer: dict, *, force: bool = False) -> Optional[list[dict]]:
    """Registered Printix users for one tenant (account exists, not
    disabled) — email, name, department. Discovered via
    /toner/bi_schema: dbo.users is RLS-scoped to just this customer's
    own tenant, same as dbo.printers. Cached 10 min; the background
    cache-refresh tick (toner_alerts._tick_cache_refresh) warms this
    alongside the printer-supply cache so callers almost always hit
    the cached-only path above.

    ``force=True`` (used by the background tick) skips the freshness
    check and always does a live query, but — unlike calling
    invalidate_customer_cache() first — never clears the existing
    entry before the new one is ready. Cached-only readers keep
    serving the last-known value throughout the query instead of
    seeing a gap (v0.24.48)."""
    if not _has_creds(customer):
        return None
    key = int(customer["id"])
    now = time.time()
    if not force:
        with _CACHE_LOCK:
            entry = _REGISTERED_USERS_CACHE.get(key)
            if entry and (now - entry[0]) < _REGISTERED_USERS_TTL_SEC:
                return entry[1]

    result = _query_registered_users(customer)
    if result is not None:
        with _CACHE_LOCK:
            _REGISTERED_USERS_CACHE[key] = (now, result)
    return result


def _query_registered_users(customer: dict) -> Optional[list[dict]]:
    try:
        import pymssql  # noqa
    except ImportError:
        return None
    try:
        with _connect(customer, login_timeout=30, timeout=60) as conn:
            cur = conn.cursor(as_dict=True)
            cur.execute("""SELECT email, name, department
                             FROM dbo.users
                            WHERE meta_status = 'ACTIVE'
                            ORDER BY name, email""")
            rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.warning("_query_registered_users failed for customer %s: %s",
                       customer.get("id"), exc)
        return None
    return [{"email": _safe_value(r.get("email")) or "",
             "name": _safe_value(r.get("name")) or "",
             "department": _safe_value(r.get("department")) or ""}
            for r in rows]


def fetch_active_users_cached_only(customer: dict) -> Optional[list[dict]]:
    """Only read from the cache — return ``None`` if nothing is stored
    or the entry is stale."""
    if not _has_creds(customer):
        return None
    key = int(customer["id"])
    now = time.time()
    with _CACHE_LOCK:
        entry = _ACTIVE_USERS_CACHE.get(key)
        if entry and (now - entry[0]) < _ACTIVE_USERS_TTL_SEC:
            return entry[1]
    return None


def fetch_active_users(customer: dict, *, force: bool = False) -> Optional[list[dict]]:
    """Genuinely active Printix users for one tenant — distinct users
    who submitted at least one print job in the last
    ``_ACTIVE_USERS_WINDOW_DAYS`` days — email, name, department.
    Cached 10 min; warmed by the same background tick as
    fetch_registered_users.

    ``force=True`` — see fetch_registered_users' docstring (v0.24.48)."""
    if not _has_creds(customer):
        return None
    key = int(customer["id"])
    now = time.time()
    if not force:
        with _CACHE_LOCK:
            entry = _ACTIVE_USERS_CACHE.get(key)
            if entry and (now - entry[0]) < _ACTIVE_USERS_TTL_SEC:
                return entry[1]

    result = _query_active_users(customer)
    if result is not None:
        with _CACHE_LOCK:
            _ACTIVE_USERS_CACHE[key] = (now, result)
    return result


def _query_active_users(customer: dict) -> Optional[list[dict]]:
    try:
        import pymssql  # noqa
    except ImportError:
        return None
    try:
        with _connect(customer, login_timeout=30, timeout=60) as conn:
            cur = conn.cursor(as_dict=True)
            cur.execute("""SELECT DISTINCT u.email, u.name, u.department
                             FROM dbo.jobs j
                             JOIN dbo.users u ON u.id = j.tenant_user_id
                            WHERE j.submit_time >= DATEADD(day, %s, GETUTCDATE())
                            ORDER BY u.name, u.email""",
                        (-_ACTIVE_USERS_WINDOW_DAYS,))
            rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.warning("_query_active_users failed for customer %s: %s",
                       customer.get("id"), exc)
        return None
    return [{"email": _safe_value(r.get("email")) or "",
             "name": _safe_value(r.get("name")) or "",
             "department": _safe_value(r.get("department")) or ""}
            for r in rows]


# ---------------------------------------------------------------------------
# Days-until-empty estimate
# ---------------------------------------------------------------------------

def estimate_days_until_empty(customer: dict, printer_id: str, color: str,
                              current_level: int) -> Optional[float]:
    """Rough linear extrapolation over the last 14 days.

    Defensive: returns None when the toner was refilled inside the window
    (level went up), when the sample window is < 1 day, or when the
    consumption rate is 0. Capped at 999 days.
    """
    if current_level is None or current_level <= 0 or not _has_creds(customer):
        return None
    marker_key = _color_to_marker(color)
    if not marker_key:
        return None
    try:
        import pymssql  # noqa
    except ImportError:
        return None

    conn = None
    try:
        conn = _connect(customer, login_timeout=6, timeout=10)
        cur = conn.cursor(as_dict=True)
        cur.execute("""
            SELECT TOP 1 additional_readings, received_time
              FROM dbo.device_readings
             WHERE printer_id = %s
               AND additional_readings LIKE %s
               AND received_time <= DATEADD(day, -1, SYSUTCDATETIME())
               AND received_time >= DATEADD(day, -14, SYSUTCDATETIME())
          ORDER BY received_time ASC
        """, (printer_id, f"%{marker_key}%"))
        row = cur.fetchone()
        if not row:
            return None
        try:
            old = json.loads(row["additional_readings"])
            old_level = int(str(old.get(marker_key, "")).strip())
        except (ValueError, TypeError, KeyError):
            return None
        from datetime import datetime, timezone
        received = row["received_time"]
        if received.tzinfo is None:
            received = received.replace(tzinfo=timezone.utc)
        delta_days = (datetime.now(timezone.utc) - received).total_seconds() / 86400.0
        if delta_days < 1.0:
            return None
        consumed = old_level - current_level
        if consumed <= 0:
            return None
        rate_per_day = consumed / delta_days
        if rate_per_day <= 0:
            return None
        return min(999.0, max(0.0, current_level / rate_per_day))
    except Exception as exc:  # noqa: BLE001
        logger.debug("estimate_days_until_empty failed: %s", str(exc)[:200])
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _color_to_marker(color: str) -> Optional[str]:
    inv = {v: k for k, v in _MARKER_TO_COLOR.items()}
    return inv.get((color or "").upper())


# ---------------------------------------------------------------------------
# Severity classification (matches the alert-runner thresholds in P3)
# ---------------------------------------------------------------------------

def classify_severity(level: Optional[int], *, warn_pct: int,
                      critical_pct: int) -> str:
    """Return 'OK' | 'WARN' | 'CRITICAL' | 'UNKNOWN' for a supply level."""
    if level is None:
        return "UNKNOWN"
    if level <= critical_pct:
        return "CRITICAL"
    if level <= warn_pct:
        return "WARN"
    return "OK"


# ---------------------------------------------------------------------------
# Cache management (used by /toner refresh endpoint and by tests)
# ---------------------------------------------------------------------------

def invalidate_customer_cache(customer_id: int) -> None:
    """Drop cached readings for one customer — used after config change."""
    with _CACHE_LOCK:
        _ALL_SUPPLIES_CACHE.pop(int(customer_id), None)
        _ACTIVE_USERS_CACHE.pop(int(customer_id), None)
        _REGISTERED_USERS_CACHE.pop(int(customer_id), None)
        for k in list(_PRINTER_SUPPLIES_CACHE.keys()):
            if k[0] == int(customer_id):
                del _PRINTER_SUPPLIES_CACHE[k]


def cache_stats() -> dict:
    with _CACHE_LOCK:
        return {
            "printer_cache_size": len(_PRINTER_SUPPLIES_CACHE),
            "all_supplies_cache_size": len(_ALL_SUPPLIES_CACHE),
            "customers_cached": list(_ALL_SUPPLIES_CACHE.keys()),
        }
