"""Tests for _html_to_text email body conversion."""

from swarm.server.email_service import _html_to_text


class TestHtmlToText:
    def test_plain_text_passthrough(self):
        assert _html_to_text("Hello world") == "Hello world"

    def test_br_tags(self):
        assert "line1\nline2" in _html_to_text("line1<br>line2")
        assert "line1\nline2" in _html_to_text("line1<BR/>line2")
        assert "line1\nline2" in _html_to_text("line1<br />line2")

    def test_paragraph_tags(self):
        result = _html_to_text("<p>First paragraph</p><p>Second paragraph</p>")
        assert "First paragraph" in result
        assert "Second paragraph" in result
        # Paragraphs should be separated by blank line
        assert "\n\n" in result

    def test_div_tags(self):
        result = _html_to_text("<div>Block 1</div><div>Block 2</div>")
        assert "Block 1" in result
        assert "Block 2" in result

    def test_list_items(self):
        result = _html_to_text("<ul><li>Item 1</li><li>Item 2</li></ul>")
        assert "Item 1" in result
        assert "Item 2" in result
        # Markdown-style bullet markers per item.
        assert "- Item 1" in result
        assert "- Item 2" in result

    def test_ordered_list_items(self):
        result = _html_to_text("<ol><li>First</li><li>Second</li></ol>")
        assert "1. First" in result
        assert "2. Second" in result

    def test_headings(self):
        # Heading level is preserved (h1 \u2192 '#', h2 \u2192 '##', not all flattened to '##').
        assert "# Title" in _html_to_text("<h1>Title</h1>")
        assert "## Sub" in _html_to_text("<h2>Sub</h2>")
        assert "### Smaller" in _html_to_text("<h3>Smaller</h3>")

    def test_inline_marks(self):
        out = _html_to_text("<p>This is <b>bold</b> and <i>italic</i>.</p>")
        assert "**bold**" in out
        assert "*italic*" in out

    def test_link(self):
        out = _html_to_text('<p>Visit <a href="https://example.com">us</a>.</p>')
        assert "[us](https://example.com)" in out

    def test_link_without_href(self):
        out = _html_to_text("<p>plain <a>label</a> here</p>")
        assert "label" in out
        assert "[" not in out

    def test_code_block(self):
        out = _html_to_text("<pre>line1\nline2</pre>")
        assert "```" in out
        assert "line1" in out
        assert "line2" in out

    def test_void_elements_in_head_dont_swallow_body(self):
        """Outlook/Graph emails ship full ``<html><head><meta><link><style>``
        boilerplate. Void elements like ``<meta>`` and ``<link>`` have no end
        tag — if they bumped the skip-depth counter, ``</head>`` would leave
        the parser permanently in skip mode and the entire ``<body>`` would
        be silently dropped (regression seen on a real Graph-fetched email)."""
        html = (
            "<html><head>"
            '<meta http-equiv="Content-Type" content="text/html; charset=utf-8">'
            '<link rel="stylesheet" href="x.css">'
            "<style>p { margin:0; }</style>"
            "</head><body><p>Hello reader</p>"
            "<p>This is the second paragraph.</p>"
            "</body></html>"
        )
        out = _html_to_text(html)
        assert "Hello reader" in out, f"body content lost; got {out!r}"
        assert "second paragraph" in out
        # Style content must NOT leak through.
        assert "margin" not in out
        assert "stylesheet" not in out

    def test_html_entities(self):
        assert "you & me" in _html_to_text("you &amp; me")
        assert '"quoted"' in _html_to_text("&quot;quoted&quot;")
        assert "it's" in _html_to_text("it&#39;s")

    def test_strips_remaining_tags(self):
        result = _html_to_text("<span class='x'>text</span>")
        assert "<span" not in result
        assert "text" in result

    def test_collapses_excessive_whitespace(self):
        result = _html_to_text("   lots   of   spaces   ")
        assert "lots of spaces" in result

    def test_collapses_excessive_newlines(self):
        result = _html_to_text("<p>A</p><p></p><p></p><p></p><p>B</p>")
        # Should not have more than 2 consecutive newlines
        assert "\n\n\n" not in result

    def test_real_outlook_email_structure(self):
        """Simulated Outlook HTML email body."""
        html = (
            '<html><body><div class="WordSection1">'
            "<p>Hi team,</p>"
            "<p>Please review the following:</p>"
            "<ul>"
            "<li>The login page is broken on mobile</li>"
            "<li>Users can't reset passwords</li>"
            "</ul>"
            "<p>Thanks,<br>John</p>"
            "</div></body></html>"
        )
        result = _html_to_text(html)
        assert "Hi team," in result
        assert "Please review the following:" in result
        assert "login page is broken" in result
        assert "reset passwords" in result
        assert "Thanks," in result
        assert "John" in result
        # No HTML tags should remain
        assert "<" not in result

    def test_empty_input(self):
        assert _html_to_text("") == ""

    def test_table_rows(self):
        result = _html_to_text("<table><tr><td>A</td><td>B</td></tr></table>")
        assert "A" in result
        assert "B" in result
