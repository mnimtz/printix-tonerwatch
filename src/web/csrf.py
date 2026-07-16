"""CSRF protection — per-session signed token, form + header double-submit.

Design
------

* A random 32-byte token lives in the session (``session["csrf_token"]``)
  from the very first request the browser makes. If the session key is
  missing (fresh visitor, expired session), the middleware generates
  and stores one, so every rendered GET has a token to embed.

* Every state-changing method (POST / PUT / PATCH / DELETE) must
  present the same token — either as a form field named
  ``csrf_token`` or as an ``X-CSRF-Token`` request header. Constant-
  time compare via ``hmac.compare_digest``.

* Templates access the token via ``request.state.csrf_token``.

Whitelist
---------

Some POST routes cannot carry a session-bound CSRF token by design:

* ``/orders/action/{token}/confirm`` — the magic-link token in the
  URL path IS the credential; no session is expected.
* ``/healthz`` — infra probes.

The middleware skips CSRF verification for those paths but still
sets the token on the response's session, so the next authenticated
request has one ready.
"""

from __future__ import annotations

import hmac
import logging
import secrets

from urllib.parse import parse_qs

from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send


logger = logging.getLogger(__name__)

_SESSION_KEY = "csrf_token"
_FORM_FIELD  = "csrf_token"
_HEADER_NAME = "x-csrf-token"

# Methods that mutate — everything else is safe by convention (RFC 7231).
_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# Paths where CSRF verification would be nonsensical or impossible.
# Prefix-match — every subpath is exempt too.
_EXEMPT_PREFIXES = (
    "/orders/action/",   # magic-link handler — token in URL IS the auth
    "/healthz",
)


def _ensure_token(request: Request) -> str:
    """Return the session's CSRF token, minting one on first access.
    Safe to call from anywhere with an active SessionMiddleware."""
    try:
        token = request.session.get(_SESSION_KEY, "")
    except AssertionError:
        # Session middleware missing (very early middleware or a route
        # that skipped it) — degrade quietly, callers get empty string.
        return ""
    if not token:
        token = secrets.token_urlsafe(32)
        try:
            request.session[_SESSION_KEY] = token
        except AssertionError:
            return ""
    return token


def _get_submitted(request: Request, form_field: str) -> str:
    """Look for the submitted token: header first (cheap), then form.
    We don't read the body here — the route handler will re-read it
    via `await request.form()` and that would consume the receive
    stream. Instead we rely on Starlette caching the form on
    Request.state — but pre-body-read, the header is the only reliable
    source. So this returns the header if present; otherwise the
    middleware falls back to parsing the form itself."""
    hdr = request.headers.get(_HEADER_NAME, "").strip()
    if hdr:
        return hdr
    return ""


class CSRFMiddleware:
    """Pure-ASGI middleware — sets ``request.state.csrf_token`` on
    every request, verifies the token on POST/PUT/PATCH/DELETE.

    Written as a pure-ASGI middleware (not BaseHTTPMiddleware) so
    that reading + replaying the request body doesn't break
    downstream form parsing. BaseHTTPMiddleware's dispatch pattern
    consumes the ASGI receive stream via ``request.form()`` and any
    subsequent read (FastAPI's ``Form(...)`` dependency) sees an
    empty body — a known Starlette limitation.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "GET").upper()
        path   = scope.get("path", "")

        # Read session via a temporary Request. SessionMiddleware
        # has already run (added earlier / wrapped further out) so
        # scope["session"] holds the decoded dict.
        session = scope.get("session")
        if session is None:
            # Session middleware not in the chain — degrade cleanly.
            scope["state"] = dict(scope.get("state") or {})
            scope["state"]["csrf_token"] = ""
            await self.app(scope, receive, send)
            return

        # Ensure / expose the token.
        token = session.get(_SESSION_KEY, "")
        if not token:
            token = secrets.token_urlsafe(32)
            session[_SESSION_KEY] = token
        # Expose to templates via request.state (initialised by Starlette
        # into scope["state"]).
        state = scope.get("state") or {}
        state["csrf_token"] = token
        scope["state"] = state

        # Skip check for safe methods and exempt paths.
        if method not in _UNSAFE_METHODS or any(
                path.startswith(p) for p in _EXEMPT_PREFIXES):
            await self.app(scope, receive, send)
            return

        # Check header first — no body read needed.
        hdrs = Headers(scope=scope)
        submitted = hdrs.get(_HEADER_NAME, "").strip()
        body_bytes = b""
        buffered_body = False   # flip to True only if we consumed receive

        if not submitted:
            # No header — buffer the ENTIRE body once, verify the form
            # token, then replay the body downstream. This costs a
            # single copy but preserves FastAPI's Form(...) dependency.
            more = True
            while more:
                msg = await receive()
                if msg["type"] == "http.request":
                    body_bytes += msg.get("body", b"") or b""
                    more = msg.get("more_body", False)
                elif msg["type"] == "http.disconnect":
                    return  # client gone
            buffered_body = True

            content_type = hdrs.get("content-type", "").split(";", 1)[0].strip().lower()
            if content_type == "application/x-www-form-urlencoded":
                try:
                    parsed = parse_qs(body_bytes.decode("utf-8", "replace"),
                                       keep_blank_values=True)
                    vals = parsed.get(_FORM_FIELD) or []
                    submitted = (vals[0] if vals else "").strip()
                except Exception:
                    submitted = ""
            elif content_type == "multipart/form-data":
                # Do a minimal parse just for the csrf_token field.
                # `python-multipart` (via Starlette) can do this but
                # instantiating a proper parser here is heavy — instead
                # we do a naive substring scan for form-field boundaries.
                submitted = _extract_multipart_field(
                    body_bytes, hdrs.get("content-type", ""),
                    _FORM_FIELD)

        ok = bool(token) and bool(submitted) and hmac.compare_digest(token, submitted)
        if not ok:
            logger.info("CSRF verify failed: method=%s path=%s "
                        "token_len=%d submitted_len=%d",
                        method, path, len(token), len(submitted))
            accepts = hdrs.get("accept", "")
            if "application/json" in accepts or path.startswith("/settings/"):
                resp = JSONResponse(
                    {"ok": False, "error": "csrf_token_invalid"},
                    status_code=403)
            else:
                resp = PlainTextResponse(
                    "CSRF verification failed. Reload the page and try again.",
                    status_code=403)
            await resp(scope, receive, send)
            return

        # v0.18: only swap in a replay-receive when we actually
        # consumed the body (form path). Header path leaves the
        # receive stream untouched, so FastAPI's Form(...) reads
        # the ORIGINAL bytes cleanly.
        if buffered_body:
            body_to_replay = body_bytes
            sent = False

            async def replay_receive() -> dict:
                nonlocal sent
                if not sent:
                    sent = True
                    return {"type": "http.request", "body": body_to_replay,
                            "more_body": False}
                return await receive()

            await self.app(scope, replay_receive, send)
        else:
            await self.app(scope, receive, send)


def _extract_multipart_field(body: bytes, content_type: str,
                              field_name: str) -> str:
    """Minimal-cost extraction of one form-field from a
    multipart/form-data body. Returns empty string on any parse
    failure — callers treat that as "no token submitted"."""
    ct = content_type.lower()
    idx = ct.find("boundary=")
    if idx < 0:
        return ""
    boundary = content_type[idx + len("boundary="):].strip().strip('"')
    if not boundary:
        return ""
    marker = ("Content-Disposition: form-data; name=\""
              + field_name + "\"").encode("utf-8", "replace")
    pos = body.find(marker)
    if pos < 0:
        return ""
    # Value follows a blank line after the header
    hdr_end = body.find(b"\r\n\r\n", pos)
    if hdr_end < 0:
        return ""
    val_start = hdr_end + 4
    boundary_bytes = ("--" + boundary).encode("ascii")
    val_end = body.find(boundary_bytes, val_start)
    if val_end < 0:
        val_end = len(body)
    # Trim trailing \r\n before the boundary line
    raw = body[val_start:val_end].rstrip(b"\r\n")
    try:
        return raw.decode("utf-8", "replace").strip()
    except Exception:
        return ""
