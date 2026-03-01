"""StyledContent — container for terminal output with per-character style data."""

from __future__ import annotations

from dataclasses import dataclass

from swarm.pty.terminal import CellStyle


@dataclass(frozen=True, slots=True)
class StyledContent:
    """Terminal content with optional per-character style information.

    Attributes
    ----------
    text:
        Plain text identical to ``get_lines()`` output.
    rows:
        Per-row ``(line_text, [CellStyle, ...])`` pairs from the
        terminal emulator.  May be empty when style data is unavailable.
    """

    text: str
    rows: list[tuple[str, list[CellStyle]]]

    def has_styles(self) -> bool:
        """True when style data is available."""
        return bool(self.rows)

    def style_at(self, row: int, col: int) -> CellStyle | None:
        """Return the style at a specific row/col, or None if out of range."""
        if row < 0 or row >= len(self.rows):
            return None
        _, styles = self.rows[row]
        if col < 0 or col >= len(styles):
            return None
        return styles[col]

    def find_styled_text(
        self,
        needle: str,
        *,
        fg: str | None = None,
        bold: bool | None = None,
        dim: bool | None = None,
    ) -> bool:
        """Search all rows for *needle* where characters match style predicates.

        Style predicates use "not default" semantics:
        - ``fg="!default"`` means fg is anything other than ``"default"``
        - ``fg="green"`` means fg is exactly ``"green"``
        - ``dim=True`` means the dim attribute must be set

        Returns True if the text is found AND all style predicates hold
        for every character of the match.
        """
        if not self.rows or not needle:
            return False

        not_default_fg = fg == "!default" if fg else False

        for row_text, row_styles in self.rows:
            start = 0
            while True:
                idx = row_text.find(needle, start)
                if idx < 0:
                    break
                end = idx + len(needle)
                if end > len(row_styles):
                    break
                if self._styles_match(
                    row_styles[idx:end],
                    fg=fg,
                    not_default_fg=not_default_fg,
                    bold=bold,
                    dim=dim,
                ):
                    return True
                start = idx + 1
        return False

    @staticmethod
    def _styles_match(
        styles: list[CellStyle],
        *,
        fg: str | None,
        not_default_fg: bool,
        bold: bool | None,
        dim: bool | None,
    ) -> bool:
        """Check whether all styles in a span satisfy the predicates."""
        for s in styles:
            if not_default_fg:
                if s.fg == "default":
                    return False
            elif fg is not None and s.fg != fg:
                return False
            if bold is not None and s.bold != bold:
                return False
            if dim is not None and s.dim != dim:
                return False
        return True
