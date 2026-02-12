"""Tests for _html_to_text email body conversion."""

from swarm.server.daemon import _html_to_text


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
        # Bullets should be present
        assert "\u2022" in result  # bullet character

    def test_headings(self):
        result = _html_to_text("<h1>Title</h1><p>Body text</p>")
        assert "## Title" in result
        assert "Body text" in result

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
