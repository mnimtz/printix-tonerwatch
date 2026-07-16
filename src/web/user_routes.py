"""MSP user management — admin-only CRUD + toggle-active + password reset."""

from __future__ import annotations

import json
import secrets

from fastapi import APIRouter, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, insert, select, update

from .. import auth, db
from ..db import users
from . import i18n


router = APIRouter()


def _user_row(user_id: int) -> dict | None:
    with db.get_conn() as conn:
        row = conn.execute(
            select(users).where(users.c.id == user_id)
        ).first()
    return db._row_to_dict(row)


def _all_users() -> list[dict]:
    with db.get_conn() as conn:
        rows = conn.execute(
            select(users).order_by(users.c.role, users.c.name, users.c.email)
        ).all()
    return [db._row_to_dict(r) for r in rows]


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------

@router.get("/users", response_class=HTMLResponse, include_in_schema=False)
async def users_list(request: Request):
    admin = auth.require_admin(request)
    templates = request.app.state.templates
    # v0.18.5: role filter — sticky in session so a reload keeps it.
    # Same pattern as the toner/orders customer filter.
    raw = (request.query_params.get("role") or "").strip().lower()
    if raw == "all":
        filter_role = ""
        try:
            request.session.pop("users_role", None)
        except AssertionError:
            pass
    elif raw in ("admin", "technician"):
        filter_role = raw
        try:
            request.session[f"users_role"] = raw
        except AssertionError:
            pass
    else:
        try:
            sess = request.session.get("users_role", "")
        except AssertionError:
            sess = ""
        filter_role = sess if sess in ("admin", "technician") else ""

    rows = _all_users()
    counts = {
        "total":      len(rows),
        "admin":      sum(1 for r in rows if r["role"] == "admin"),
        "technician": sum(1 for r in rows if r["role"] == "technician"),
    }
    if filter_role:
        rows = [r for r in rows if r["role"] == filter_role]

    return templates.TemplateResponse(
        "users/list.html",
        {
            "request": request,
            "lang": request.state.lang,
            "user": admin,
            "users": rows,
            "counts": counts,
            "filter_role": filter_role,
        },
    )


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------

@router.get("/users/new", response_class=HTMLResponse, include_in_schema=False)
async def user_new_form(request: Request):
    admin = auth.require_admin(request)
    templates = request.app.state.templates
    generated = secrets.token_urlsafe(16)
    return templates.TemplateResponse(
        "users/edit.html",
        {
            "request": request,
            "lang": request.state.lang,
            "user": admin,
            "form": {"id": None, "email": "", "name": "",
                     "role": "technician", "active": 1, "lang": "en",
                     "generated_password": generated},
            "is_new": True,
            "error": None,
        },
    )


@router.post("/users/new", include_in_schema=False)
async def user_new_submit(request: Request,
                          email: str = Form(""),
                          name: str = Form(""),
                          role: str = Form("technician"),
                          password: str = Form(""),
                          lang: str = Form("en"),
                          active: str = Form("1")):
    admin = auth.require_admin(request)
    templates = request.app.state.templates
    ui_lang = request.state.lang

    email = email.strip().lower()
    name = name.strip()
    role = role if role in ("admin", "technician") else "technician"
    lang = lang if lang in i18n.SUPPORTED_LANGS else "en"

    err = None
    if not email:
        err = i18n.t("user.error.email_required", ui_lang)
    elif "@" not in email:
        err = i18n.t("user.error.email_invalid", ui_lang)
    elif len(password) < 12:
        err = i18n.t("user.error.password_too_short", ui_lang)
    elif db.find_user_by_email(email) is not None:
        err = i18n.t("user.error.email_taken", ui_lang)

    if err:
        return templates.TemplateResponse(
            "users/edit.html",
            {
                "request": request,
                "lang": ui_lang,
                "user": admin,
                "form": {"id": None, "email": email, "name": name, "role": role,
                         "active": 1 if active else 0, "lang": lang,
                         "generated_password": password},
                "is_new": True,
                "error": err,
            },
            status_code=400,
        )

    with db.get_conn() as conn:
        result = conn.execute(insert(users).values(
            email=email,
            password_hash=auth.hash_password(password),
            name=name,
            role=role,
            lang=lang,
            active=1 if active in ("1", "on", "true", "yes") else 0,
        ))
        new_id = result.inserted_primary_key[0]
    db.audit(admin["id"], "user.created",
             target_type="user", target_id=str(new_id),
             meta_json=json.dumps({"email": email, "role": role}))

    return RedirectResponse(f"/users/{new_id}/access", status_code=303)


# ---------------------------------------------------------------------------
# Edit
# ---------------------------------------------------------------------------

@router.get("/users/{user_id}/edit",
            response_class=HTMLResponse, include_in_schema=False)
async def user_edit_form(user_id: int, request: Request):
    admin = auth.require_admin(request)
    row = _user_row(user_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        "users/edit.html",
        {
            "request": request,
            "lang": request.state.lang,
            "user": admin,
            "form": {**row, "generated_password": ""},
            "is_new": False,
            "error": None,
        },
    )


@router.post("/users/{user_id}/edit", include_in_schema=False)
async def user_edit_submit(user_id: int, request: Request,
                           email: str = Form(""),
                           name: str = Form(""),
                           role: str = Form("technician"),
                           new_password: str = Form(""),
                           lang: str = Form("en"),
                           active: str = Form("")):
    admin = auth.require_admin(request)
    templates = request.app.state.templates
    ui_lang = request.state.lang
    row = _user_row(user_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)

    email = email.strip().lower()
    name = name.strip()
    role = role if role in ("admin", "technician") else "technician"
    lang = lang if lang in i18n.SUPPORTED_LANGS else "en"

    err = None
    if not email or "@" not in email:
        err = i18n.t("user.error.email_invalid", ui_lang)
    elif email != row["email"]:
        # changed email → must not clash with someone else
        other = db.find_user_by_email(email)
        if other is not None and int(other["id"]) != user_id:
            err = i18n.t("user.error.email_taken", ui_lang)
    if not err and new_password and len(new_password) < 12:
        err = i18n.t("user.error.password_too_short", ui_lang)

    # Don't let an admin demote themselves out of their only-admin role
    if not err and admin["id"] == user_id and role != "admin":
        with db.get_conn() as conn:
            n_admins = conn.execute(
                select(func.count()).select_from(users)
                .where(users.c.role == "admin").where(users.c.active == 1)
            ).scalar_one()
        if n_admins <= 1:
            err = i18n.t("user.error.last_admin_demote", ui_lang)

    if err:
        return templates.TemplateResponse(
            "users/edit.html",
            {
                "request": request,
                "lang": ui_lang,
                "user": admin,
                "form": {**row, "email": email, "name": name, "role": role,
                         "lang": lang, "generated_password": ""},
                "is_new": False,
                "error": err,
            },
            status_code=400,
        )

    values: dict = {"email": email, "name": name, "role": role,
                    "lang": lang,
                    "active": 1 if active in ("1", "on", "true", "yes") else 0}
    if new_password:
        values["password_hash"] = auth.hash_password(new_password)

    with db.get_conn() as conn:
        conn.execute(update(users).where(users.c.id == user_id).values(**values))
    db.audit(admin["id"], "user.updated",
             target_type="user", target_id=str(user_id),
             meta_json=json.dumps({
                 "email": email, "role": role,
                 "password_changed": bool(new_password),
             }))
    return RedirectResponse("/users", status_code=303)
