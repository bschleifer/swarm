"""Input validation module — intentionally has a broken email regex."""

import re

# BUG: missing + in local part character class
_EMAIL_RE = re.compile(r"^[a-zA-Z0-9._-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")


def is_valid_email(email: str) -> bool:
    return bool(_EMAIL_RE.match(email))


def is_valid_username(username: str) -> bool:
    return bool(re.match(r"^[a-zA-Z][a-zA-Z0-9_]{2,19}$", username))
