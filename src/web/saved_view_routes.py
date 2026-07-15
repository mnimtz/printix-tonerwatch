"""Saved-view routes — create / apply / delete for the toner list.

All routes are session-scoped: a user only sees their own views +
views others explicitly shared. Owner-only for delete.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from .. import auth, db, saved_views


router = APIRouter()


@router.post("/toner/views", include_in_schema=False)
async def create_toner_view(request: Request):
    user = auth.require_user(request)
    form = await request.form()
    name = (form.get("name") or "").strip()
    is_shared = bool(form.get("is_shared"))
    # Filters come from the same form: the /toner page includes
    # every active filter as a hidden field so save-current-view
    # captures exactly what's on screen.
    filters = {
        "customer": form.get("customer") or "",
        "severity": form.get("severity") or "",
        "group":    form.get("group") or "",
        "q":        form.get("q") or "",
        "view":     form.get("view") or "",
    }
    try:
        view_id = saved_views.create_view(
            user["id"], "toner", name, filters,
            is_shared=is_shared)
    except saved_views.SavedViewError as e:
        return RedirectResponse(
            f"/toner?error={str(e)[:120].replace('&','')}",
            status_code=303)
    db.audit(user["id"], "saved_view.created",
             target_type="saved_view", target_id=str(view_id),
             meta_json=json.dumps({"name": name, "is_shared": is_shared}))
    # Redirect back into the just-saved view so the user sees the
    # chip highlighted immediately.
    qs = saved_views.filters_to_querystring(filters)
    return RedirectResponse(
        f"/toner?{qs}&view_saved=1" if qs else "/toner?view_saved=1",
        status_code=303)


@router.post("/toner/views/{view_id}/delete", include_in_schema=False)
async def delete_toner_view(view_id: int, request: Request):
    user = auth.require_user(request)
    ok = saved_views.delete_view(view_id, user["id"])
    if ok:
        db.audit(user["id"], "saved_view.deleted",
                 target_type="saved_view", target_id=str(view_id))
        return RedirectResponse("/toner?view_deleted=1", status_code=303)
    return RedirectResponse("/toner?error=cannot_delete_view", status_code=303)


@router.get("/toner/views/{view_id}/apply", include_in_schema=False)
async def apply_toner_view(view_id: int, request: Request):
    """Apply a saved view — redirect to /toner with the saved filters
    as query params. Doubles as a bookmarkable URL for the user."""
    user = auth.require_user(request)
    view = saved_views.get_view(view_id)
    if view is None or view["scope"] != "toner":
        return RedirectResponse("/toner?error=view_not_found", status_code=303)
    # Access check: own view or shared. Anything else is 403-esque.
    if view["user_id"] != user["id"] and not view.get("is_shared"):
        return RedirectResponse("/toner?error=forbidden", status_code=303)
    qs = saved_views.filters_to_querystring(view.get("filters") or {})
    target = f"/toner?{qs}" if qs else "/toner"
    return RedirectResponse(target, status_code=303)
