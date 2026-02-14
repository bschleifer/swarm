"""Tests for formatter module — all should pass."""

from src.formatter import snake_case, title_case, truncate


def test_title_case():
    assert title_case("hello world") == "Hello World"
    assert title_case("") == ""


def test_snake_case():
    assert snake_case("HelloWorld") == "hello_world"
    assert snake_case("already_snake") == "already_snake"


def test_truncate():
    assert truncate("short", max_len=80) == "short"
    assert truncate("a" * 100, max_len=10) == "aaaaaaa..."
    assert len(truncate("a" * 100, max_len=20)) == 20
