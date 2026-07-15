"""Login, logout, first-run admin setup and language switcher."""

from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import auth, db
from . import i18n


router = APIRouter()


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
                conn.execute("UPDATE users SET lang = ? WHERE id = ?",
                             (lang, user["id"]))
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
        cur = conn.execute(
            "INSERT INTO users(email, password_hash, name, role, lang, active) "
            "VALUES (?,?,?,?,?,1)",
            (email, password_hash, name, "admin", lang),
        )
        new_id = cur.lastrowid
    db.audit(new_id, "user.first_admin_created",
             target_type="user", target_id=str(new_id))

    request.session["user_id"] = new_id
    return RedirectResponse("/dashboard", status_code=303)


# --------------------------------------------------------------------------
# Login / logout
# --------------------------------------------------------------------------

@router.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_form(request: Request):
    templates = request.app.state.templates
    if db.user_count() == 0:
        return RedirectResponse("/setup", status_code=303)
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "lang": request.state.lang,
         "error": None, "info": None, "form": {"email": ""}},
    )


@router.post("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_submit(request: Request,
                       email: str = Form(""),
                       password: str = Form("")):
    templates = request.app.state.templates
    lang = request.state.lang
    email = email.strip().lower()

    user = db.find_user_by_email(email) if email else None
    if user is None or not user["active"] or \
            not auth.verify_password(password, user["password_hash"]):
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "lang": lang,
             "error": i18n.t("auth.invalid_credentials", lang),
             "info": None,
             "form": {"email": email}},
            status_code=401,
        )

    request.session["user_id"] = user["id"]
    if user["lang"] in i18n.SUPPORTED_LANGS:
        request.session["lang"] = user["lang"]
    db.touch_last_login(user["id"])
    db.audit(user["id"], "user.login",
             target_type="user", target_id=str(user["id"]))
    return RedirectResponse("/dashboard", status_code=303)


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
