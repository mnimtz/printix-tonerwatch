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

from . import backup as _backup
from . import bi_client, db, mail_client, orders, supply_library
from . import graph_connector as _graph


logger = logging.getLogger(__name__)


SEVERITY_RANK = {"OK": 0, "UNKNOWN": 0, "INFO": 0, "WARN": 1, "CRITICAL": 2}
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


def _detect_level_anomaly(prev_level: int, level: int) -> str | None:
    """v0.24.5 — flag a toner-level reading that doesn't match any
    normal real-world event between two poll ticks, without needing
    any historical time series (we only ever have "previous tick" vs
    "this tick" from ``toner_state``).

    A cartridge replacement resets the level close to 100% — an
    increase that lands somewhere in the middle instead is the
    clearest signal a real event can't produce, so it's the primary,
    high-confidence finding. A very steep one-tick drop is softer
    evidence (a large print job is plausible) but still worth a look.
    Both thresholds are deliberately conservative to avoid noise —
    this only flags for review, it never blocks or auto-acts on
    anything."""
    delta = level - prev_level
    if delta >= 5 and level < 85:
        return "partial_increase"
    if delta <= -40:
        return "steep_drop"
    return None


def list_recent_anomalies(customer_id: int, limit: int = 5) -> list[dict]:
    """v0.24.5 — most recent ``toner.anomaly`` events for one customer,
    newest first. Backs the customer detail page's anomaly card."""
    with db.get_conn() as conn:
        rows = conn.execute(
            select(db.toner_events)
            .where(db.toner_events.c.customer_id == customer_id)
            .where(db.toner_events.c.kind == "toner.anomaly")
            .order_by(db.toner_events.c.created_at.desc())
            .limit(limit)
        ).all()
    out = []
    for r in rows:
        d = db._row_to_dict(r)
        try:
            d["meta"] = json.loads(d.get("meta_json") or "{}")
        except json.JSONDecodeError:
            d["meta"] = {}
        out.append(d)
    return out


def list_recent_anomalies_multi(customer_ids: list[int], limit: int = 5) -> list[dict]:
    """v0.24.13 — most recent ``toner.anomaly`` events across several
    customers in one query, newest first, with the customer name
    resolved in — backs the AI dashboard greeting, which needs to say
    *which* customer without an extra lookup per row."""
    if not customer_ids:
        return []
    with db.get_conn() as conn:
        rows = conn.execute(
            select(db.toner_events, db.customers.c.name.label("customer_name"))
            .select_from(db.toner_events.join(
                db.customers, db.customers.c.id == db.toner_events.c.customer_id))
            .where(db.toner_events.c.customer_id.in_(customer_ids))
            .where(db.toner_events.c.kind == "toner.anomaly")
            .order_by(db.toner_events.c.created_at.desc())
            .limit(limit)
        ).all()
    out = []
    for r in rows:
        d = db._row_to_dict(r)
        try:
            d["meta"] = json.loads(d.get("meta_json") or "{}")
        except json.JSONDecodeError:
            d["meta"] = {}
        out.append(d)
    return out


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

            prev_level = prev.get("level")
            if prev_level is not None and level is not None:
                anomaly = _detect_level_anomaly(prev_level, level)
                if anomaly:
                    _log_event(customer["id"], "toner.anomaly",
                               printer_id=p["printer_id"], color=color,
                               level=level, severity=severity,
                               meta={"prev_level": prev_level, "kind": anomaly,
                                     "printer_name": p["printer_name"] or ""})

            row = {
                "printer_id":   p["printer_id"],
                "printer_name": p["printer_name"] or "",
                # v0.17.1: was `printer_model` — BI emits `model`, so
                # every auto-draft got sku="" and every alert-mail row
                # lost its Order button.
                "printer_model": p.get("model") or "",
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
            else:
                # No threshold crossing → free to persist the fresh level.
                # v0.17.1: previously we persisted for EVERY slot even
                # on a crossing, which meant that if quiet-hours or a
                # missing-recipient abort came next, the DB was already
                # advanced and the transition would never fire again.
                # Now transition slots get their state written only
                # after the send decision (see below).
                _upsert_state(customer["id"], p["printer_id"], color, level,
                              severity, notified=False)

    result["transitions"] = len(transitions_up) + len(transitions_recover)
    if not transitions_up and not transitions_recover:
        return result

    # Auto-draft: for each critical/warn transition, resolve the supply
    # record and either attach an existing active order or create a new
    # draft. The mail then carries the SKU + a one-click "Mark as
    # ordered" magic link per alert row.
    # v0.20.0 read the customer's ordering mode + daily cap ONCE per
    # tick so all transitions in this batch use consistent values,
    # even if an admin flips the setting mid-run.
    _order_mode = (customer.get("auto_order_mode") or "off").lower()
    _daily_cap  = int(customer.get("auto_order_daily_cap") or 10)
    _orders_today = orders.count_today(customer["id"]) if _order_mode == "autonomous" else 0

    for t in transitions_up:
        supply = supply_library.resolve_supply(
            customer["id"], t["printer_id"],
            t["printer_model"], t["color"])
        t["supply"] = supply
        sku = (supply or {}).get("sku", "")
        qty = int((supply or {}).get("default_quantity") or 1)
        order_id, created_new = orders.create_draft_if_none(
            customer["id"], t["printer_id"],
            t["printer_name"], t["color"],
            sku=sku, quantity=qty)
        t["order_id"] = order_id
        t["order_token"] = orders.sign_action_token(order_id, "ordered")

        # v0.20.0 — AI SKU completion. Only fire when we JUST created
        # the draft (created_new) AND the supply template gave us
        # nothing. Doing this AFTER create_draft_if_none avoids paying
        # LLM cost on every recovery/re-fire of the same slot.
        if created_new and not sku and t.get("printer_model"):
            ai = supply_library.ai_suggest_supply(
                t["printer_model"], t["color"])
            if ai and ai.get("sku"):
                orders.update_draft_sku(
                    order_id, ai["sku"],
                    notes_append=(
                        f"AI-suggested {ai['sku']}"
                        + (f" ({ai['description']})"
                            if ai.get("description") else "")
                        + f" via {ai.get('provider', 'llm')}"))
                t["ai_sku"] = ai["sku"]
                t["ai_description"] = ai.get("description") or ""
                _log_event(customer["id"], "order.ai_completed",
                           meta={"order_id": order_id,
                                  "sku": ai["sku"],
                                  "provider": ai.get("provider")})

        # v0.20.0 — autonomous mode: transition the fresh draft straight
        # to "ordered", up to the customer's daily cap. Anything above
        # the cap stays as a draft (operator must approve manually).
        if (_order_mode == "autonomous" and created_new
                and _orders_today < _daily_cap):
            try:
                orders.transition(order_id, "ordered",
                                   user_id=None,
                                   reason="auto-ordered by runner "
                                          "(customer.auto_order_mode=autonomous)")
                _orders_today += 1
                t["auto_ordered"] = True
                _log_event(customer["id"], "order.auto_ordered",
                           meta={"order_id": order_id,
                                  "orders_today": _orders_today,
                                  "cap": _daily_cap})
            except orders.OrderError as e:
                logger.warning("auto-order transition failed for "
                                "order %s: %s", order_id, e)
        elif _order_mode == "autonomous" and created_new:
            # Hit the cap — flag it so the mail template can surface it.
            t["auto_order_cap_hit"] = True
            _log_event(customer["id"], "order.auto_order_cap_hit",
                       meta={"order_id": order_id,
                              "orders_today": _orders_today,
                              "cap": _daily_cap})

    # v0.17.1: when the alert is suppressed (quiet hours, no recipients),
    # DO NOT advance toner_state for the transitioning slots. Otherwise
    # the next tick sees prev_sev == curr_sev, treats it as no-crossing,
    # and the alert is lost forever. State for these slots stays at the
    # PREVIOUS severity so the transition re-fires when the suppression
    # condition clears.
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
        # v0.20.0 — surface autonomous/AI activity in the mail so the
        # operator can tell at a glance whether they still need to act.
        badge_html = ""
        if t.get("auto_ordered"):
            badge_html = (
                '<div style="margin-top:6px;padding:6px 10px;'
                'background:#ECFDF5;border:1px solid #10B981;'
                'border-radius:6px;color:#065F46;font-weight:700;'
                'font-size:11px;">🤖 Auto-ordered — no action needed</div>')
        elif t.get("auto_order_cap_hit"):
            badge_html = (
                '<div style="margin-top:6px;padding:6px 10px;'
                'background:#FFFBEB;border:1px solid #FCD34D;'
                'border-radius:6px;color:#92400E;font-weight:700;'
                'font-size:11px;">⚠ Daily auto-order cap reached — draft only</div>')
        elif t.get("ai_sku"):
            badge_html = (
                '<div style="margin-top:6px;padding:6px 10px;'
                'background:#EEF2FF;border:1px solid #A5B4FC;'
                'border-radius:6px;color:#3730A3;font-weight:700;'
                f'font-size:11px;">🧠 AI-suggested SKU: {_escape(t["ai_sku"])}</div>')
        order_html = f"""
      <tr>
        <td colspan="2" style="padding:0 10px 10px;">
          <div style="padding:10px 12px;background:#F8FAFC;
                      border:1px solid #E2E8F0;border-radius:8px;
                      font-size:12px;line-height:1.5;">
            {" · ".join(parts)}
            <div style="margin-top:8px;">{buttons_html}</div>
            {badge_html}
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


def reschedule_intervals(alert_minutes: int, refresh_minutes: int) -> None:
    """Apply new intervals to the already-running scheduler jobs.
    Called by runner_config.save_config() after the settings form is
    saved, so an operator's edit takes effect without a server restart.
    Silent no-op if the scheduler hasn't started yet or if the requested
    interval is invalid."""
    if _scheduler is None:
        return
    try:
        from apscheduler.triggers.interval import IntervalTrigger
    except ImportError:
        return
    alert_minutes   = max(1, int(alert_minutes or 0))
    refresh_minutes = max(1, int(refresh_minutes or 0))
    try:
        _scheduler.reschedule_job(
            "toner_alerts_tick",
            trigger=IntervalTrigger(minutes=alert_minutes))
        logger.info("alert-runner rescheduled: every %d min", alert_minutes)
    except Exception as e:  # noqa: BLE001
        logger.warning("reschedule alert runner failed: %s", e)
    try:
        _scheduler.reschedule_job(
            "toner_cache_refresh",
            trigger=IntervalTrigger(minutes=refresh_minutes))
        logger.info("cache-refresh rescheduled: every %d min", refresh_minutes)
    except Exception as e:  # noqa: BLE001
        logger.warning("reschedule cache refresh failed: %s", e)


def start_runner(interval_minutes: int | None = None) -> None:
    """Kick off the APScheduler background runner. No-op if already started.

    ``interval_minutes`` is now an OVERRIDE — when omitted (or ``None``),
    the config is loaded from the settings table via ``runner_config``,
    which itself falls back to env vars and then to defaults. Kept as
    a parameter so tests can force a value without touching the DB.
    """
    global _scheduler
    if _scheduler is not None:
        return
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.interval import IntervalTrigger
    except ImportError:
        logger.warning("APScheduler not installed — runner disabled")
        return

    # Late import — runner_config imports us back for reschedule.
    from . import runner_config
    cfg = runner_config.load_config()
    alert_min = int(interval_minutes) if interval_minutes else cfg["alert_interval_minutes"]
    refresh_min = cfg["refresh_interval_minutes"]
    if alert_min <= 0:
        return

    _scheduler = BackgroundScheduler(timezone="UTC")
    _scheduler.add_job(
        _tick_all_customers,
        trigger=IntervalTrigger(minutes=alert_min),
        id="toner_alerts_tick",
        name="Toner alert evaluation",
        max_instances=1,
        coalesce=True,
        next_run_time=_dt.datetime.now(_dt.timezone.utc)
                      + _dt.timedelta(seconds=60),  # wait a bit after boot
    )

    # v0.8: Background BI-cache warmer.
    # v0.15: interval now editable in Settings (was env-only).
    if refresh_min > 0:
        _scheduler.add_job(
            _tick_cache_refresh,
            trigger=IntervalTrigger(minutes=refresh_min),
            id="toner_cache_refresh",
            name="Toner cache warm-up",
            max_instances=1,
            coalesce=True,
            next_run_time=_dt.datetime.now(_dt.timezone.utc)
                          + _dt.timedelta(seconds=15),
        )

    # v0.10: Azure Blob backup job. Reads the persisted config on
    # every tick so a settings save takes effect on the next fire
    # without needing a restart. The job itself decides whether to
    # actually run (config.enabled + valid connection string).
    _scheduler.add_job(
        _tick_backup_upload,
        trigger=IntervalTrigger(hours=1),
        id="backup_upload",
        name="Backup — Azure Blob upload",
        max_instances=1,
        coalesce=True,
        next_run_time=_dt.datetime.now(_dt.timezone.utc)
                      + _dt.timedelta(minutes=5),
    )

    # v0.14: M365 Copilot Connector sync. Same debounce pattern as
    # backup upload — the job checks the persisted interval + last
    # sync timestamp before actually pushing to Graph.
    _scheduler.add_job(
        _tick_graph_sync,
        trigger=IntervalTrigger(hours=1),
        id="graph_sync",
        name="M365 Copilot Connector sync",
        max_instances=1,
        coalesce=True,
        next_run_time=_dt.datetime.now(_dt.timezone.utc)
                      + _dt.timedelta(minutes=10),
    )

    _scheduler.start()
    logger.info("toner_alerts: scheduler started, alerts every %d min, "
                "cache refresh every %d min", alert_min, refresh_min)


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


def _tick_graph_sync() -> None:
    """Push all printers to M365 Copilot Connector, if enabled."""
    try:
        cfg = _graph.load_config()
    except Exception:
        return
    if not cfg.get("enabled") or not cfg.get("client_secret"):
        return
    interval = max(1, int(cfg.get("interval_hours") or 24))
    last = (cfg.get("last_sync_at") or "").split(" UTC")[0]
    if last:
        try:
            last_dt = _dt.datetime.strptime(last, "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=_dt.timezone.utc)
            if (_dt.datetime.now(_dt.timezone.utc) - last_dt).total_seconds() < interval * 3600 - 60:
                return
        except ValueError:
            pass
    public_base = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    pushed, err = _graph.sync_all_printers(public_base_url=public_base)
    if err:
        logger.info("scheduled graph sync: pushed %d, errors: %s", pushed, err)
    else:
        logger.info("scheduled graph sync: pushed %d items OK", pushed)


def _tick_backup_upload() -> None:
    """Fire the Azure Blob upload if the operator's schedule is due.
    Idle by default. Enabled + interval_hours are configured via
    /settings → Backup section."""
    try:
        cfg = _backup.load_config()
    except Exception:
        return
    if not cfg.get("azure_enabled") or not cfg.get("azure_conn_str"):
        return
    # Debounce: don't run if the last upload was less than
    # `azure_interval_hours` ago.
    interval = max(1, int(cfg.get("azure_interval_hours") or 24))
    last = (cfg.get("last_upload_at") or "").split(" UTC")[0]
    if last:
        try:
            last_dt = _dt.datetime.strptime(last, "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=_dt.timezone.utc)
            if (_dt.datetime.now(_dt.timezone.utc) - last_dt).total_seconds() < interval * 3600 - 60:
                return
        except ValueError:
            pass
    ok, msg = _backup.run_scheduled_upload()
    if ok:
        logger.info("scheduled backup upload OK: %s", msg)
    else:
        logger.info("scheduled backup upload skipped/failed: %s", msg)


def _tick_cache_refresh() -> None:
    """Pre-warm the BI-DB cache for every active customer.

    Runs on a shorter interval than the alert evaluator so the dashboard
    and toner grid always read from memory. Silent no-op for customers
    without BI credentials.
    """
    with db.get_conn() as conn:
        customers = [db._row_to_dict(r) for r in conn.execute(
            select(db.customers).where(db.customers.c.active == 1)
        ).all()]
    for c in customers:
        if not (c.get("sql_server") and c.get("sql_database")
                and c.get("sql_username")):
            continue
        try:
            bi = bi_client.customer_for_bi(c)
            # force_refresh=False: normal cache-write; on cold cache
            # this fetches, on warm cache it's a fast returning read
            # (the fetch function no-ops if the entry is still fresh).
            bi_client.invalidate_customer_cache(c["id"])
            bi_client.fetch_all_printer_supplies(bi)
        except Exception as e:  # noqa: BLE001 — never let the tick die
            logger.info("cache refresh: customer %s failed: %s",
                        c.get("id"), str(e)[:120])
