"""Tests for calculator module."""

from src.calculator import add, divide, subtract


def test_add():
    assert add(2, 3) == 5
    assert add(-1, 1) == 0


def test_subtract():
    assert subtract(5, 3) == 2
    assert subtract(0, 0) == 0


def test_divide():
    assert divide(10, 2) == 5.0
    assert divide(7, 2) == 3.5


def test_divide_by_zero():
    """This test will FAIL — exposing the bug."""
    try:
        divide(1, 0)
        assert False, "Should have raised an error"
    except ZeroDivisionError:
        pass  # Bug: should return error, not raise
