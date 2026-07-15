"""Human-friendly rendering for Printix printer status codes.

`detected_error_states` and `printer_reported_state` come out of the
Printix BI DB as terse SNMP-style codes (NO_PAPER, JAMMED, LOW_TONER,
MARKER_SUPPLY_LOW, …). Bare uppercase reads like log spam in a UI, so
this module maps each code to a colored badge with an icon and an
i18n key. Unknown codes fall back to the raw code, styled neutrally,
so a new state from a firmware update still shows up sensibly.
"""

from __future__ import annotations

# severity → tailwind-ish colour band used by badge_class() below
# critical: red   warn: amber   info: slate   ok: green
_ERROR_STATE_META: dict[str, tuple[str, str]] = {
    # ── Critical — printer is stopped or effectively unusable ──────
    "NO_PAPER":                  ("critical", "📄"),
    "JAMMED":                    ("critical", "⚙"),
    "PAPER_JAM":                 ("critical", "⚙"),
    "DOOR_OPEN":                 ("critical", "🚪"),
    "COVER_OPEN":                ("critical", "🚪"),
    "OFFLINE":                   ("critical", "🔌"),
    "MARKER_SUPPLY_EMPTY":       ("critical", "⚫"),
    "MARKER_WASTE_FULL":         ("critical", "🗑"),
    "INPUT_TRAY_MISSING":        ("critical", "📥"),
    "OUTPUT_TRAY_MISSING":       ("critical", "📤"),
    "INPUT_MEDIA_EMPTY":         ("critical", "📄"),
    "OUTPUT_AREA_FULL":          ("critical", "📤"),
    "SUBUNIT_MISSING":           ("critical", "❗"),
    "SUBUNIT_LIFE_OVER":         ("critical", "🛠"),
    "FUSER_OVER_TEMP":           ("critical", "🌡"),
    "FUSER_UNDER_TEMP":          ("critical", "🌡"),

    # ── Warn — needs attention soon, printer still runs ────────────
    "LOW_PAPER":                 ("warn", "📄"),
    "LOW_TONER":                 ("warn", "🎨"),
    "MARKER_SUPPLY_LOW":         ("warn", "🎨"),
    "MARKER_WASTE_ALMOST_FULL":  ("warn", "🗑"),
    "OUTPUT_BIN_NEAR_FULL":      ("warn", "📤"),
    "INPUT_MEDIA_LOW":           ("warn", "📄"),
    "INPUT_TRAY_EMPTY":          ("warn", "📥"),
    "SERVICE_REQUESTED":         ("warn", "🛠"),
    "SUBUNIT_LIFE_ALMOST_OVER":  ("warn", "🛠"),
    "SUBUNIT_NEAR_LIMIT":        ("warn", "🛠"),
    "TONER_LOW":                 ("warn", "🎨"),
    "TIMED_OUT":                 ("warn", "⏱"),

    # ── Info — informational, no action strictly required ─────────
    "MAINTENANCE_REQUIRED":      ("info", "🛠"),
    "CALIBRATING":               ("info", "⚙"),
    "WARMUP":                    ("info", "🌡"),
}

# printer_reported_state — IDLE/PRINTING intentionally omitted (normal
# working states; showing a badge for those is noise).
_REPORTED_STATE_META: dict[str, tuple[str, str]] = {
    "STOPPED":     ("critical", "⏸"),
    "DOWN":        ("critical", "⛔"),
    "OFFLINE":     ("critical", "🔌"),
    "UNKNOWN":     ("info",     "❓"),
    "MAINTENANCE": ("warn",     "🛠"),
    "WARNING":     ("warn",     "⚠"),
}

# States that should NOT render a badge (normal working states).
HIDDEN_REPORTED_STATES = {"", "IDLE", "PRINTING"}


def error_state_meta(code: str) -> tuple[str, str, str | None]:
    """Return (severity, icon, i18n_key) for a detected error state.

    If the code is not in the map, returns ("warn", "⚠", None) — the
    caller then renders the raw code as the text. Codes are compared
    case-insensitive and stripped, so a stray "  low_toner " from the
    DB still hits the map.
    """
    key = (code or "").strip().upper()
    if key in _ERROR_STATE_META:
        sev, icon = _ERROR_STATE_META[key]
        return sev, icon, f"label.error.{key}"
    return "warn", "⚠", None


def reported_state_meta(code: str) -> tuple[str, str, str | None]:
    """Return (severity, icon, i18n_key) for a reported printer state."""
    key = (code or "").strip().upper()
    if key in _REPORTED_STATE_META:
        sev, icon = _REPORTED_STATE_META[key]
        return sev, icon, f"label.state.{key}"
    return "info", "•", None


def is_hidden_reported_state(code: str) -> bool:
    """True for normal working states we don't badge."""
    return (code or "").strip().upper() in HIDDEN_REPORTED_STATES
