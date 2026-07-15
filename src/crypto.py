"""Fernet-based encryption for at-rest credentials.

The key is loaded from the ``FERNET_KEY`` environment variable, which the
container entrypoint auto-generates on first start and persists into
``/data/fernet.key``. Losing the key means losing the ability to decrypt
customer BI credentials — back up ``/data`` as a whole.
"""

from __future__ import annotations

import os
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken


class CryptoError(RuntimeError):
    pass


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    key = os.environ.get("FERNET_KEY", "").strip()
    if not key:
        raise CryptoError(
            "FERNET_KEY is not set. The entrypoint generates one on first "
            "start; if you run outside the container, export it manually."
        )
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, TypeError) as exc:
        raise CryptoError(f"FERNET_KEY is not a valid Fernet key: {exc}") from exc


def encrypt(plaintext: str) -> str:
    """Return a URL-safe base64 ciphertext for the given plaintext."""
    if plaintext is None:
        return ""
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(ciphertext: str) -> str:
    """Return the plaintext for a Fernet ciphertext, or ``""`` on empty input."""
    if not ciphertext:
        return ""
    try:
        return _fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise CryptoError(
            "Ciphertext could not be decrypted — likely a Fernet key mismatch "
            "(was the /data volume rebuilt after credentials were stored?)."
        ) from exc
