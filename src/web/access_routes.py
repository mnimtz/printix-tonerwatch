"""User↔Customer access-grid management (admin-only)."""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import delete, insert, select

from .. import auth, db
from ..db import customer_access, customers, users


router = APIRouter()


@router.get("/users/{user_id}/access",
            response_class=HTMLResponse, include_in_schema=False)
async def user_access_form(user_id: int, request: Request):
    admin = auth.require_admin(request)
    with db.get_conn() as conn:
        u = conn.execute(select(users).where(users.c.id == user_id)).first()
        if u is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND)
        target = db._row_to_dict(u)

        all_customers = [db._row_to_dict(r) for r in conn.execute(
            select(customers).where(customers.c.active == 1)
            .order_by(customers.c.name)
        ).all()]

        granted = {r[0]: r[1] for r in conn.execute(
            select(customer_access.c.customer_id,
                   customer_access.c.access_level)
            .where(customer_access.c.user_id == user_id)
        ).all()}

    templates = request.app.state.templates
    return templates.TemplateResponse(
        "users/access.html",
        {
            "request": request,
            "lang": request.state.lang,
            "user": admin,
            "target": target,
            "all_customers": all_customers,
            "granted": granted,
        },
    )


@router.post("/users/{user_id}/access", include_in_schema=False)
async def user_access_submit(user_id: int, request: Request):
    admin = auth.require_admin(request)
    form = await request.form()

    # Guard: the target user must exist
    with db.get_conn() as conn:
        u = conn.execute(select(users).where(users.c.id == user_id)).first()
        if u is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND)

        # Form field names are of the form access:<customer_id> = "read"|"admin"
        # An unchecked customer sends no field at all → falls out of the dict.
        new_grants: dict[int, str] = {}
        for key, val in form.multi_items():
            if not key.startswith("access:"):
                continue
            try:
                cid = int(key.split(":", 1)[1])
            except ValueError:
                continue
            level = (val or "").strip().lower()
            if level not in ("read", "admin"):
                level = "read"
            new_grants[cid] = level

        # Overwrite: nuke everything for this user, then insert the new set.
        # A single transaction keeps a technician from being briefly
        # access-less mid-refresh.
        conn.execute(delete(customer_access)
                     .where(customer_access.c.user_id == user_id))
        for cid, level in new_grants.items():
            conn.execute(insert(customer_access).values(
                user_id=user_id,
                customer_id=cid,
                access_level=level,
                granted_by=admin["id"],
            ))

    db.audit(admin["id"], "user.access_updated",
             target_type="user", target_id=str(user_id),
             meta_json=json.dumps({"customer_ids": sorted(new_grants.keys())}))
    return RedirectResponse("/users", status_code=303)
