"""Tests for validator module."""

from src.validator import is_valid_email, is_valid_username


def test_valid_emails():
    assert is_valid_email("user@example.com")
    assert is_valid_email("first.last@domain.org")


def test_plus_sign_emails():
    """This test will FAIL — exposing the regex bug."""
    assert is_valid_email("user+tag@example.com")


def test_invalid_emails():
    assert not is_valid_email("")
    assert not is_valid_email("not-an-email")
    assert not is_valid_email("@missing-local.com")


def test_valid_usernames():
    assert is_valid_username("alice")
    assert is_valid_username("bob_123")


def test_invalid_usernames():
    assert not is_valid_username("ab")  # too short
    assert not is_valid_username("1start")  # starts with number
