"""Printix BI database client (thin stub for P1).

P2 will grow this into a proper query layer that mirrors the queries
in the reference mysecureprint-server (device supplies, printer
inventory, network topology, etc.). For P1 we only need one thing:
the ability to open a connection to the customer's BI-DB with the
credentials they entered, and confirm that we got past the auth /
network handshake.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ConnectionResult:
    ok: bool
    message: str
    server_version: str = ""


# The Printix BI database is Microsoft SQL Server. We use pymssql
# rather than pyodbc — no ODBC driver install required, it's a pure
# ctypes wrapper over FreeTDS which is already in the runtime image.
def test_connection(server: str, database: str, username: str,
                    password: str, *, timeout: int = 5) -> ConnectionResult:
    """Attempt a `SELECT @@VERSION` against the customer BI-DB.

    Returns a :class:`ConnectionResult` with a short human-readable
    message. Any exception is caught — we never let a bad customer
    credential set take down the request handler.
    """
    if not server or not database or not username:
        return ConnectionResult(False,
                                "server, database and username are required")

    # Import lazily so a machine without pymssql installed (e.g. a
    # unit-test sandbox) can still import this module.
    try:
        import pymssql  # type: ignore
    except Exception as exc:  # pragma: no cover
        return ConnectionResult(False, f"pymssql not available: {exc}")

    conn: Any = None
    try:
        conn = pymssql.connect(
            server=server,
            user=username,
            password=password or "",
            database=database,
            timeout=timeout,
            login_timeout=timeout,
        )
        cur = conn.cursor()
        cur.execute("SELECT @@VERSION")
        version = (cur.fetchone() or ("",))[0]
        cur.close()
        # Keep only the first line and cap at 100 chars — @@VERSION is
        # a multi-line string with build metadata; the first line is
        # enough to prove we got a real handshake.
        first = version.splitlines()[0].strip() if version else ""
        return ConnectionResult(True, "Connection successful", first[:100])
    except Exception as exc:
        # pymssql surfaces LoginError, InterfaceError, OperationalError…
        # all of them subclass Exception; a short truncated message is
        # what the admin actually wants to see in the UI.
        msg = str(exc).strip().splitlines()[0][:200]
        return ConnectionResult(False, msg or exc.__class__.__name__)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
