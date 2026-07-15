"""SQLite backend — schema, migrations, connection factory.

Design notes
------------
* One SQLite file for the entire deployment, mounted from ``/data``.
* WAL journaling so the alert runner never blocks HTTP handlers.
* ``sqlite3.Row`` row factory. Reminder: ``sqlite3.Row`` has **no**
  ``.get()`` method — always use ``row["column"]`` or wrap with
  ``dict(row).get(...)``.
* All schema for every planned phase is created up-front by
  :func:`init_schema`. Later phases add rows and code paths, not new
  tables. This keeps deployment "restart on new tag → done".
* Only additive migrations are supported via :data:`MIGRATIONS`.
  Never rename/drop columns in-place; add new ones and back-fill.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = 1

# Serialise schema init across threads — SQLite tolerates concurrent readers
# but not two concurrent DDL statements racing at first-boot.
_INIT_LOCK = threading.Lock()
_INITIALISED = False


def db_path() -> str:
    return os.environ.get("DB_PATH", "/data/tonerwatch.sqlite")


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30, isolation_level=None,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    """Open a short-lived connection. Callers must not hold across requests."""
    conn = _connect(db_path())
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ── users ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS users (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    email          TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    password_hash  TEXT    NOT NULL DEFAULT '',
    name           TEXT    NOT NULL DEFAULT '',
    role           TEXT    NOT NULL DEFAULT 'technician'
                             CHECK (role IN ('admin', 'technician')),
    entra_oid      TEXT    NOT NULL DEFAULT '',
    lang           TEXT    NOT NULL DEFAULT '',
    active         INTEGER NOT NULL DEFAULT 1,
    created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    last_login_at  TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_users_entra_oid ON users(entra_oid)
    WHERE entra_oid <> '';

-- ── customers (Printix tenants under management) ────────────────────────
CREATE TABLE IF NOT EXISTS customers (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    name                   TEXT    NOT NULL,
    tenant_url             TEXT    NOT NULL DEFAULT '',
    notes                  TEXT    NOT NULL DEFAULT '',

    -- Printix BI database (Azure SQL) — password Fernet-encrypted at rest
    sql_server             TEXT    NOT NULL DEFAULT '',
    sql_database           TEXT    NOT NULL DEFAULT '',
    sql_username           TEXT    NOT NULL DEFAULT '',
    sql_password_enc       TEXT    NOT NULL DEFAULT '',

    -- Alerting configuration
    alert_recipients_csv   TEXT    NOT NULL DEFAULT '',
    alert_min_level        TEXT    NOT NULL DEFAULT 'WARN'
                             CHECK (alert_min_level IN ('INFO','WARN','CRITICAL')),
    order_recipients_csv   TEXT    NOT NULL DEFAULT '',
    warn_pct               INTEGER NOT NULL DEFAULT 20,
    critical_pct           INTEGER NOT NULL DEFAULT 5,
    timezone               TEXT    NOT NULL DEFAULT 'Europe/Berlin',
    quiet_hours_start      TEXT    NOT NULL DEFAULT '',  -- 'HH:MM' or ''
    quiet_hours_end        TEXT    NOT NULL DEFAULT '',
    digest_mode            INTEGER NOT NULL DEFAULT 0,
    auto_order_mode        TEXT    NOT NULL DEFAULT 'off'
                             CHECK (auto_order_mode IN ('off','draft')),

    active                 INTEGER NOT NULL DEFAULT 1,
    created_at             TEXT    NOT NULL DEFAULT (datetime('now')),
    created_by_user_id     INTEGER REFERENCES users(id) ON DELETE SET NULL,
    updated_at             TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_customers_active ON customers(active);

-- ── customer_access (M:N — technicians ↔ customers) ─────────────────────
CREATE TABLE IF NOT EXISTS customer_access (
    user_id       INTEGER NOT NULL REFERENCES users(id)     ON DELETE CASCADE,
    customer_id   INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    access_level  TEXT    NOT NULL DEFAULT 'read'
                    CHECK (access_level IN ('read','admin')),
    granted_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    granted_by    INTEGER REFERENCES users(id) ON DELETE SET NULL,
    PRIMARY KEY (user_id, customer_id)
);
CREATE INDEX IF NOT EXISTS idx_customer_access_customer
    ON customer_access(customer_id);

-- ── supply_templates (per printer-model catalog entry) ──────────────────
CREATE TABLE IF NOT EXISTS supply_templates (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    printer_model     TEXT    NOT NULL COLLATE NOCASE,
    color             TEXT    NOT NULL
                        CHECK (color IN ('K','C','M','Y','other')),
    sku               TEXT    NOT NULL DEFAULT '',
    description       TEXT    NOT NULL DEFAULT '',
    manufacturer      TEXT    NOT NULL DEFAULT '',
    supplier          TEXT    NOT NULL DEFAULT '',
    supplier_url      TEXT    NOT NULL DEFAULT '',
    default_quantity  INTEGER NOT NULL DEFAULT 1,
    unit_price_cents  INTEGER,
    yield_pages       INTEGER,
    notes             TEXT    NOT NULL DEFAULT '',
    is_shared         INTEGER NOT NULL DEFAULT 1,
    updated_at        TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    UNIQUE (printer_model, color)
);

-- ── printer_supplies (per-device override on top of the template) ──────
CREATE TABLE IF NOT EXISTS printer_supplies (
    customer_id       INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    printer_id        TEXT    NOT NULL,
    color             TEXT    NOT NULL
                        CHECK (color IN ('K','C','M','Y','other')),
    sku               TEXT    NOT NULL DEFAULT '',
    description       TEXT    NOT NULL DEFAULT '',
    manufacturer      TEXT    NOT NULL DEFAULT '',
    supplier          TEXT    NOT NULL DEFAULT '',
    supplier_url      TEXT    NOT NULL DEFAULT '',
    default_quantity  INTEGER NOT NULL DEFAULT 1,
    unit_price_cents  INTEGER,
    notes             TEXT    NOT NULL DEFAULT '',
    updated_at        TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    PRIMARY KEY (customer_id, printer_id, color)
);

-- ── toner_state (latest reading + alert bookkeeping per marker) ────────
CREATE TABLE IF NOT EXISTS toner_state (
    customer_id       INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    printer_id        TEXT    NOT NULL,
    color             TEXT    NOT NULL,
    level             INTEGER,
    severity          TEXT    NOT NULL DEFAULT 'OK'
                        CHECK (severity IN ('OK','WARN','CRITICAL','UNKNOWN')),
    last_notified_at  TEXT    NOT NULL DEFAULT '',
    last_notified_sev TEXT    NOT NULL DEFAULT '',
    last_seen_at      TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (customer_id, printer_id, color)
);

-- ── toner_events (append-only log for auditing + reporting) ────────────
CREATE TABLE IF NOT EXISTS toner_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id  INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    kind         TEXT    NOT NULL,
    printer_id   TEXT    NOT NULL DEFAULT '',
    color        TEXT    NOT NULL DEFAULT '',
    level        INTEGER,
    severity     TEXT    NOT NULL DEFAULT '',
    created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    meta_json    TEXT    NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_toner_events_customer_time
    ON toner_events(customer_id, created_at DESC);

-- ── toner_orders (cross-customer order pipeline) ───────────────────────
CREATE TABLE IF NOT EXISTS toner_orders (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id       INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    printer_id        TEXT    NOT NULL,
    printer_name      TEXT    NOT NULL DEFAULT '',
    color             TEXT    NOT NULL,
    sku               TEXT    NOT NULL DEFAULT '',
    quantity          INTEGER NOT NULL DEFAULT 1,
    status            TEXT    NOT NULL DEFAULT 'ordered'
                        CHECK (status IN
                              ('draft','ordered','delivered','installed','cancelled')),
    ordered_at        TEXT    NOT NULL DEFAULT (datetime('now')),
    closed_at         TEXT    NOT NULL DEFAULT '',
    closed_reason     TEXT    NOT NULL DEFAULT '',
    ordered_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    notes             TEXT    NOT NULL DEFAULT ''
);
-- Only one active order per (customer, printer, color) at a time.
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_order
    ON toner_orders(customer_id, printer_id, color)
    WHERE status IN ('draft','ordered','delivered');
CREATE INDEX IF NOT EXISTS idx_orders_status ON toner_orders(status);

-- ── saved_views (persistent per-user filter presets) ───────────────────
CREATE TABLE IF NOT EXISTS saved_views (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name         TEXT    NOT NULL,
    scope        TEXT    NOT NULL
                   CHECK (scope IN ('toner','orders','printers','customers')),
    filters_json TEXT    NOT NULL DEFAULT '{}',
    is_shared    INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_saved_views_user_scope
    ON saved_views(user_id, scope);

-- ── audit_log (every state-changing operation) ─────────────────────────
CREATE TABLE IF NOT EXISTS audit_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER REFERENCES users(id) ON DELETE SET NULL,
    action       TEXT    NOT NULL,
    target_type  TEXT    NOT NULL DEFAULT '',
    target_id    TEXT    NOT NULL DEFAULT '',
    meta_json    TEXT    NOT NULL DEFAULT '{}',
    created_at   TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at DESC);

-- ── settings (instance-wide key-value JSON store) ──────────────────────
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


# Additive migrations. Each entry is (version_after, sql_or_callable).
# Never mutate existing entries; only append new ones.
MIGRATIONS: list[tuple[int, str]] = [
    # (2, "ALTER TABLE customers ADD COLUMN foo TEXT NOT NULL DEFAULT ''"),
]


def _current_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT value FROM schema_meta WHERE key = 'schema_version'"
    ).fetchone()
    return int(row["value"]) if row else 0


def _set_version(conn: sqlite3.Connection, version: int) -> None:
    conn.execute(
        "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(version),),
    )


def init_schema() -> None:
    """Create the schema on first boot; apply pending additive migrations."""
    global _INITIALISED
    with _INIT_LOCK:
        if _INITIALISED:
            return
        Path(db_path()).parent.mkdir(parents=True, exist_ok=True)
        with get_conn() as conn:
            conn.executescript(SCHEMA_SQL)
            version = _current_version(conn)
            if version == 0:
                _set_version(conn, SCHEMA_VERSION)
                version = SCHEMA_VERSION
            for target, ddl in MIGRATIONS:
                if version < target:
                    conn.executescript(ddl if isinstance(ddl, str) else ddl())
                    _set_version(conn, target)
                    version = target
        _INITIALISED = True


# ---------------------------------------------------------------------------
# Convenience queries used by more than one module
# ---------------------------------------------------------------------------

def user_count() -> int:
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]


def find_user_by_email(email: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE email = ? COLLATE NOCASE",
            (email,),
        ).fetchone()


def find_user_by_id(user_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ).fetchone()


def touch_last_login(user_id: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET last_login_at = datetime('now') WHERE id = ?",
            (user_id,),
        )


def audit(user_id: int | None, action: str, *, target_type: str = "",
          target_id: str = "", meta_json: str = "{}") -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO audit_log(user_id, action, target_type, target_id, "
            "meta_json) VALUES (?,?,?,?,?)",
            (user_id, action, target_type, target_id, meta_json),
        )
