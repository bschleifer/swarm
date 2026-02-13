"""Regression tests for HTML template attribute correctness.

Duplicate class= attributes on HTML elements cause the browser to ignore
all but the first, breaking styles (e.g. width: 120px instead of 100%).
"""

from __future__ import annotations

import re
from pathlib import Path

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "src" / "swarm" / "web" / "templates"

# Matches opening HTML tags (possibly spanning multiple lines)
_TAG_RE = re.compile(r"<[a-zA-Z][^>]*>", re.DOTALL)
# Matches individual class="..." attributes within a tag
_CLASS_ATTR_RE = re.compile(r'\bclass\s*=\s*"[^"]*"')


def test_no_duplicate_class_attributes():
    """Every HTML tag should have at most one class= attribute."""
    errors: list[str] = []
    for template in sorted(TEMPLATES_DIR.glob("*.html")):
        content = template.read_text()
        lines = content.split("\n")
        # Track character offset → line number
        offset_to_line: list[int] = []
        for i, line in enumerate(lines, 1):
            offset_to_line.extend([i] * (len(line) + 1))  # +1 for \n
        for m in _TAG_RE.finditer(content):
            tag_text = m.group()
            class_matches = _CLASS_ATTR_RE.findall(tag_text)
            if len(class_matches) > 1:
                line_no = offset_to_line[m.start()] if m.start() < len(offset_to_line) else "?"
                errors.append(
                    f"{template.name}:{line_no} — tag has {len(class_matches)} "
                    f"class attributes: {class_matches}"
                )
    assert not errors, "Duplicate class= attributes found:\n" + "\n".join(errors)
