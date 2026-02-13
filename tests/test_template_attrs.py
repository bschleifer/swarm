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


def test_dashboard_has_paste_interception():
    """Ctrl-V paste must be intercepted so raw 0x16 doesn't reach Claude Code.

    Both inline and modal xterm.js terminals need:
    1. attachCustomKeyEventHandler blocking Ctrl+V
    2. Capture-phase paste handler on the textarea
    Without these, Claude Code shows "No images found in clipboard" on paste.
    """
    content = (TEMPLATES_DIR / "dashboard.html").read_text()
    # attachCustomKeyEventHandler must appear at least twice (inline + modal)
    assert content.count("attachCustomKeyEventHandler") >= 2, (
        "dashboard.html must block Ctrl+V via attachCustomKeyEventHandler on both terminals"
    )
    # Capture-phase paste handlers (addEventListener('paste', ..., true))
    assert content.count("addEventListener('paste'") >= 2, (
        "dashboard.html must have capture-phase paste handlers on both terminals"
    )
