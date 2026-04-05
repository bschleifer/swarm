"""Tests for the feedback URL/markdown builder."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from swarm.feedback.builder import (
    Attachment,
    FeedbackPayload,
    build_issue_url,
    build_markdown,
)


def _payload(**overrides):
    defaults = {
        "title": "Something broke",
        "description": "Steps: 1. start swarm. 2. boom.",
        "category": "bug",
        "attachments": [
            Attachment(
                key="environment",
                label="Environment",
                content="Swarm: 1.0\nPython: 3.12",
            ),
        ],
    }
    defaults.update(overrides)
    return FeedbackPayload(**defaults)


def test_build_markdown_includes_description_and_attachments():
    md = build_markdown(_payload())
    assert "## Description" in md
    assert "Steps: 1. start swarm" in md
    assert "## Diagnostics" in md
    assert "Environment" in md
    assert "Swarm: 1.0" in md


def test_build_markdown_handles_empty_description():
    md = build_markdown(_payload(description="  "))
    assert "_(no description provided)_" in md


def test_build_markdown_skips_disabled_attachments():
    payload = _payload(
        attachments=[
            Attachment(key="a", label="A", content="keep me", enabled=True),
            Attachment(key="b", label="B", content="drop me", enabled=False),
        ]
    )
    md = build_markdown(payload)
    assert "keep me" in md
    assert "drop me" not in md


def test_build_markdown_skips_empty_content_attachments():
    payload = _payload(attachments=[Attachment(key="a", label="A", content="   ", enabled=True)])
    md = build_markdown(payload)
    assert "## Diagnostics" not in md


def test_build_issue_url_basic():
    url, markdown, truncated = build_issue_url(_payload(), repo="owner/repo")
    assert url.startswith("https://github.com/owner/repo/issues/new?")
    assert not truncated
    # Title is sent via the URL param, not the body
    assert "Steps: 1. start swarm" in markdown

    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    assert params["title"] == ["Something broke"]
    assert params["labels"] == ["bug"]
    assert "Steps: 1. start swarm" in params["body"][0]


def test_build_issue_url_category_labels():
    _, _, _ = build_issue_url(_payload(category="feature"), repo="o/r")
    url, _, _ = build_issue_url(_payload(category="feature"), repo="o/r")
    params = parse_qs(urlparse(url).query)
    assert params["labels"] == ["enhancement"]

    url, _, _ = build_issue_url(_payload(category="question"), repo="o/r")
    params = parse_qs(urlparse(url).query)
    assert params["labels"] == ["question"]


def test_build_issue_url_truncates_when_too_long():
    # Build an attachment that's well over the URL limit
    huge = "line of log data " * 2000  # ~34KB
    payload = _payload(
        attachments=[
            Attachment(key="logs", label="Logs", content=huge, enabled=True),
        ]
    )
    url, markdown, truncated = build_issue_url(payload, repo="o/r")
    assert truncated
    assert len(url) <= 7500 + 200  # small headroom for the base URL overhead
    # Full markdown should contain the full content (not truncated)
    assert len(markdown) > len(url)
    assert huge[:100] in markdown


def test_build_issue_url_keeps_title_even_when_over_limit():
    huge = "x" * 50000
    payload = _payload(
        attachments=[Attachment(key="logs", label="Logs", content=huge, enabled=True)]
    )
    url, _, truncated = build_issue_url(payload, repo="o/r")
    assert truncated
    params = parse_qs(urlparse(url).query)
    assert params["title"] == ["Something broke"]


def test_build_markdown_multiple_attachments_order_preserved():
    payload = _payload(
        attachments=[
            Attachment(key="a", label="First", content="one"),
            Attachment(key="b", label="Second", content="two"),
            Attachment(key="c", label="Third", content="three"),
        ]
    )
    md = build_markdown(payload)
    first_idx = md.index("First")
    second_idx = md.index("Second")
    third_idx = md.index("Third")
    assert first_idx < second_idx < third_idx
