"""ASGI entry point.

``uvicorn src.server:app`` — no factory pattern needed at the CLI layer;
:func:`web.app.create_app` is a plain call that runs at import time.
"""

from __future__ import annotations

from .web.app import create_app


app = create_app()
