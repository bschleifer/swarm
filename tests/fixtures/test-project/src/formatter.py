"""String formatting utilities — clean code, all tests should pass."""


def title_case(text: str) -> str:
    return text.title()


def snake_case(text: str) -> str:
    result = []
    for i, ch in enumerate(text):
        if ch.isupper() and i > 0:
            result.append("_")
        result.append(ch.lower())
    return "".join(result)


def truncate(text: str, max_len: int = 80, suffix: str = "...") -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - len(suffix)] + suffix
