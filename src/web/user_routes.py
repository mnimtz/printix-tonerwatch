"""MSP user management — admin-only CRUD + toggle-active + password reset."""

from __future__ import annotations

import json
import secrets

from fastapi import APIRouter, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, insert, select, update

from .. import auth, db, mail_client
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
# Invite — v0.24.40: email + name + role only, no admin-set password.
# password_hash stays "" (auth.verify_password already rejects any
# login attempt against an empty hash) until the invitee completes
# /invite/{token}, which is how the pending-invite state is detected
# everywhere (users/list.html badge, resend button) — no extra column.
# ---------------------------------------------------------------------------

def _build_invite_url(request: Request, token: str) -> str:
    return str(request.base_url).rstrip("/") + "/invite/" + token


def _send_invite_mail(to_email: str, name: str, invite_url: str,
                      lang: str) -> tuple[bool, str]:
    """Returns (sent, error). sent=False + error="" means mail simply
    isn't configured (not a failure) — the caller shows the raw link
    instead. sent=False + a non-empty error means mail IS configured
    but the send itself failed."""
    cfg = mail_client.load_config()
    if cfg["provider"] == "disabled":
        return False, ""
    subject = i18n.t("user.invite.mail_subject", lang)
    greeting = name or to_email
    html = (
        '<!doctype html><html><body style="font-family:Arial,sans-serif;">'
        f'<h2 style="color:#002854;">{subject}</h2>'
        f'<p>{i18n.t("user.invite.mail_greeting", lang)} {greeting},</p>'
        f'<p>{i18n.t("user.invite.mail_body", lang)}</p>'
        f'<p><a href="{invite_url}" style="display:inline-block;background:#002854;'
        'color:#fff;padding:10px 18px;border-radius:6px;text-decoration:none;">'
        f'{i18n.t("user.invite.mail_button", lang)}</a></p>'
        f'<p style="color:#8094AA;font-size:0.85rem;">{invite_url}</p>'
        '</body></html>'
    )
    text = f'{i18n.t("user.invite.mail_body", lang)}\n\n{invite_url}'
    try:
        mail_client.send([to_email], subject, html, text, config=cfg)
        return True, ""
    except mail_client.MailSendError as e:
        return False, str(e)


def _issue_invite(request: Request, admin: dict, user_id: int,
                  email: str, name: str, lang: str):
    token = auth.sign_magic_token({"user_id": user_id}, salt=auth.INVITE_TOKEN_SALT)
    invite_url = _build_invite_url(request, token)
    sent, mail_error = _send_invite_mail(email, name, invite_url, lang)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        "users/invite_sent.html",
        {
            "request": request, "lang": request.state.lang, "user": admin,
            "target_user_id": user_id, "email": email,
            "invite_url": invite_url,
            "mail_sent": sent, "mail_error": mail_error,
        },
    )


@router.get("/users/invite", response_class=HTMLResponse, include_in_schema=False)
async def user_invite_form(request: Request):
    admin = auth.require_admin(request)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        "users/invite.html",
        {
            "request": request, "lang": request.state.lang, "user": admin,
            "form": {"email": "", "name": "", "role": "technician"},
            "error": None,
        },
    )


@router.post("/users/invite", include_in_schema=False)
async def user_invite_submit(request: Request,
                             email: str = Form(""),
                             name: str = Form(""),
                             role: str = Form("technician")):
    admin = auth.require_admin(request)
    templates = request.app.state.templates
    ui_lang = request.state.lang

    email = email.strip().lower()
    name = name.strip()
    role = role if role in ("admin", "technician") else "technician"

    err = None
    if not email:
        err = i18n.t("user.error.email_required", ui_lang)
    elif "@" not in email:
        err = i18n.t("user.error.email_invalid", ui_lang)
    elif db.find_user_by_email(email) is not None:
        err = i18n.t("user.error.email_taken", ui_lang)

    if err:
        return templates.TemplateResponse(
            "users/invite.html",
            {
                "request": request, "lang": ui_lang, "user": admin,
                "form": {"email": email, "name": name, "role": role},
                "error": err,
            },
            status_code=400,
        )

    with db.get_conn() as conn:
        result = conn.execute(insert(users).values(
            email=email, password_hash="", name=name, role=role,
            lang=ui_lang, active=1,
        ))
        new_id = result.inserted_primary_key[0]
    db.audit(admin["id"], "user.invited",
             target_type="user", target_id=str(new_id),
             meta_json=json.dumps({"email": email, "role": role}))

    return _issue_invite(request, admin, new_id, email, name, ui_lang)


@router.post("/users/{user_id}/invite/resend", include_in_schema=False)
async def user_invite_resend(user_id: int, request: Request):
    admin = auth.require_admin(request)
    row = _user_row(user_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    if row["password_hash"]:
        # Already completed — nothing to resend, just go back.
        return RedirectResponse("/users", status_code=303)
    db.audit(admin["id"], "user.invite_resent",
             target_type="user", target_id=str(user_id))
    lang = row["lang"] if row["lang"] in i18n.SUPPORTED_LANGS else request.state.lang
    return _issue_invite(request, admin, user_id, row["email"], row["name"], lang)


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
                           active: str = Form(""),
                           printix_tenants_access: str = Form("")):
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
                    "active": 1 if active in ("1", "on", "true", "yes") else 0,
                    "printix_tenants_access":
                        1 if printix_tenants_access in ("1", "on", "true", "yes") else 0}
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
