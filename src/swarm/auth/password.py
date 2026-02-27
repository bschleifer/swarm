"""Password hashing and verification using scrypt.

Stored format: ``scrypt:<salt_hex>:<hash_hex>``
"""

from __future__ import annotations

import hashlib
import hmac
import os

_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_KEY_LEN = 32
_SALT_LEN = 16

_PREFIX = "scrypt:"


def hash_password(password: str) -> str:
    """Hash *password* with a random salt. Returns ``scrypt:<salt>:<hash>``."""
    salt = os.urandom(_SALT_LEN)
    dk = hashlib.scrypt(
        password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_KEY_LEN
    )
    return f"{_PREFIX}{salt.hex()}:{dk.hex()}"


def is_hashed(stored: str) -> bool:
    """Return True if *stored* looks like an scrypt hash."""
    return stored.startswith(_PREFIX)


def verify_password(password: str, stored: str) -> bool:
    """Verify *password* against *stored* (hashed or plaintext).

    Accepts three cases:
    1. *password* is the original plaintext, *stored* is the scrypt hash → hash & compare
    2. *password* is the stored hash itself (token pass-through from dashboard) → exact match
    3. Both are plaintext → constant-time comparison
    """
    if not password or not stored:
        return False

    # Token pass-through: dashboard injects the stored value and sends it back.
    # Accept an exact match (constant-time) regardless of format.
    if hmac.compare_digest(password.encode(), stored.encode()):
        return True

    if is_hashed(stored):
        # Format: scrypt:<salt_hex>:<hash_hex>
        parts = stored.split(":")
        if len(parts) != 3:
            return False
        try:
            salt = bytes.fromhex(parts[1])
            expected = bytes.fromhex(parts[2])
        except ValueError:
            return False
        dk = hashlib.scrypt(
            password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_KEY_LEN
        )
        return hmac.compare_digest(dk, expected)

    return False
