"""FastAPI application factory.

Wires middleware, static assets, routers and Jinja templates. Kept small
so that phase-specific routers (customers, dashboard, alerts, orders,
views) plug in cleanly by importing this factory.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware
from urllib.parse import quote as _urlquote

from .. import auth, db, toner_alerts
from . import (access_routes, auth_routes, backup_routes, customer_routes,
               dashboard_routes, graph_routes, i18n, order_routes,
               printer_info_routes, report_routes, saved_view_routes,
               settings_routes, supplier_routes, supply_routes, toner_routes,
               user_routes)
from .lang import LanguageMiddleware


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name, "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


def _read_version() -> str:
    """Return the running app version — env var takes precedence (that's
    what the container entrypoint exports from /app/VERSION), local dev
    falls back to reading the VERSION file next to the source tree."""
    env = os.environ.get("APP_VERSION", "").strip()
    if env:
        return env
    for p in (Path(__file__).parent.parent.parent / "VERSION",
              Path("/app/VERSION")):
        try:
            return p.read_text(encoding="utf-8").strip()
        except OSError:
            continue
    return "dev"


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

    # Middleware order matters: Starlette wraps in add-order-REVERSED,
    # so the LAST add is the OUTERMOST. Request path we want:
    #   SecurityHeaders → Session → CSRF → Language → app
    # Rationale:
    #   * Session must run before CSRF (CSRF reads session).
    #   * Language must run after CSRF so any 403 from CSRF fires
    #     before we bother resolving a language, and the language
    #     middleware still has access to request.state.csrf_token
    #     via templates.
    # Add in reverse of that path (last-add = outermost).
    # SESSION_HTTPS_ONLY=false when running behind a plain-HTTP dev
    # environment (docker compose on localhost, for instance).
    from .csrf import CSRFMiddleware
    app.add_middleware(LanguageMiddleware)
    app.add_middleware(CSRFMiddleware)
    app.add_middleware(
        SessionMiddleware,
        secret_key=auth.session_secret(),
        session_cookie="tonerwatch_session",
        max_age=60 * 60 * 24 * 30,   # 30 days
        same_site="lax",
        https_only=_env_bool("SESSION_HTTPS_ONLY", True),
    )
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
    templates.env.globals["APP_VERSION"] = _read_version()
    templates.env.globals["LANG_LABELS"] = i18n.LANG_LABELS
    templates.env.globals["SUPPORTED_LANGS"] = i18n.SUPPORTED_LANGS

    # Printer-status label helper — used by the toner grid to turn raw
    # SNMP codes (NO_PAPER, LOW_TONER, ...) into badges with an icon
    # and a translated caption.
    from . import labels as _labels
    templates.env.globals["error_state_meta"]    = _labels.error_state_meta
    templates.env.globals["reported_state_meta"] = _labels.reported_state_meta
    templates.env.globals["is_hidden_state"]     = _labels.is_hidden_reported_state

    app.state.templates = templates

    # Routers.
    app.include_router(auth_routes.router)
    app.include_router(customer_routes.router)
    app.include_router(user_routes.router)
    app.include_router(access_routes.router)
    app.include_router(dashboard_routes.router)   # P2: real /dashboard
    app.include_router(toner_routes.router)       # P2: /toner grid + /toner/refresh
    app.include_router(settings_routes.router)    # P3: mail config + test-mail
    app.include_router(supply_routes.router)      # P4a: model templates + per-printer overrides
    app.include_router(supplier_routes.router)    # v0.24.14: vendor list + per-customer account details
    app.include_router(order_routes.router)       # P4b: kanban + magic-link handlers
    app.include_router(report_routes.router)      # v0.24.36: flexible reporting hub
    app.include_router(printer_info_routes.router)  # v0.9: per-printer metadata overrides
    app.include_router(backup_routes.router)      # v0.10: backup download + Azure Blob upload
    app.include_router(saved_view_routes.router)  # v0.11: saved filter presets on /toner
    app.include_router(graph_routes.router)       # v0.14: Copilot Connector admin

    # ── Alert runner (P3) ─────────────────────────────────────────────
    # Env-driven cadence: 0 disables the scheduler entirely (useful for
    # local dev + integration tests where we don't want background jobs).
    try:
        interval = int(os.environ.get("ALERT_INTERVAL_MINUTES", "15"))
    except (TypeError, ValueError):
        interval = 15
    app.state.alert_interval_minutes = interval
    toner_alerts.start_runner(interval_minutes=interval)

    # Root → dashboard when logged in, otherwise setup/login.
    @app.get("/", include_in_schema=False)
    async def root(request: Request):
        if db.user_count() == 0:
            return RedirectResponse("/setup", status_code=303)
        if auth.current_user(request) is None:
            return RedirectResponse("/login", status_code=303)
        return RedirectResponse("/dashboard", status_code=303)

    # Every sidebar entry now has a real route — no coming-soon stubs
    # remain. /dashboard (P2), /toner (P2), /orders (P4b), /customers
    # (P1), /supplies (P4a), /users (P1), /settings (P3).

    # Health check for Azure App Service liveness probes.
    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return JSONResponse({"status": "ok"})

    # ── HTML-aware exception handlers ────────────────────────────────
    # Default FastAPI returns raw JSON for HTTPException, which is
    # jarring in a browser context. Rewrite 401/403/404 to the right
    # user-facing surface when the client wants HTML.
    def _accepts_html(request: Request) -> bool:
        accept = request.headers.get("accept", "")
        return "text/html" in accept or "*/*" in accept and "application/json" not in accept

    @app.exception_handler(HTTPException)
    async def _http_exc(request: Request, exc: HTTPException):
        if not _accepts_html(request):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code,
                                headers=getattr(exc, "headers", None))

        lang = getattr(request.state, "lang", i18n.DEFAULT_LANG)
        user = auth.current_user(request)

        if exc.status_code == 401:
            # Not authenticated → send them to /login and come back here
            next_path = request.url.path
            if request.url.query:
                next_path += "?" + request.url.query
            return RedirectResponse(
                f"/login?next={_urlquote(next_path, safe='/?&=')}",
                status_code=303,
            )
        if exc.status_code in (403, 404):
            tmpl = "error_403.html" if exc.status_code == 403 else "error_404.html"
            return templates.TemplateResponse(
                tmpl,
                {
                    "request": request, "lang": lang,
                    "user": dict(user) if user else None,
                    "detail": exc.detail,
                },
                status_code=exc.status_code,
            )
        # Everything else: JSON, same as before
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    @app.exception_handler(404)
    async def _not_found(request: Request, exc: HTTPException):
        # Starlette raises a bare Not Found for unmatched routes — route
        # it through the same HTML-vs-JSON logic.
        return await _http_exc(request, HTTPException(status_code=404,
                                                     detail="not_found"))

    return app
