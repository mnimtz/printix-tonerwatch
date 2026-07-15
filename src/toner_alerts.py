"""Toner alert runner — evaluate BI-DB snapshots, notify on threshold crossings.

Runs on APScheduler every N minutes (default 15). For each active customer
with BI credentials configured:

1. ``bi_client.fetch_all_printer_supplies(customer)`` — cache-first
2. For each (printer, color) fill level, classify severity vs the
   customer's warn / critical thresholds
3. Compare against ``toner_state`` (last known severity) — only notify on
   NEW crossings or on recovery
4. Respect quiet hours (customer.quiet_hours_start / _end)
5. Digest mode groups per-customer transitions into one daily mail

Every notification is written to ``toner_events`` for the dashboard's
recent-activity feed.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
from typing import Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, insert, select, update

from . import bi_client, db, mail_client, orders, supply_library


logger = logging.getLogger(__name__)


SEVERITY_RANK = {"OK": 0, "UNKNOWN": 0, "WARN": 1, "CRITICAL": 2}
COLOR_LABEL = {"K": "Black", "C": "Cyan", "M": "Magenta", "Y": "Yellow"}


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def _load_state_for_customer(customer_id: int) -> dict[tuple[str, str], dict]:
    with db.get_conn() as conn:
        rows = conn.execute(
            select(db.toner_state).where(
                db.toner_state.c.customer_id == customer_id)
        ).all()
    return {(r.printer_id, r.color): db._row_to_dict(r) for r in rows}


def _upsert_state(customer_id: int, printer_id: str, color: str,
                  level: int | None, severity: str,
                  notified: bool, notified_severity: str = "") -> None:
    with db.get_conn() as conn:
        exists = conn.execute(
            select(db.toner_state.c.customer_id).where(
                (db.toner_state.c.customer_id == customer_id) &
                (db.toner_state.c.printer_id == printer_id) &
                (db.toner_state.c.color == color)
            )
        ).first()
        values = {
            "level": level, "severity": severity,
            "last_seen_at": _now_iso(),
        }
        if notified:
            values["last_notified_at"] = _now_iso()
            values["last_notified_sev"] = notified_severity or severity
        if exists is None:
            conn.execute(insert(db.toner_state).values(
                customer_id=customer_id, printer_id=printer_id,
                color=color, **values,
            ))
        else:
            conn.execute(update(db.toner_state).where(
                (db.toner_state.c.customer_id == customer_id) &
                (db.toner_state.c.printer_id == printer_id) &
                (db.toner_state.c.color == color)
            ).values(**values))


def _log_event(customer_id: int, kind: str, *, printer_id: str = "",
               color: str = "", level: int | None = None,
               severity: str = "", meta: dict | None = None) -> None:
    with db.get_conn() as conn:
        conn.execute(insert(db.toner_events).values(
            customer_id=customer_id, kind=kind,
            printer_id=printer_id, color=color, level=level,
            severity=severity, meta_json=json.dumps(meta or {}),
        ))


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _in_quiet_hours(customer: dict) -> bool:
    """True if the current local time (customer.timezone) is inside the
    quiet-hours window. Empty start/end disables the check."""
    start = customer.get("quiet_hours_start") or ""
    end   = customer.get("quiet_hours_end") or ""
    if not (start and end and ":" in start and ":" in end):
        return False
    try:
        tz = ZoneInfo(customer.get("timezone") or "UTC")
    except ZoneInfoNotFoundError:
        tz = _dt.timezone.utc
    now = _dt.datetime.now(tz).time()
    try:
        s = _dt.time(*map(int, start.split(":")[:2]))
        e = _dt.time(*map(int, end.split(":")[:2]))
    except ValueError:
        return False
    if s <= e:
        return s <= now < e
    return now >= s or now < e   # window wraps midnight


# ---------------------------------------------------------------------------
# Main evaluation
# ---------------------------------------------------------------------------

def evaluate_and_notify(customer: dict, *,
                        force_send: bool = False,
                        force_refresh: bool = False) -> dict:
    """Fetch latest supplies, compare with state, send notification if needed.

    Returns a small summary dict: how many new crossings, whether a mail
    was sent, and any error string. Callers (runner, admin "Test mail")
    use the return value to render an audit-log entry.
    """
    result = {"customer_id": customer["id"], "customer_name": customer["name"],
              "checked": 0, "transitions": 0, "sent": False, "error": ""}

    if not customer.get("active"):
        return result

    bi = bi_client.customer_for_bi(customer)
    if not (bi.get("sql_server") and bi.get("sql_database")
            and bi.get("sql_username")):
        return result

    # Cache invalidation on force_refresh so we go all the way to BI
    if force_refresh:
        bi_client.invalidate_customer_cache(customer["id"])
    snapshots = bi_client.fetch_all_printer_supplies(bi)
    if snapshots is None:
        result["error"] = "fetch_failed"
        return result

    warn = int(customer.get("warn_pct") or 20)
    crit = int(customer.get("critical_pct") or 5)
    min_level = (customer.get("alert_min_level") or "WARN").upper()
    min_rank = SEVERITY_RANK.get(min_level, 1)
    prior_state = _load_state_for_customer(customer["id"])

    transitions_up: list[dict] = []
    transitions_recover: list[dict] = []

    for p in snapshots:
        for supply in p["supplies"]:
            result["checked"] += 1
            level = supply["level"]
            color = supply["color"]
            severity = bi_client.classify_severity(
                level, warn_pct=warn, critical_pct=crit)
            key = (p["printer_id"], color)
            prev = prior_state.get(key, {})
            prev_sev = (prev.get("severity") or "OK").upper()

            row = {
                "printer_id":   p["printer_id"],
                "printer_name": p["printer_name"] or "",
                "printer_model": p.get("printer_model") or "",
                "location":     p["location"] or "",
                "color":        color, "color_label": COLOR_LABEL.get(color, color),
                "level":        level, "severity":    severity,
                "prev_severity": prev_sev,
            }

            if (SEVERITY_RANK[severity] > SEVERITY_RANK[prev_sev]
                    and SEVERITY_RANK[severity] >= min_rank):
                transitions_up.append(row)
            elif (SEVERITY_RANK[severity] < SEVERITY_RANK[prev_sev]
                    and SEVERITY_RANK[prev_sev] >= min_rank):
                transitions_recover.append(row)

            # Always keep state fresh (level might change without crossing)
            _upsert_state(customer["id"], p["printer_id"], color, level,
                          severity, notified=False)

    result["transitions"] = len(transitions_up) + len(transitions_recover)
    if not transitions_up and not transitions_recover:
        return result

    # Auto-draft: for each critical/warn transition, resolve the supply
    # record and either attach an existing active order or create a new
    # draft. The mail then carries the SKU + a one-click "Mark as
    # ordered" magic link per alert row.
    for t in transitions_up:
        supply = supply_library.resolve_supply(
            customer["id"], t["printer_id"],
            t["printer_model"], t["color"])
        t["supply"] = supply
        sku = (supply or {}).get("sku", "")
        qty = int((supply or {}).get("default_quantity") or 1)
        order_id, _created = orders.create_draft_if_none(
            customer["id"], t["printer_id"],
            t["printer_name"], t["color"],
            sku=sku, quantity=qty)
        t["order_id"] = order_id
        t["order_token"] = orders.sign_action_token(order_id, "ordered")

    if _in_quiet_hours(customer) and not force_send:
        _log_event(customer["id"], "alert.quiet_hours_skipped",
                   meta={"transitions": result["transitions"]})
        return result

    recipients = [r.strip() for r in
                  (customer.get("alert_recipients_csv") or "").split(",") if r.strip()]
    if not recipients:
        _log_event(customer["id"], "alert.no_recipients",
                   meta={"transitions": result["transitions"]})
        return result

    subject = _compose_subject(customer, transitions_up, transitions_recover)
    public_base_url = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    html, text = _compose_body(customer, transitions_up, transitions_recover,
                                public_base_url=public_base_url)

    try:
        msg_id = mail_client.send(recipients, subject, html, text)
        result["sent"] = True
        # Record the notification on each affected (printer, color)
        for t in transitions_up + transitions_recover:
            _upsert_state(customer["id"], t["printer_id"], t["color"],
                          t["level"], t["severity"],
                          notified=True, notified_severity=t["severity"])
        _log_event(customer["id"], "alert.sent",
                   meta={"transitions": result["transitions"],
                         "recipients": len(recipients), "msg_id": msg_id})
    except mail_client.MailSendError as e:
        result["error"] = str(e)[:200]
        _log_event(customer["id"], "alert.send_failed",
                   meta={"error": result["error"]})

    return result


# ---------------------------------------------------------------------------
# Message composition
# ---------------------------------------------------------------------------

def _compose_subject(customer: dict, ups: list[dict],
                     recoveries: list[dict]) -> str:
    crits = sum(1 for t in ups if t["severity"] == "CRITICAL")
    warns = sum(1 for t in ups if t["severity"] == "WARN")
    parts: list[str] = []
    if crits: parts.append(f"{crits} critical")
    if warns: parts.append(f"{warns} warn")
    if recoveries: parts.append(f"{len(recoveries)} recovered")
    return f"[TonerWatch · {customer['name']}] {' · '.join(parts) or 'update'}"


def _compose_body(customer: dict, ups: list[dict],
                  recoveries: list[dict], *,
                  public_base_url: str = "") -> tuple[str, str]:
    """Return (html, plain-text) versions of the alert body.

    ``public_base_url`` is the origin the app is reachable at from
    outside (``https://tonerwatch.example.com``). Magic-link buttons
    fall back to relative paths when it's empty — still clickable from
    an authenticated browser session, less useful in an email.
    """
    # HTML — inline styles because most mail clients strip <style> or CSP them
    html_rows_up = "".join(
        _alert_row_html(t, public_base_url=public_base_url) for t in ups) or ""
    html_rows_rec = "".join(
        _alert_row_html(t, recovered=True) for t in recoveries) or ""

    html = f"""<!doctype html>
<html><body style="font-family:Arial,Helvetica,sans-serif;color:#231F20;
                   background:#fafafa;margin:0;padding:24px;">
  <table role="presentation" cellpadding="0" cellspacing="0"
         style="max-width:640px;margin:0 auto;background:#fff;
                border:1px solid #E4E4E4;border-radius:12px;overflow:hidden;">
    <tr>
      <td style="height:6px;background:linear-gradient(90deg,#00EB86,#00A0FB);"></td>
    </tr>
    <tr>
      <td style="padding:24px 28px 12px;">
        <div style="color:#8094AA;font-size:11px;letter-spacing:0.14em;
                    text-transform:uppercase;font-weight:700;">
          Printix TonerWatch
        </div>
        <h1 style="color:#002854;margin:6px 0 0;font-size:22px;">
          {_escape(customer['name'])}
        </h1>
        <div style="color:#8094AA;font-size:13px;margin-top:4px;">
          {len(ups)} new alert(s), {len(recoveries)} recovery(s)
        </div>
      </td>
    </tr>
    {'<tr><td style="padding:0 28px 8px;font-weight:700;color:#002854;">'
      'New alerts</td></tr>' + f'<tr><td style="padding:0 28px 16px;">'
      f'<table role="presentation" cellpadding="0" cellspacing="0" width="100%">'
      f'{html_rows_up}</table></td></tr>' if ups else ''}
    {'<tr><td style="padding:0 28px 8px;font-weight:700;color:#065F46;">'
      'Recoveries</td></tr>' + f'<tr><td style="padding:0 28px 16px;">'
      f'<table role="presentation" cellpadding="0" cellspacing="0" width="100%">'
      f'{html_rows_rec}</table></td></tr>' if recoveries else ''}
    <tr>
      <td style="padding:16px 28px;color:#8094AA;font-size:11px;
                 border-top:1px solid #E4E4E4;">
        Sent by Printix TonerWatch — Print Supply Intelligence.
        You are receiving this because your MSP configured your address
        as an alert recipient for this customer.
      </td>
    </tr>
  </table>
</body></html>"""

    # Plain-text variant
    lines: list[str] = []
    lines.append(f"Printix TonerWatch — {customer['name']}")
    lines.append("=" * 60)
    if ups:
        lines.append("\nNEW ALERTS")
        for t in ups:
            lines.append(f"  [{t['severity']:>8}] {t['printer_name']} "
                         f"({t['location'] or '—'}) — {t['color_label']} "
                         f"at {t['level']}%")
            supply = t.get("supply") or {}
            if supply.get("sku"):
                lines.append(f"           Order: {supply['sku']}"
                             + (f" — {supply['supplier_url']}"
                                if supply.get("supplier_url") else ""))
            if t.get("order_token"):
                mark_url = _abs_url(public_base_url,
                                    f"/orders/action/{t['order_token']}")
                lines.append(f"           Mark as ordered: {mark_url}")
    if recoveries:
        lines.append("\nRECOVERED")
        for t in recoveries:
            lines.append(f"  [   OK   ] {t['printer_name']} "
                         f"({t['location'] or '—'}) — {t['color_label']} "
                         f"at {t['level']}%")
    lines.append("")
    lines.append("Sent by Printix TonerWatch — Print Supply Intelligence.")
    return html, "\n".join(lines)


def _alert_row_html(t: dict, *, recovered: bool = False,
                    public_base_url: str = "") -> str:
    if recovered:
        badge_bg, badge_col, badge = "#ECFDF5", "#065F46", "OK"
    elif t["severity"] == "CRITICAL":
        badge_bg, badge_col, badge = "#FEF2F2", "#991B1B", "CRITICAL"
    else:
        badge_bg, badge_col, badge = "#FFFBEB", "#92400E", "WARN"

    color_hex = {"K": "#231F20", "C": "#00A0FB",
                 "M": "#D030E8", "Y": "#FFC600"}.get(t["color"], "#8094AA")

    # Order-info line — SKU + one-click order flow. Only rendered on
    # up-transitions (recoveries don't need a buy button).
    supply = t.get("supply") or {}
    order_html = ""
    if not recovered and (supply.get("sku") or t.get("order_token")):
        parts: list[str] = []
        if supply.get("sku"):
            desc = _escape(supply.get("description", ""))
            parts.append(
                f"""<span style="font-family:ui-monospace,monospace;
                              font-weight:700;color:#002854;">
                    {_escape(supply['sku'])}</span>"""
                + (f' <span style="color:#8094AA;">— {desc}</span>' if desc else "")
            )
        buttons_html = ""
        if supply.get("supplier_url"):
            buttons_html += (
                f'<a href="{_escape(supply["supplier_url"])}" '
                f'style="display:inline-block;padding:6px 14px;'
                f'background:#00EB86;color:#002854;'
                f'border-radius:6px;text-decoration:none;'
                f'font-weight:700;font-size:12px;'
                f'margin-right:8px;">🛒 Order now</a>'
            )
        if t.get("order_token"):
            confirm_url = _abs_url(
                public_base_url, f"/orders/action/{t['order_token']}")
            buttons_html += (
                f'<a href="{confirm_url}" '
                f'style="display:inline-block;padding:6px 14px;'
                f'background:#002854;color:#fff;border-radius:6px;'
                f'text-decoration:none;font-weight:700;font-size:12px;">'
                f'✓ Mark as ordered</a>'
            )
        order_html = f"""
      <tr>
        <td colspan="2" style="padding:0 10px 10px;">
          <div style="padding:10px 12px;background:#F8FAFC;
                      border:1px solid #E2E8F0;border-radius:8px;
                      font-size:12px;line-height:1.5;">
            {" · ".join(parts)}
            <div style="margin-top:8px;">{buttons_html}</div>
          </div>
        </td>
      </tr>"""

    return f"""
      <tr>
        <td style="padding:8px 10px;border-bottom:1px solid #F0F0F0;">
          <div style="font-weight:700;color:#002854;">
            {_escape(t['printer_name']) or '(unnamed printer)'}
          </div>
          <div style="color:#8094AA;font-size:12px;">
            {_escape(t['location']) or '—'}
          </div>
        </td>
        <td style="padding:8px 10px;border-bottom:1px solid #F0F0F0;
                   text-align:right;white-space:nowrap;">
          <span style="display:inline-block;width:10px;height:10px;
                       background:{color_hex};border-radius:2px;
                       margin-right:4px;vertical-align:middle;"></span>
          <span style="font-family:ui-monospace,monospace;font-weight:700;
                       color:#002854;">{t['level']}%</span>
          <span style="display:inline-block;margin-left:8px;padding:2px 8px;
                       border-radius:10px;background:{badge_bg};
                       color:{badge_col};font-size:11px;font-weight:700;
                       letter-spacing:0.06em;">{badge}</span>
        </td>
      </tr>{order_html}"""


def _abs_url(base: str, path: str) -> str:
    """Turn a relative path into an absolute URL. Empty base returns
    the path unchanged so links still work from an authenticated
    browser tab, just not from mail."""
    base = (base or "").rstrip("/")
    if not base:
        return path
    return base + path


def _escape(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

_scheduler = None


def start_runner(interval_minutes: int = 15) -> None:
    """Kick off the APScheduler background runner. No-op if already started
    or if the interval is <= 0 (feature-flag off)."""
    global _scheduler
    if _scheduler is not None or interval_minutes <= 0:
        return
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.interval import IntervalTrigger
    except ImportError:
        logger.warning("APScheduler not installed — runner disabled")
        return

    _scheduler = BackgroundScheduler(timezone="UTC")
    _scheduler.add_job(
        _tick_all_customers,
        trigger=IntervalTrigger(minutes=interval_minutes),
        id="toner_alerts_tick",
        name="Toner alert evaluation",
        max_instances=1,
        coalesce=True,
        next_run_time=_dt.datetime.now(_dt.timezone.utc)
                      + _dt.timedelta(seconds=60),  # wait a bit after boot
    )
    _scheduler.start()
    logger.info("toner_alerts: scheduler started, tick every %d min",
                interval_minutes)


def _tick_all_customers() -> None:
    with db.get_conn() as conn:
        customers = [db._row_to_dict(r) for r in conn.execute(
            select(db.customers).where(db.customers.c.active == 1)
        ).all()]
    for c in customers:
        try:
            summary = evaluate_and_notify(c)
            if summary["sent"]:
                logger.info("alerts sent for customer %s: %d transitions",
                            c["name"], summary["transitions"])
        except Exception as e:  # noqa: BLE001 — runner mustn't die
            logger.exception("evaluate_and_notify crashed for customer %s: %s",
                             c.get("id"), str(e)[:200])
