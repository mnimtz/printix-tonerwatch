"""Saved views — named filter presets that persist across sessions.

Every user can save their current filter state on any list-view
scope (currently only 'toner'; the schema supports 'orders',
'printers', 'customers' too for later expansion). A view can be
private (default) or shared with everyone in the tenant.

Filters are stored as a JSON blob under `filters_json` — narrow
schema so we don't have to migrate every time we add a new filter
dimension to /toner.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import and_, delete, insert, or_, select
from sqlalchemy.exc import IntegrityError

from . import db


ALLOWED_SCOPES = ("toner", "orders", "printers", "customers")

# Which query-string keys we accept when saving a view. Anything not
# in this whitelist gets dropped so a URL-crafted attack can't stuff
# random state into the saved view.
_TONER_FILTER_KEYS = ("customer", "severity", "group", "q", "view")

_SCOPE_KEYS = {
    "toner": _TONER_FILTER_KEYS,
}


class SavedViewError(Exception):
    """Raised on validation / integrity issues."""


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

def list_visible(user_id: int, scope: str) -> list[dict[str, Any]]:
    """Return every view this user can see for the given scope:
    own views + views others have marked as shared. Ordered by
    (own first, then alphabetically)."""
    scope = _validate_scope(scope)
    with db.get_conn() as conn:
        rows = conn.execute(
            select(db.saved_views)
            .where(and_(
                db.saved_views.c.scope == scope,
                or_(
                    db.saved_views.c.user_id == user_id,
                    db.saved_views.c.is_shared == 1,
                ),
            ))
        ).all()
    out = []
    for r in rows:
        d = db._row_to_dict(r)
        d["is_own"] = (d["user_id"] == user_id)
        try:
            d["filters"] = json.loads(d.get("filters_json") or "{}")
        except json.JSONDecodeError:
            d["filters"] = {}
        out.append(d)
    out.sort(key=lambda v: (not v["is_own"], (v["name"] or "").lower()))
    return out


def get_view(view_id: int) -> dict[str, Any] | None:
    with db.get_conn() as conn:
        row = conn.execute(
            select(db.saved_views).where(db.saved_views.c.id == view_id)
        ).first()
    if row is None:
        return None
    d = db._row_to_dict(row)
    try:
        d["filters"] = json.loads(d.get("filters_json") or "{}")
    except json.JSONDecodeError:
        d["filters"] = {}
    return d


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

def create_view(
    user_id: int, scope: str, name: str, filters: dict[str, Any],
    is_shared: bool = False,
) -> int:
    scope = _validate_scope(scope)
    name = (name or "").strip()
    if not name:
        raise SavedViewError("name is required")
    if len(name) > 60:
        name = name[:60]
    clean = _sanitise_filters(scope, filters)

    with db.get_conn() as conn:
        try:
            result = conn.execute(insert(db.saved_views).values(
                user_id=user_id, scope=scope, name=name,
                filters_json=json.dumps(clean, ensure_ascii=False),
                is_shared=1 if is_shared else 0,
            ))
        except IntegrityError as e:
            raise SavedViewError(str(e)[:200]) from e
    return int(result.inserted_primary_key[0])


def delete_view(view_id: int, user_id: int) -> bool:
    """Delete a view. Only the OWNER can delete; returns True if a row
    was actually removed, False if not (view not found / not owned)."""
    with db.get_conn() as conn:
        row = conn.execute(
            select(db.saved_views.c.user_id)
            .where(db.saved_views.c.id == view_id)
        ).first()
        if row is None or row.user_id != user_id:
            return False
        conn.execute(
            delete(db.saved_views).where(db.saved_views.c.id == view_id)
        )
    return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _validate_scope(scope: str) -> str:
    scope = (scope or "").strip().lower()
    if scope not in ALLOWED_SCOPES:
        raise SavedViewError(f"unknown scope: {scope!r}")
    return scope


def _sanitise_filters(scope: str, filters: dict[str, Any]) -> dict[str, str]:
    """Only keep whitelisted keys with non-empty string values.
    Coerces everything to str so the JSON blob stays predictable."""
    allowed = _SCOPE_KEYS.get(scope, ())
    out: dict[str, str] = {}
    for k in allowed:
        v = filters.get(k)
        if v is None or v == "":
            continue
        s = str(v).strip()
        if s:
            out[k] = s
    return out


def filters_to_querystring(filters: dict[str, Any]) -> str:
    """Turn a saved filter dict into a URL query string
    (percent-encoded, sorted for deterministic URLs)."""
    from urllib.parse import urlencode
    return urlencode(sorted(filters.items()))
