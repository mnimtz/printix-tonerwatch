"""Database backend — SQLAlchemy Core over SQLite or Azure SQL.

Design
------
* Every table is declared once as a :class:`Table` object on the shared
  :data:`metadata`. No raw SQL strings in the codebase — the same
  Python queries run against both SQLite and Microsoft SQL Server
  (Azure SQL Database).

* Backend is chosen at start-up from the ``DATABASE_URL`` environment
  variable:

  - ``sqlite:///data/tonerwatch.sqlite`` — default, no external
    dependency, everything lives in the mounted ``/data`` volume.
  - ``mssql+pymssql://user:pass@server:1433/dbname`` — Azure SQL
    Database (or any MSSQL). Fernet key stays in ``/data`` even in
    this mode — encryption is deliberately not co-located with the
    encrypted data.

  When ``DATABASE_URL`` is unset we fall back to
  ``sqlite:///${DB_PATH}`` where ``DB_PATH`` defaults to
  ``/data/tonerwatch.sqlite``. Existing deployments keep working with
  zero configuration.

* Schema management goes through Alembic. On first start the runtime
  detects an empty database, calls ``metadata.create_all`` and stamps
  the Alembic head revision so subsequent upgrades apply cleanly. On
  every start after that, ``alembic upgrade head`` runs pending
  migrations transactionally.

* Callers use :func:`get_conn` (a context-managed
  :class:`sqlalchemy.engine.Connection` inside a transaction) or the
  narrow helper functions at the bottom of the file. Helper functions
  return ``dict`` objects so the auth / template layer doesn't need
  to care whether the row came from SQLAlchemy, sqlite3 or something
  else.
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping

from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Text,
    UniqueConstraint,
    delete,
    event,
    func,
    inspect,
    insert,
    select,
    update,
)
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.engine.url import make_url


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

_INIT_LOCK = threading.Lock()
_INITIALISED = False
_ENGINE: Engine | None = None


def database_url() -> str:
    """Resolve the SQLAlchemy connection URL from the environment."""
    explicit = os.environ.get("DATABASE_URL", "").strip()
    if explicit:
        return explicit
    db_path = os.environ.get("DB_PATH", "/data/tonerwatch.sqlite")
    return f"sqlite:///{db_path}"


def _is_sqlite(url: str) -> bool:
    return make_url(url).get_backend_name() == "sqlite"


def get_engine() -> Engine:
    """Return the process-wide SQLAlchemy Engine, creating it lazily."""
    global _ENGINE
    if _ENGINE is not None:
        return _ENGINE

    url = database_url()

    # SQLite needs the parent directory to exist.
    if _is_sqlite(url):
        raw = make_url(url).database or ""
        if raw:
            Path(raw).parent.mkdir(parents=True, exist_ok=True)

    # `future=True` is the SQLAlchemy 2.x behaviour (default in 2.0+).
    # `pool_pre_ping=True` keeps long-lived MSSQL connections healthy
    # across App Service idle timeouts.
    _ENGINE = _create_engine(url)

    if _is_sqlite(url):
        @event.listens_for(_ENGINE, "connect")
        def _sqlite_pragmas(dbapi_connection, _connection_record):  # pragma: no cover
            cur = dbapi_connection.cursor()
            cur.execute("PRAGMA journal_mode = WAL")
            cur.execute("PRAGMA synchronous  = NORMAL")
            cur.execute("PRAGMA foreign_keys = ON")
            cur.execute("PRAGMA busy_timeout = 5000")
            cur.close()

    return _ENGINE


def _create_engine(url: str) -> Engine:
    """Wrapper so :func:`get_engine` can be unit-tested without env I/O."""
    from sqlalchemy import create_engine
    kwargs: dict[str, Any] = {"pool_pre_ping": True, "pool_recycle": 1800}
    if _is_sqlite(url):
        # SQLite: allow cross-thread use (FastAPI runs handlers on a
        # threadpool for sync code paths). We use short-lived
        # connections per request, so cross-thread reuse is safe.
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    return create_engine(url, **kwargs)


@contextmanager
def get_conn() -> Iterator[Connection]:
    """Yield a Connection inside a transaction — commits on clean exit,
    rolls back on exception. Do not hold across HTTP requests.
    """
    with get_engine().begin() as conn:
        yield conn


# ---------------------------------------------------------------------------
# Schema (single source of truth — Alembic env.py imports `metadata`)
# ---------------------------------------------------------------------------

metadata = MetaData()

users = Table(
    "users", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("email", Text, nullable=False, unique=True),
    Column("password_hash", Text, nullable=False, server_default=""),
    Column("name", Text, nullable=False, server_default=""),
    Column("role", Text, nullable=False, server_default="technician"),
    Column("entra_oid", Text, nullable=False, server_default=""),
    Column("lang", Text, nullable=False, server_default=""),
    Column("active", Integer, nullable=False, server_default="1"),
    Column("created_at", Text, nullable=False, server_default=func.current_timestamp()),
    Column("last_login_at", Text, nullable=False, server_default=""),
    CheckConstraint("role IN ('admin','technician')", name="ck_users_role"),
)
Index("idx_users_entra_oid", users.c.entra_oid,
      sqlite_where=users.c.entra_oid != "",
      mssql_where=users.c.entra_oid != "")

customers = Table(
    "customers", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("name", Text, nullable=False),
    Column("tenant_url", Text, nullable=False, server_default=""),
    Column("notes", Text, nullable=False, server_default=""),

    # Printix BI database (Azure SQL) — password Fernet-encrypted at rest
    Column("sql_server", Text, nullable=False, server_default=""),
    Column("sql_database", Text, nullable=False, server_default=""),
    Column("sql_port", Integer, nullable=False, server_default="1433"),
    Column("sql_username", Text, nullable=False, server_default=""),
    Column("sql_password_enc", Text, nullable=False, server_default=""),

    # Alerting configuration
    Column("alert_recipients_csv", Text, nullable=False, server_default=""),
    Column("alert_min_level", Text, nullable=False, server_default="WARN"),
    Column("order_recipients_csv", Text, nullable=False, server_default=""),
    Column("warn_pct", Integer, nullable=False, server_default="20"),
    Column("critical_pct", Integer, nullable=False, server_default="5"),
    Column("timezone", Text, nullable=False, server_default="Europe/Berlin"),
    Column("quiet_hours_start", Text, nullable=False, server_default=""),
    Column("quiet_hours_end", Text, nullable=False, server_default=""),
    Column("digest_mode", Integer, nullable=False, server_default="0"),
    Column("auto_order_mode", Text, nullable=False, server_default="off"),
    # v0.20.0: autonomous mode transitions drafts straight to "ordered"
    # and emails the supplier. Cap protects against a runaway alert
    # storm turning into a runaway P.O. spam.
    Column("auto_order_daily_cap", Integer, nullable=False, server_default="10"),

    Column("active", Integer, nullable=False, server_default="1"),
    Column("created_at", Text, nullable=False, server_default=func.current_timestamp()),
    Column("created_by_user_id", Integer,
           ForeignKey("users.id", ondelete="SET NULL")),
    Column("updated_at", Text, nullable=False, server_default=func.current_timestamp()),

    CheckConstraint("alert_min_level IN ('INFO','WARN','CRITICAL')",
                    name="ck_customers_alert_min_level"),
    CheckConstraint("auto_order_mode IN ('off','draft','autonomous')",
                    name="ck_customers_auto_order_mode"),
)
Index("idx_customers_active", customers.c.active)

customer_access = Table(
    "customer_access", metadata,
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"),
           nullable=False),
    Column("customer_id", Integer,
           ForeignKey("customers.id", ondelete="CASCADE"), nullable=False),
    Column("access_level", Text, nullable=False, server_default="read"),
    Column("granted_at", Text, nullable=False,
           server_default=func.current_timestamp()),
    Column("granted_by", Integer, ForeignKey("users.id", ondelete="SET NULL")),
    PrimaryKeyConstraint("user_id", "customer_id"),
    CheckConstraint("access_level IN ('read','admin')",
                    name="ck_customer_access_level"),
)
Index("idx_customer_access_customer", customer_access.c.customer_id)

supply_templates = Table(
    "supply_templates", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("printer_model", Text, nullable=False),
    Column("color", Text, nullable=False),
    Column("sku", Text, nullable=False, server_default=""),
    Column("description", Text, nullable=False, server_default=""),
    Column("manufacturer", Text, nullable=False, server_default=""),
    Column("supplier", Text, nullable=False, server_default=""),
    Column("supplier_url", Text, nullable=False, server_default=""),
    Column("default_quantity", Integer, nullable=False, server_default="1"),
    Column("unit_price_cents", Integer),
    Column("yield_pages", Integer),
    Column("notes", Text, nullable=False, server_default=""),
    Column("is_shared", Integer, nullable=False, server_default="1"),
    Column("updated_at", Text, nullable=False,
           server_default=func.current_timestamp()),
    Column("updated_by_user_id", Integer,
           ForeignKey("users.id", ondelete="SET NULL")),
    UniqueConstraint("printer_model", "color",
                     name="uq_supply_templates_model_color"),
    CheckConstraint("color IN ('K','C','M','Y','other')",
                    name="ck_supply_templates_color"),
)

printer_info = Table(
    "printer_info", metadata,
    Column("customer_id", Integer,
           ForeignKey("customers.id", ondelete="CASCADE"), nullable=False),
    Column("printer_id", Text, nullable=False),
    # Per-device overrides — non-empty wins over the value from Printix BI.
    # Empty means "use whatever BI says".
    Column("location_override", Text, nullable=False, server_default=""),
    Column("serial_override",   Text, nullable=False, server_default=""),
    Column("asset_tag",         Text, nullable=False, server_default=""),
    Column("group_name",        Text, nullable=False, server_default=""),
    Column("contact_email",     Text, nullable=False, server_default=""),
    Column("purchased_at",      Text, nullable=False, server_default=""),
    Column("warranty_until",    Text, nullable=False, server_default=""),
    Column("notes",             Text, nullable=False, server_default=""),
    Column("updated_at",        Text, nullable=False,
           server_default=func.current_timestamp()),
    Column("updated_by_user_id", Integer,
           ForeignKey("users.id", ondelete="SET NULL")),
    PrimaryKeyConstraint("customer_id", "printer_id",
                         name="pk_printer_info"),
)
Index("idx_printer_info_group",
      printer_info.c.customer_id, printer_info.c.group_name)


printer_supplies = Table(
    "printer_supplies", metadata,
    Column("customer_id", Integer,
           ForeignKey("customers.id", ondelete="CASCADE"), nullable=False),
    Column("printer_id", Text, nullable=False),
    Column("color", Text, nullable=False),
    Column("sku", Text, nullable=False, server_default=""),
    Column("description", Text, nullable=False, server_default=""),
    Column("manufacturer", Text, nullable=False, server_default=""),
    Column("supplier", Text, nullable=False, server_default=""),
    Column("supplier_url", Text, nullable=False, server_default=""),
    Column("default_quantity", Integer, nullable=False, server_default="1"),
    Column("unit_price_cents", Integer),
    Column("notes", Text, nullable=False, server_default=""),
    Column("updated_at", Text, nullable=False,
           server_default=func.current_timestamp()),
    Column("updated_by_user_id", Integer,
           ForeignKey("users.id", ondelete="SET NULL")),
    PrimaryKeyConstraint("customer_id", "printer_id", "color"),
    CheckConstraint("color IN ('K','C','M','Y','other')",
                    name="ck_printer_supplies_color"),
)

toner_state = Table(
    "toner_state", metadata,
    Column("customer_id", Integer,
           ForeignKey("customers.id", ondelete="CASCADE"), nullable=False),
    Column("printer_id", Text, nullable=False),
    Column("color", Text, nullable=False),
    Column("level", Integer),
    Column("severity", Text, nullable=False, server_default="OK"),
    Column("last_notified_at", Text, nullable=False, server_default=""),
    Column("last_notified_sev", Text, nullable=False, server_default=""),
    Column("last_seen_at", Text, nullable=False, server_default=""),
    PrimaryKeyConstraint("customer_id", "printer_id", "color"),
    CheckConstraint("severity IN ('OK','WARN','CRITICAL','UNKNOWN')",
                    name="ck_toner_state_severity"),
)

toner_events = Table(
    "toner_events", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("customer_id", Integer,
           ForeignKey("customers.id", ondelete="CASCADE"), nullable=False),
    Column("kind", Text, nullable=False),
    Column("printer_id", Text, nullable=False, server_default=""),
    Column("color", Text, nullable=False, server_default=""),
    Column("level", Integer),
    Column("severity", Text, nullable=False, server_default=""),
    Column("created_at", Text, nullable=False,
           server_default=func.current_timestamp()),
    Column("meta_json", Text, nullable=False, server_default="{}"),
)
Index("idx_toner_events_customer_time",
      toner_events.c.customer_id, toner_events.c.created_at.desc())

toner_orders = Table(
    "toner_orders", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("customer_id", Integer,
           ForeignKey("customers.id", ondelete="CASCADE"), nullable=False),
    Column("printer_id", Text, nullable=False),
    Column("printer_name", Text, nullable=False, server_default=""),
    Column("color", Text, nullable=False),
    Column("sku", Text, nullable=False, server_default=""),
    Column("quantity", Integer, nullable=False, server_default="1"),
    Column("status", Text, nullable=False, server_default="ordered"),
    Column("ordered_at", Text, nullable=False,
           server_default=func.current_timestamp()),
    Column("closed_at", Text, nullable=False, server_default=""),
    Column("closed_reason", Text, nullable=False, server_default=""),
    Column("ordered_by_user_id", Integer,
           ForeignKey("users.id", ondelete="SET NULL")),
    Column("notes", Text, nullable=False, server_default=""),
    CheckConstraint(
        "status IN ('draft','ordered','delivered','installed','cancelled')",
        name="ck_toner_orders_status"),
)
# Only one active order per (customer, printer, color) at a time.
# Both SQLite and MSSQL support filtered / partial indices, but the
# `where` clause has to be spelled per-dialect for Alembic to generate
# the correct DDL on each side.
_active_status_filter = toner_orders.c.status.in_(
    ("draft", "ordered", "delivered"))
Index(
    "uq_active_toner_order",
    toner_orders.c.customer_id, toner_orders.c.printer_id, toner_orders.c.color,
    unique=True,
    sqlite_where=_active_status_filter,
    mssql_where=_active_status_filter,
)
Index("idx_toner_orders_status", toner_orders.c.status)

saved_views = Table(
    "saved_views", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"),
           nullable=False),
    Column("name", Text, nullable=False),
    Column("scope", Text, nullable=False),
    Column("filters_json", Text, nullable=False, server_default="{}"),
    Column("is_shared", Integer, nullable=False, server_default="0"),
    Column("created_at", Text, nullable=False,
           server_default=func.current_timestamp()),
    CheckConstraint("scope IN ('toner','orders','printers','customers')",
                    name="ck_saved_views_scope"),
)
Index("idx_saved_views_user_scope", saved_views.c.user_id, saved_views.c.scope)

audit_log = Table(
    "audit_log", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", Integer, ForeignKey("users.id", ondelete="SET NULL")),
    Column("action", Text, nullable=False),
    Column("target_type", Text, nullable=False, server_default=""),
    Column("target_id", Text, nullable=False, server_default=""),
    Column("meta_json", Text, nullable=False, server_default="{}"),
    Column("created_at", Text, nullable=False,
           server_default=func.current_timestamp()),
)
Index("idx_audit_created", audit_log.c.created_at.desc())

settings = Table(
    "settings", metadata,
    Column("key", Text, primary_key=True),
    Column("value_json", Text, nullable=False, server_default="{}"),
    Column("updated_at", Text, nullable=False,
           server_default=func.current_timestamp()),
)


# ---------------------------------------------------------------------------
# Schema init + migrations (Alembic)
# ---------------------------------------------------------------------------

def _alembic_config():
    from alembic.config import Config
    root = Path(__file__).parent.parent
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "alembic"))
    cfg.set_main_option("sqlalchemy.url", database_url())
    return cfg


def init_schema() -> None:
    """Create the schema on first boot, or apply pending migrations."""
    global _INITIALISED
    with _INIT_LOCK:
        if _INITIALISED:
            return

        engine = get_engine()
        insp = inspect(engine)
        fresh = not insp.has_table("users") and not insp.has_table("alembic_version")

        if fresh:
            # Fresh database — create everything from the current metadata
            # in one shot, then stamp the Alembic head so future upgrades
            # know we're already at the latest revision.
            metadata.create_all(engine)
            from alembic import command
            command.stamp(_alembic_config(), "head")
        else:
            # Existing database — apply any pending migrations.
            from alembic import command
            command.upgrade(_alembic_config(), "head")

        _INITIALISED = True


# ---------------------------------------------------------------------------
# Convenience helpers (used from more than one caller)
# ---------------------------------------------------------------------------

def _row_to_dict(row: Any | None) -> dict | None:
    """SQLAlchemy Row → plain dict, or None."""
    if row is None:
        return None
    m: Mapping = row._mapping if hasattr(row, "_mapping") else row
    return dict(m)


def user_count() -> int:
    with get_conn() as conn:
        return int(conn.execute(select(func.count()).select_from(users)).scalar_one())


def find_user_by_email(email: str) -> dict | None:
    with get_conn() as conn:
        # SQLite COLLATE NOCASE and MSSQL default case-insensitivity differ;
        # use func.lower() for a portable case-insensitive match.
        row = conn.execute(
            select(users).where(func.lower(users.c.email) == email.lower())
        ).first()
    return _row_to_dict(row)


def find_user_by_id(user_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(select(users).where(users.c.id == user_id)).first()
    return _row_to_dict(row)


def touch_last_login(user_id: int) -> None:
    with get_conn() as conn:
        conn.execute(
            update(users).where(users.c.id == user_id)
            .values(last_login_at=func.current_timestamp())
        )


def audit(user_id: int | None, action: str, *, target_type: str = "",
          target_id: str = "", meta_json: str = "{}") -> None:
    with get_conn() as conn:
        conn.execute(insert(audit_log).values(
            user_id=user_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            meta_json=meta_json,
        ))
