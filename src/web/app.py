"""FastAPI application factory.

Wires middleware, static assets, routers and Jinja templates. Kept small
so that phase-specific routers (customers, dashboard, alerts, orders,
views) plug in cleanly by importing this factory.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from .. import auth, db
from . import access_routes, auth_routes, customer_routes, i18n, user_routes
from .lang import LanguageMiddleware


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name, "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Attach a small set of hardening response headers on every request.

    * Content-Security-Policy — allow inline styles + scripts (we ship
      them inline in base.html for zero-latency rendering), Bunny CDN
      for the Red Hat Display webfont, self for everything else.
    * X-Content-Type-Options — stop MIME sniffing.
    * X-Frame-Options — deny framing (no legitimate embed use case).
    * Referrer-Policy — send origin only cross-site.
    * Permissions-Policy — deny sensor / device APIs that we never use.
    """
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        headers = response.headers
        headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline' https://fonts.bunny.net; "
            "font-src 'self' https://fonts.bunny.net data:; "
            "img-src 'self' data: blob:; "
            "connect-src 'self'; "
            "frame-ancestors 'none'; "
            "base-uri 'self'; "
            "form-action 'self'"
        )
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        headers.setdefault(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=(), payment=()",
        )
        return response


ASSETS_DIR = Path(__file__).parent / "assets"
TEMPLATES_DIR = Path(__file__).parent / "templates"


def create_app() -> FastAPI:
    """Build and return the FastAPI application."""

    # Fail loudly at import time if translations are incomplete.
    i18n.check_translations()

    # Ensure DB schema is present before the first request.
    db.init_schema()

    app = FastAPI(
        title="Printix TonerWatch",
        docs_url=None,        # No public API docs surface — internal tool.
        redoc_url=None,
        openapi_url=None,
    )

    # Session cookie is Secure by default. Override with
    # SESSION_HTTPS_ONLY=false when running behind a plain-HTTP dev
    # environment (docker compose on localhost, for instance).
    app.add_middleware(
        SessionMiddleware,
        secret_key=auth.session_secret(),
        session_cookie="tonerwatch_session",
        max_age=60 * 60 * 24 * 30,   # 30 days
        same_site="lax",
        https_only=_env_bool("SESSION_HTTPS_ONLY", True),
    )
    app.add_middleware(LanguageMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)

    # Static assets — logo, favicon, printer pictograms.
    app.mount(
        "/assets",
        StaticFiles(directory=str(ASSETS_DIR)),
        name="assets",
    )

    # Templates — single Jinja env used across every router.
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    # Expose translation function + brand metadata to every template.
    def _t(key: str, lang: str | None = None) -> str:
        return i18n.t(key, lang or i18n.DEFAULT_LANG)

    templates.env.globals["_"] = _t
    templates.env.globals["APP_NAME"] = "Printix TonerWatch"
    templates.env.globals["LANG_LABELS"] = i18n.LANG_LABELS
    templates.env.globals["SUPPORTED_LANGS"] = i18n.SUPPORTED_LANGS

    app.state.templates = templates

    # Routers.
    app.include_router(auth_routes.router)
    app.include_router(customer_routes.router)
    app.include_router(user_routes.router)
    app.include_router(access_routes.router)

    # Root → dashboard when logged in, otherwise setup/login.
    @app.get("/", include_in_schema=False)
    async def root(request: Request):
        if db.user_count() == 0:
            return RedirectResponse("/setup", status_code=303)
        if auth.current_user(request) is None:
            return RedirectResponse("/login", status_code=303)
        return RedirectResponse("/dashboard", status_code=303)

    # Placeholder — real dashboard lands in P2. Keeps the root redirect
    # from breaking during the P0/P1 preview.
    @app.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
    async def dashboard_stub(request: Request):
        user = auth.require_user(request)
        return templates.TemplateResponse(
            "coming_soon.html",
            {
                "request": request,
                "lang": request.state.lang,
                "user": dict(user),
                "phase": "P2",
                "title_key": "nav.dashboard",
            },
        )

    # Health check for Azure App Service liveness probes.
    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return JSONResponse({"status": "ok"})

    return app
