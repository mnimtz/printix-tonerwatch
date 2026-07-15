"""Login, logout, first-run admin setup and language switcher."""

from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import insert, update

from .. import auth, db
from ..db import users
from . import i18n


router = APIRouter()


# ---------------------------------------------------------------------------
# Very small in-memory login throttle
# ---------------------------------------------------------------------------
# Not a replacement for a real rate limiter — that comes with fastapi-limiter
# or a reverse-proxy WAF — but this fold-in stops the trivial brute-force
# case (one attacker hammering /login) without any external dependency.
#
# Bookkeeping is per (client ip, email) tuple so a single bad email can't
# lock out an entire tenant, and per-IP + per-email combined so many
# users behind a shared NAT don't lock each other out either.
_LOGIN_FAILS: dict[tuple[str, str], list[float]] = {}
_LOGIN_WINDOW_SECONDS = 60 * 5   # rolling 5 min window
_LOGIN_MAX_ATTEMPTS = 8          # after N fails in the window → back-off
_LOGIN_BACKOFF_BASE = 0.5        # seconds; multiplied by (fails - MAX)


def _login_client_key(request: Request, email: str) -> tuple[str, str]:
    fwd = request.headers.get("x-forwarded-for", "")
    ip = fwd.split(",")[0].strip() if fwd else (request.client.host
                                                if request.client else "-")
    return (ip, email.lower())


async def _throttle_login_failure(request: Request, email: str) -> None:
    key = _login_client_key(request, email)
    now = time.time()
    hits = [t for t in _LOGIN_FAILS.get(key, []) if now - t < _LOGIN_WINDOW_SECONDS]
    hits.append(now)
    _LOGIN_FAILS[key] = hits
    if len(hits) > _LOGIN_MAX_ATTEMPTS:
        # Exponential back-off up to a hard cap so the request doesn't
        # tie up a worker for too long even under attack.
        delay = min(_LOGIN_BACKOFF_BASE * (2 ** (len(hits) - _LOGIN_MAX_ATTEMPTS)),
                    30.0)
        await asyncio.sleep(delay)


def _reset_login_throttle(request: Request, email: str) -> None:
    _LOGIN_FAILS.pop(_login_client_key(request, email), None)


# --------------------------------------------------------------------------
# Language switcher — POST /language?lang=xx&next=/path
# --------------------------------------------------------------------------

@router.get("/language", include_in_schema=False)
async def switch_language(request: Request, lang: str = "en", next: str = "/"):
    if lang in i18n.SUPPORTED_LANGS:
        request.session["lang"] = lang
        user = auth.current_user(request)
        if user is not None:
            with db.get_conn() as conn:
                conn.execute(
                    update(users).where(users.c.id == user["id"]).values(lang=lang)
                )
    if not next.startswith("/"):
        next = "/"
    return RedirectResponse(next, status_code=303)


# --------------------------------------------------------------------------
# First-run setup wizard
# --------------------------------------------------------------------------

@router.get("/setup", response_class=HTMLResponse, include_in_schema=False)
async def setup_form(request: Request):
    templates = request.app.state.templates
    if db.user_count() > 0:
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "lang": request.state.lang,
                "info": i18n.t("setup.already_configured", request.state.lang),
            },
            status_code=200,
        )
    return templates.TemplateResponse(
        "setup_first_admin.html",
        {"request": request, "lang": request.state.lang, "error": None,
         "form": {"name": "", "email": ""}},
    )


@router.post("/setup", response_class=HTMLResponse, include_in_schema=False)
async def setup_submit(request: Request,
                       name: str = Form(""),
                       email: str = Form(""),
                       password: str = Form(""),
                       password_confirm: str = Form("")):
    templates = request.app.state.templates
    lang = request.state.lang

    if db.user_count() > 0:
        # Race: someone else finished the wizard first. Send them to login.
        return RedirectResponse("/login", status_code=303)

    name = name.strip()
    email = email.strip().lower()
    error: str | None = None

    if not name or not email or not password:
        error = i18n.t("common.error", lang)
    elif len(password) < 12:
        error = i18n.t("setup.password_too_short", lang)
    elif password != password_confirm:
        error = i18n.t("setup.password_mismatch", lang)

    if error:
        return templates.TemplateResponse(
            "setup_first_admin.html",
            {"request": request, "lang": lang, "error": error,
             "form": {"name": name, "email": email}},
            status_code=400,
        )

    password_hash = auth.hash_password(password)
    with db.get_conn() as conn:
        result = conn.execute(
            insert(users).values(
                email=email, password_hash=password_hash, name=name,
                role="admin", lang=lang, active=1,
            )
        )
        new_id = result.inserted_primary_key[0]
    db.audit(new_id, "user.first_admin_created",
             target_type="user", target_id=str(new_id))

    request.session["user_id"] = new_id
    return RedirectResponse("/dashboard", status_code=303)


# --------------------------------------------------------------------------
# Login / logout
# --------------------------------------------------------------------------

@router.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_form(request: Request, next: str = "/dashboard"):
    templates = request.app.state.templates
    if db.user_count() == 0:
        return RedirectResponse("/setup", status_code=303)
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "lang": request.state.lang,
         "error": None, "info": None,
         "form": {"email": "", "next": _safe_next(next)}},
    )


def _safe_next(value: str | None) -> str:
    """Only allow same-origin relative paths — no `//evil.com` open-redirect."""
    if not value or not value.startswith("/") or value.startswith("//"):
        return "/dashboard"
    return value


@router.post("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_submit(request: Request,
                       email: str = Form(""),
                       password: str = Form(""),
                       next: str = Form("")):
    templates = request.app.state.templates
    lang = request.state.lang
    email = email.strip().lower()
    next_path = _safe_next(next or request.query_params.get("next", ""))

    user = db.find_user_by_email(email) if email else None
    if user is None or not user["active"] or \
            not auth.verify_password(password, user["password_hash"]):
        await _throttle_login_failure(request, email)
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "lang": lang,
             "error": i18n.t("auth.invalid_credentials", lang),
             "info": None,
             "form": {"email": email, "next": next_path}},
            status_code=401,
        )

    _reset_login_throttle(request, email)
    request.session["user_id"] = user["id"]
    if user["lang"] in i18n.SUPPORTED_LANGS:
        request.session["lang"] = user["lang"]
    db.touch_last_login(user["id"])
    db.audit(user["id"], "user.login",
             target_type="user", target_id=str(user["id"]))
    return RedirectResponse(next_path, status_code=303)


@router.post("/logout", include_in_schema=False)
async def logout(request: Request):
    user_id = request.session.get("user_id")
    request.session.clear()
    if user_id:
        db.audit(int(user_id), "user.logout",
                 target_type="user", target_id=str(user_id))
    return RedirectResponse("/login", status_code=303)


@router.get("/logout", include_in_schema=False)
async def logout_get():
    # Some clients still follow GET; treat it the same way but discourage
    # in-app usage — the base template posts a form.
    raise HTTPException(status.HTTP_405_METHOD_NOT_ALLOWED)
