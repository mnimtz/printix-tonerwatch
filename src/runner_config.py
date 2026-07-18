"""Runner intervals — editable from Settings.

Two intervals live here:

* ``alert_interval_minutes`` — how often the toner-alert evaluator
  wakes up and checks every active customer's BI-DB for threshold
  crossings. Default 15 min.
* ``refresh_interval_minutes`` — how often the background BI-cache
  warmer runs so dashboards read from memory instead of blocking
  on a cold BI-DB query. Default 5 min.

Priority when reading: DB (persisted from the Settings form) →
env var (``ALERT_INTERVAL_MINUTES`` / ``REFRESH_INTERVAL_MINUTES``,
kept as boot-time override for the very first start) → default.

Writing the Settings form ONLY writes the DB row; env vars stay
untouched. Saving triggers a live reschedule of the running
APScheduler jobs — no restart required.
"""

from __future__ import annotations

import json
import os
from typing import Any

from sqlalchemy import func, insert, select, update

from . import db


SETTINGS_KEY = "runner"

DEFAULT_ALERT_MINUTES   = 15
DEFAULT_REFRESH_MINUTES = 5
# v0.24.38: how long raw toner-level readings (toner_readings) are
# kept before being compacted into daily averages
# (toner_readings_daily) — see toner_history.py. One quarter by
# default; admin-editable in Settings → Alert-Runner.
DEFAULT_TONER_HISTORY_RETENTION_DAYS = 90


def load_config() -> dict[str, Any]:
    """Return the current intervals + which source they came from
    (DB / env / default), so the UI can tell the operator whether an
    env var is overriding their saved value."""
    with db.get_conn() as conn:
        row = conn.execute(
            select(db.settings.c.value_json)
            .where(db.settings.c.key == SETTINGS_KEY)
        ).first()
    stored = json.loads(row[0]) if row else {}

    env_alert = _int_or_none(os.environ.get("ALERT_INTERVAL_MINUTES"))
    env_refresh = _int_or_none(os.environ.get("REFRESH_INTERVAL_MINUTES"))

    alert = stored.get("alert_interval_minutes")
    if alert is None or alert < 1:
        alert = env_alert if env_alert is not None else DEFAULT_ALERT_MINUTES
        alert_source = "env" if env_alert is not None else "default"
    else:
        alert_source = "db"

    refresh = stored.get("refresh_interval_minutes")
    if refresh is None or refresh < 1:
        refresh = (env_refresh if env_refresh is not None
                   else DEFAULT_REFRESH_MINUTES)
        refresh_source = "env" if env_refresh is not None else "default"
    else:
        refresh_source = "db"

    retention = stored.get("toner_history_raw_retention_days")
    if retention is None or retention < 1:
        retention = DEFAULT_TONER_HISTORY_RETENTION_DAYS

    return {
        "alert_interval_minutes":   int(alert),
        "refresh_interval_minutes": int(refresh),
        "toner_history_raw_retention_days": int(retention),
        "alert_source":             alert_source,
        "refresh_source":           refresh_source,
        # Expose env values so the UI can hint at what would be used
        # if the DB row were cleared.
        "env_alert_minutes":        env_alert,
        "env_refresh_minutes":      env_refresh,
    }


def save_config(alert_minutes: int, refresh_minutes: int,
                toner_history_retention_days: int | None = None) -> None:
    """Persist the intervals + trigger a live scheduler reschedule.
    ``toner_history_retention_days`` defaults to the current stored
    value (or the module default) when omitted, so existing callers
    that only pass the two original intervals keep working unchanged."""
    alert_minutes   = _clamp(alert_minutes,   1, 1440)
    refresh_minutes = _clamp(refresh_minutes, 1, 1440)
    if toner_history_retention_days is None:
        toner_history_retention_days = load_config()["toner_history_raw_retention_days"]
    toner_history_retention_days = _clamp(toner_history_retention_days, 7, 3650)
    payload = {
        "alert_interval_minutes":   alert_minutes,
        "refresh_interval_minutes": refresh_minutes,
        "toner_history_raw_retention_days": toner_history_retention_days,
    }
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

    # Late import to avoid circular reference at module load.
    from . import toner_alerts
    toner_alerts.reschedule_intervals(alert_minutes, refresh_minutes)


def _int_or_none(v: Any) -> int | None:
    try:
        i = int(v)
        return i if i > 0 else None
    except (TypeError, ValueError):
        return None


def _clamp(v: int, lo: int, hi: int) -> int:
    try:
        v = int(v)
    except (TypeError, ValueError):
        return lo
    return max(lo, min(hi, v))
