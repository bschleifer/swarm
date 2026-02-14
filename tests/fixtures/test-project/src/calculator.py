"""Simple calculator module — intentionally has a divide-by-zero bug."""


def add(a: float, b: float) -> float:
    return a + b


def subtract(a: float, b: float) -> float:
    return a - b


def divide(a: float, b: float) -> float:
    # BUG: no zero-division guard
    return a / b
