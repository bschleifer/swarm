"""Tests for swarm.auth.session — HMAC cookie create/verify."""

from __future__ import annotations

import time
from unittest.mock import patch

from swarm.auth.session import create_session_cookie, verify_session_cookie


class TestCreateSessionCookie:
    def test_returns_tuple(self) -> None:
        value, max_age = create_session_cookie("secret")
        assert isinstance(value, str)
        assert isinstance(max_age, int)
        assert max_age == 30 * 24 * 3600

    def test_cookie_format(self) -> None:
        value, _ = create_session_cookie("secret")
        parts = value.split(".")
        assert len(parts) == 2
        expiry_str, sig = parts
        assert expiry_str.isdigit()
        assert len(sig) == 64  # hex SHA-256


class TestVerifySessionCookie:
    def test_valid_cookie(self) -> None:
        value, _ = create_session_cookie("secret")
        assert verify_session_cookie(value, "secret") is True

    def test_wrong_password(self) -> None:
        value, _ = create_session_cookie("secret")
        assert verify_session_cookie(value, "wrong") is False

    def test_expired_cookie(self) -> None:
        value, _ = create_session_cookie("secret")
        with patch("swarm.auth.session.time") as mock_time:
            mock_time.time.return_value = time.time() + 31 * 24 * 3600
            assert verify_session_cookie(value, "secret") is False

    def test_empty_inputs(self) -> None:
        assert verify_session_cookie("", "secret") is False
        assert verify_session_cookie("123.abc", "") is False

    def test_malformed_cookie(self) -> None:
        assert verify_session_cookie("not-a-cookie", "secret") is False
        assert verify_session_cookie("abc.def", "secret") is False

    def test_password_change_invalidates(self) -> None:
        value, _ = create_session_cookie("old-password")
        assert verify_session_cookie(value, "old-password") is True
        assert verify_session_cookie(value, "new-password") is False
