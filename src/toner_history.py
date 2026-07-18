"""Toner-level history — v0.24.38.

Delta-based time series: a row is written to ``toner_readings`` only
when the level for a given (customer, printer, color) slot actually
changes from what was last recorded, or the slot is being seen for
the first time — not on every poll. Most poll ticks see no change
between two consecutive readings, so this keeps the table far smaller
than "one row per poll" while still capturing every real level
transition with a timestamp. Writing happens from
``toner_alerts._upsert_state``, which already knows the previous
value.

Raw rows age out after ``runner_config``'s
``toner_history_raw_retention_days`` (default 90, admin-editable in
Settings → Alert-Runner) into ``toner_readings_daily`` — one row per
(customer, printer, color, date) with avg/min/max/sample_count — so
long-run trend analysis stays cheap even after months of data. The
aggregation happens in Python rather than SQL date-truncation
functions on purpose: SQLite's ``SUBSTR`` and MSSQL's ``SUBSTRING``
aren't the same function, and this app runs on either backend.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, timedelta

from sqlalchemy import and_, delete, insert, select

from . import db

logger = logging.getLogger(__name__)


def record_reading(customer_id: int, printer_id: str, color: str,
                    level: int | None) -> None:
    """Append one row. Caller has already decided the level changed
    (or this is a brand-new slot) — this function doesn't re-check,
    it just writes."""
    if level is None:
        return
    with db.get_conn() as conn:
        conn.execute(insert(db.toner_readings).values(
            customer_id=customer_id, printer_id=printer_id,
            color=color, level=level,
        ))


def downsample_old_readings(retention_days: int) -> dict[str, int]:
    """Compact every raw reading older than ``retention_days`` into
    ``toner_readings_daily`` (avg/min/max/count per slot per day),
    then delete the raw rows that got compacted. If a daily row for
    the same (customer, printer, color, date) already exists, the
    new batch is merged into it (weighted average, min-of-mins,
    max-of-maxes, summed sample_count) rather than overwriting it —
    normal operation never revisits an already-compacted day since
    ``record_reading`` only ever stamps "now", but a merge keeps this
    safe even if a cycle is ever re-run against a day it already
    touched."""
    cutoff = (date.today() - timedelta(days=max(1, retention_days))).isoformat()
    with db.get_conn() as conn:
        rows = conn.execute(
            select(db.toner_readings.c.customer_id, db.toner_readings.c.printer_id,
                   db.toner_readings.c.color, db.toner_readings.c.level,
                   db.toner_readings.c.recorded_at)
            .where(db.toner_readings.c.recorded_at < cutoff)
        ).all()
        if not rows:
            return {"days_compacted": 0, "raw_rows_deleted": 0}

        buckets: dict[tuple[int, str, str, str], list[int]] = defaultdict(list)
        for r in rows:
            if r.level is None or not r.recorded_at:
                continue
            day = r.recorded_at[:10]
            buckets[(r.customer_id, r.printer_id, r.color, day)].append(r.level)

        for (customer_id, printer_id, color, day), levels in buckets.items():
            if not levels:
                continue
            new_sum = sum(levels)
            new_count = len(levels)
            new_min = min(levels)
            new_max = max(levels)

            where = and_(
                db.toner_readings_daily.c.customer_id == customer_id,
                db.toner_readings_daily.c.printer_id == printer_id,
                db.toner_readings_daily.c.color == color,
                db.toner_readings_daily.c.date == day,
            )
            existing = conn.execute(
                select(db.toner_readings_daily.c.avg_level,
                       db.toner_readings_daily.c.min_level,
                       db.toner_readings_daily.c.max_level,
                       db.toner_readings_daily.c.sample_count)
                .where(where)
            ).first()

            if existing:
                total_count = existing.sample_count + new_count
                total_sum = existing.avg_level * existing.sample_count + new_sum
                values = {
                    "avg_level": round(total_sum / total_count),
                    "min_level": min(existing.min_level, new_min),
                    "max_level": max(existing.max_level, new_max),
                    "sample_count": total_count,
                }
                conn.execute(db.toner_readings_daily.update().where(where).values(**values))
            else:
                conn.execute(insert(db.toner_readings_daily).values(
                    customer_id=customer_id, printer_id=printer_id,
                    color=color, date=day,
                    avg_level=round(new_sum / new_count),
                    min_level=new_min, max_level=new_max,
                    sample_count=new_count))

        deleted = conn.execute(
            delete(db.toner_readings).where(db.toner_readings.c.recorded_at < cutoff)
        ).rowcount

    return {"days_compacted": len(buckets), "raw_rows_deleted": deleted or 0}
