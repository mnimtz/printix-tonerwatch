"""Language resolution middleware.

Resolves the UI language for every request in this order:

    1. ``?lang=xx`` query parameter → persisted to the session
    2. ``lang`` on the session cookie
    3. User's stored ``users.lang`` preference (if logged in)
    4. ``Accept-Language`` header, matched against the EFIGS set
    5. ``DEFAULT_LANG`` environment variable (defaults to 'en')

The resolved value is exposed as ``request.state.lang`` and threaded into
every Jinja template via a context processor in ``app.py``.
"""

from __future__ import annotations

import os
import re

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from .i18n import DEFAULT_LANG, SUPPORTED_LANGS


_ACCEPT_LANG_RE = re.compile(
    r"([a-zA-Z]{2,3})(?:-[a-zA-Z0-9]+)?\s*(?:;\s*q\s*=\s*([0-9.]+))?"
)


def parse_accept_language(header: str) -> str | None:
    """Return the highest-quality language from the header that we support.

    ``Accept-Language: de-DE,de;q=0.9,en;q=0.8`` → ``'de'``
    ``Accept-Language: pt-BR,en;q=0.5``           → ``'en'``
    ``Accept-Language: pt-BR``                    → ``None``
    """
    if not header:
        return None
    ranked: list[tuple[float, str]] = []
    for match in _ACCEPT_LANG_RE.finditer(header):
        code = match.group(1).lower()
        try:
            weight = float(match.group(2)) if match.group(2) else 1.0
        except ValueError:
            weight = 1.0
        if code in SUPPORTED_LANGS:
            ranked.append((weight, code))
    if not ranked:
        return None
    ranked.sort(key=lambda p: -p[0])
    return ranked[0][1]


def _fallback_lang() -> str:
    env = os.environ.get("DEFAULT_LANG", DEFAULT_LANG).lower()
    return env if env in SUPPORTED_LANGS else DEFAULT_LANG


class LanguageMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        lang = self._resolve(request)
        request.state.lang = lang
        try:
            request.session["lang"] = lang
        except AssertionError:
            # Session middleware missing in a very narrow set of routes;
            # falling through with request.state.lang set is fine.
            pass
        response: Response = await call_next(request)
        return response

    def _resolve(self, request: Request) -> str:
        query = request.query_params.get("lang", "").lower()
        if query in SUPPORTED_LANGS:
            return query

        try:
            session_lang = request.session.get("lang", "")
        except AssertionError:
            session_lang = ""
        if session_lang in SUPPORTED_LANGS:
            return session_lang

        try:
            user_id = request.session.get("user_id")
        except AssertionError:
            user_id = None
        if user_id:
            # Late import — avoids a circular reference at module load.
            from .. import db
            row = db.find_user_by_id(int(user_id))
            if row is not None and row["lang"] in SUPPORTED_LANGS:
                return row["lang"]

        header = request.headers.get("accept-language", "")
        picked = parse_accept_language(header)
        if picked:
            return picked

        return _fallback_lang()
