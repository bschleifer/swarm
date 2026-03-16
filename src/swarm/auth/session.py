"""Cookie-based session management using HMAC-SHA256.

Sessions auto-invalidate when ``api_password`` changes because the signing
key is derived from the password.
"""

from __future__ import annotations

import hashlib
import hmac
import time

_COOKIE_NAME = "swarm_session"
_MAX_AGE = 30 * 24 * 3600  # 30 days


def _signing_key(password: str) -> bytes:
    """Derive a signing key from the API password."""
    return hmac.new(password.encode(), b"swarm-session-v1", hashlib.sha256).digest()


def create_session_cookie(password: str) -> tuple[str, int]:
    """Create a session cookie value and its max-age in seconds.

    Returns ``(cookie_value, max_age)`` where *cookie_value* is
    ``<expiry_unix>.<hmac_hex>``.
    """
    expiry = int(time.time()) + _MAX_AGE
    key = _signing_key(password)
    sig = hmac.new(key, str(expiry).encode(), hashlib.sha256).hexdigest()
    return f"{expiry}.{sig}", _MAX_AGE


def verify_session_cookie(cookie: str, password: str) -> bool:
    """Verify a session cookie value against the current password."""
    if not cookie or not password:
        return False
    parts = cookie.split(".", 1)
    if len(parts) != 2:
        return False
    expiry_str, sig = parts
    try:
        expiry = int(expiry_str)
    except ValueError:
        return False
    if time.time() > expiry:
        return False
    key = _signing_key(password)
    expected = hmac.new(key, expiry_str.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)
