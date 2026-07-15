"""Session-based authentication, password hashing and access checks.

* Session cookie signed with ``SESSION_SECRET`` (derived from ``FERNET_KEY``
  if unset — that keeps single-file deployments zero-config while still
  allowing operators to rotate the two secrets independently).
* Passwords hashed with bcrypt (cost 12).
* Magic-link tokens for order-close links are signed with the same secret
  and time-boxed via ``itsdangerous``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import sqlite3
from typing import Any

import bcrypt
from fastapi import HTTPException, Request, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from . import db


BCRYPT_ROUNDS = 12
MAGIC_LINK_TTL_SECONDS = 60 * 60 * 24 * 30  # 30 days


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------

def session_secret() -> str:
    """Return the session signing key.

    If the operator did not set ``SESSION_SECRET`` explicitly, derive one
    deterministically from ``FERNET_KEY`` so the cookies survive restarts
    without extra configuration.
    """
    explicit = os.environ.get("SESSION_SECRET", "").strip()
    if explicit:
        return explicit
    fernet = os.environ.get("FERNET_KEY", "").strip()
    if not fernet:
        raise RuntimeError("Neither SESSION_SECRET nor FERNET_KEY is set.")
    digest = hashlib.sha256(("session::" + fernet).encode()).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii")


def _serializer(salt: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret_key=session_secret(), salt=salt)


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------

def hash_password(plaintext: str) -> str:
    if not plaintext:
        raise ValueError("empty password")
    return bcrypt.hashpw(plaintext.encode("utf-8"),
                         bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode("ascii")


def verify_password(plaintext: str, stored_hash: str) -> bool:
    if not plaintext or not stored_hash:
        return False
    try:
        return bcrypt.checkpw(plaintext.encode("utf-8"),
                              stored_hash.encode("ascii"))
    except ValueError:
        return False


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


# ---------------------------------------------------------------------------
# Magic-link tokens (used for "mark as ordered" links in alert emails)
# ---------------------------------------------------------------------------

def sign_magic_token(payload: dict[str, Any], *, salt: str = "magic-link") -> str:
    return _serializer(salt).dumps(payload)


def verify_magic_token(token: str, *, max_age: int = MAGIC_LINK_TTL_SECONDS,
                       salt: str = "magic-link") -> dict[str, Any] | None:
    try:
        return _serializer(salt).loads(token, max_age=max_age)
    except (SignatureExpired, BadSignature):
        return None


# ---------------------------------------------------------------------------
# Request-side helpers (FastAPI dependencies)
# ---------------------------------------------------------------------------

def current_user(request: Request) -> sqlite3.Row | None:
    """Resolve the logged-in user for the current request, or return None."""
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    row = db.find_user_by_id(int(user_id))
    if row is None or not row["active"]:
        request.session.pop("user_id", None)
        return None
    return row


def require_user(request: Request) -> sqlite3.Row:
    user = current_user(request)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="login_required")
    return user


def require_admin(request: Request) -> sqlite3.Row:
    user = require_user(request)
    if user["role"] != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="admin_required")
    return user


def user_can_see_customer(user: sqlite3.Row, customer_id: int) -> bool:
    """Admins see all customers; technicians only those with an access row."""
    if user["role"] == "admin":
        return True
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM customer_access "
            "WHERE user_id = ? AND customer_id = ?",
            (user["id"], customer_id),
        ).fetchone()
    return row is not None


def require_customer_access(request: Request, customer_id: int) -> sqlite3.Row:
    user = require_user(request)
    if not user_can_see_customer(user, customer_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            detail="customer_access_denied")
    return user


def visible_customer_ids(user: sqlite3.Row) -> list[int]:
    """Return the customer ids this user is allowed to see."""
    with db.get_conn() as conn:
        if user["role"] == "admin":
            rows = conn.execute("SELECT id FROM customers WHERE active = 1").fetchall()
        else:
            rows = conn.execute(
                "SELECT c.id FROM customers c "
                "JOIN customer_access a ON a.customer_id = c.id "
                "WHERE a.user_id = ? AND c.active = 1",
                (user["id"],),
            ).fetchall()
    return [r["id"] for r in rows]
