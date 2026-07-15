"""FastAPI application factory.

Wires middleware, static assets, routers and Jinja templates. Kept small
so that phase-specific routers (customers, dashboard, alerts, orders,
views) plug in cleanly by importing this factory.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .. import auth, db
from . import auth_routes, i18n
from .lang import LanguageMiddleware


ASSETS_DIR = Path(__file__).parent / "assets"
TEMPLATES_DIR = Path(__file__).parent / "templates"


def create_app() -> FastAPI:
    """Build and return the FastAPI application."""

    # Fail loudly at import time if translations are incomplete.
    i18n.check_translations()

    # Ensure DB schema is present before the first request.
    db.init_schema()

    app = FastAPI(
        title="TonerWatch",
        docs_url=None,        # No public API docs surface — internal tool.
        redoc_url=None,
        openapi_url=None,
    )

    app.add_middleware(
        SessionMiddleware,
        secret_key=auth.session_secret(),
        session_cookie="tonerwatch_session",
        max_age=60 * 60 * 24 * 30,   # 30 days
        same_site="lax",
        https_only=False,            # Set True in production via reverse proxy.
    )
    app.add_middleware(LanguageMiddleware)

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
    templates.env.globals["APP_NAME"] = "TonerWatch"
    templates.env.globals["LANG_LABELS"] = i18n.LANG_LABELS
    templates.env.globals["SUPPORTED_LANGS"] = i18n.SUPPORTED_LANGS

    app.state.templates = templates

    # Routers.
    app.include_router(auth_routes.router)

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
