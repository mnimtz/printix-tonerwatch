"""Login, logout, first-run admin setup and language switcher."""

from __future__ import annotations

import asyncio
import logging
import time
from urllib.parse import quote_plus

from fastapi import APIRouter, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import insert, update

from .. import auth, db, entra_sso
from ..db import users
from . import i18n

_entra_log = logging.getLogger("tonerwatch.entra")


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
async def switch_language_get(request: Request, lang: str = "en", next: str = "/"):
    """v0.18: /language is now POST-only for state changes.
    Kept as GET for backward compat with old bookmarks — but this
    doesn't write anything, it renders a tiny auto-submit form so the
    change goes via a proper CSRF-protected POST."""
    templates = request.app.state.templates
    if lang not in i18n.SUPPORTED_LANGS:
        lang = i18n.DEFAULT_LANG
    return templates.TemplateResponse(
        "language_redirect.html",
        {"request": request, "lang": request.state.lang,
         "target_lang": lang, "next": _safe_next(next)},
    )


@router.post("/language", include_in_schema=False)
async def switch_language(request: Request):
    form = await request.form()
    lang = (form.get("lang") or "").strip()
    next_ = form.get("next") or "/"
    if lang in i18n.SUPPORTED_LANGS:
        request.session["lang"] = lang
        user = auth.current_user(request)
        if user is not None:
            with db.get_conn() as conn:
                conn.execute(
                    update(users).where(users.c.id == user["id"]).values(lang=lang)
                )
    return RedirectResponse(_safe_next(next_), status_code=303)


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
    # v0.18.6 — surface ?error=… and ?info=… from the query string
    # so that SSO/callback failures (which redirect here with a
    # message) actually render. Prior versions dropped them on the
    # floor and Marcus's browser looked like a silent loop.
    q_error = (request.query_params.get("error") or "").strip()[:400]
    q_info  = (request.query_params.get("info") or "").strip()[:400]
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "lang": request.state.lang,
         "error": q_error or None,
         "info":  q_info  or None,
         "sso_configured": entra_sso.is_configured(),
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
             "sso_configured": entra_sso.is_configured(),
             "form": {"email": email, "next": next_path}},
            status_code=401,
        )

    _reset_login_throttle(request, email)
    # v0.17.1: drop any pre-login state (e.g. entra_state, matrix_pending)
    # before setting user_id, so a fixation attempt via a shared pre-auth
    # session dies here. Language pref survives — set again below.
    saved_lang = request.session.get("lang", "")
    request.session.clear()
    request.session["user_id"] = user["id"]
    if user["lang"] in i18n.SUPPORTED_LANGS:
        request.session["lang"] = user["lang"]
    elif saved_lang in i18n.SUPPORTED_LANGS:
        request.session["lang"] = saved_lang
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


# ---------------------------------------------------------------------------
# Entra SSO — kick off the OAuth2 code flow + handle Microsoft's callback
# ---------------------------------------------------------------------------

@router.get("/auth/entra/login", include_in_schema=False)
async def entra_login(request: Request, next: str = "/dashboard"):
    """Redirect the browser to Microsoft's /authorize.

    v0.18.5 — the previous try/except only caught EntraSSOError.
    MSAL itself can raise ValueError (bad authority URL from an
    empty/whitespace tenant_id), ImportError (msal not in the venv),
    or plain requests-side errors when it hydrates the tenant
    metadata. Any of those became a 500 that told the user
    "Internal Server Error" and nothing more. Catch broadly, log
    with full traceback for Azure Log Stream, then redirect to
    /login with a readable message."""
    if not entra_sso.is_configured():
        _entra_log.warning(
            "[Entra login] is_configured() returned False — check the "
            "diagnose page for which of the four required flags is missing")
        return RedirectResponse(
            "/login?error=" + quote_plus(
                "SSO not fully configured — see /settings/entra/diagnose"),
            status_code=303)
    try:
        url = entra_sso.build_auth_url(request, _safe_next(next))
    except entra_sso.EntraSSOError as e:
        _entra_log.warning("[Entra login] build_auth_url raised: %s", e)
        return RedirectResponse(
            "/login?error=" + quote_plus(f"SSO: {str(e)[:200]}"),
            status_code=303)
    except Exception as e:  # noqa: BLE001
        _entra_log.exception(
            "[Entra login] unexpected exception — check redirect_uri, "
            "tenant_id, and client_secret. Falling back to /login "
            "with visible error."
        )
        # Type + message so Marcus can tell "invalid_client" apart from
        # "no msal package" without having to open the log.
        return RedirectResponse(
            "/login?error=" + quote_plus(
                f"SSO ({type(e).__name__}): {str(e)[:200]}"),
            status_code=303)
    return RedirectResponse(url, status_code=303)


@router.get("/auth/entra/callback", include_in_schema=False)
async def entra_callback(request: Request,
                         code: str = "", state: str = "",
                         error: str = "", error_description: str = ""):
    """Handle the OAuth2 callback. Log the user in or send them back
    to /login with a descriptive message.

    v0.18.4 — surface errors as ?error=... URL params via a 303 so
    the message survives a browser hitting the SSO button again.
    Template-with-401 was invisible in production when the browser
    (or Azure Front Door) automatically re-navigated straight back
    to /auth/entra/login, making the loop look silent."""
    if error:
        msg = (error_description or error)[:200]
        _entra_log.warning("[Entra callback] Microsoft error: %s (%s)",
                            error, error_description[:200] if error_description else "")
        return RedirectResponse(
            "/login?error=" + quote_plus(f"Microsoft: {msg}"),
            status_code=303)
    if not code or not state:
        _entra_log.warning("[Entra callback] missing code/state — "
                           "code_len=%d state_len=%d", len(code), len(state))
        return RedirectResponse(
            "/login?error=" + quote_plus("SSO callback missing code or state"),
            status_code=303)

    try:
        claims = entra_sso.handle_callback(request, code, state)
    except entra_sso.EntraSSOError as e:
        _entra_log.warning("[Entra callback] token exchange failed: %s", e)
        return RedirectResponse(
            "/login?error=" + quote_plus(f"SSO: {str(e)[:200]}"),
            status_code=303)

    user = entra_sso.resolve_or_create_user(claims)
    if user is None or not user["active"]:
        _entra_log.warning(
            "[Entra callback] no local account matches email=%r oid=%r "
            "(auto-provision: %s). Options: (a) create a local user with "
            "the same email in /users, (b) enable auto-provisioning in "
            "settings, or (c) sign the tenant's email to an existing "
            "user's `entra_oid` column directly.",
            claims.get("email", ""), claims.get("oid", ""),
            entra_sso.load_config().get("allow_auto_provision", False))
        return RedirectResponse(
            "/login?error=" + quote_plus(
                i18n.t("auth.sso_no_local_account", request.state.lang)
                + f" (email={claims.get('email', '')})"),
            status_code=303)

    # Log the user in — same session shape as the local login path.
    # v0.17.1: read+clear+restore, same rationale as local login.
    saved_lang = request.session.get("lang", "")
    saved_next = request.session.get("entra_next", "/dashboard")
    request.session.clear()
    request.session["user_id"] = user["id"]
    if user["lang"] in i18n.SUPPORTED_LANGS:
        request.session["lang"] = user["lang"]
    elif saved_lang in i18n.SUPPORTED_LANGS:
        request.session["lang"] = saved_lang

    # Redirect to the URL we stashed at the start of the flow (or
    # dashboard as a safe default).
    next_url = _safe_next(saved_next)

    db.audit(user["id"], "user.login.entra_sso",
             target_type="user", target_id=str(user["id"]))
    return RedirectResponse(next_url, status_code=303)
