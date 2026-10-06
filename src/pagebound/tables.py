"""Render a Docling table as a markdown table.

Docling gives cells with row and column offsets rather than rendered
text, so the grid is rebuilt here. A table lands in the document as one
block whose text is this rendering, which keeps quote verification
working against the markdown consumers already read.
"""

from __future__ import annotations

from typing import Any


def _clean(text: str) -> str:
    """One line, with pipes escaped so they do not break the row."""
    return " ".join(text.split()).replace("|", "\\|")


def render_table(data: dict[str, Any]) -> str:
    rows = data.get("num_rows", 0)
    cols = data.get("num_cols", 0)
    if rows <= 0 or cols <= 0:
        return ""

    grid = [["" for _ in range(cols)] for _ in range(rows)]
    header_rows: set[int] = set()
    for cell in data.get("table_cells", []):
        row = cell.get("start_row_offset_idx", 0)
        col = cell.get("start_col_offset_idx", 0)
        if not (0 <= row < rows and 0 <= col < cols):
            continue
        grid[row][col] = _clean(cell.get("text", ""))
        if cell.get("column_header"):
            header_rows.add(row)

    # Markdown needs a separator after the first row whether or not that
    # row is semantically a header, so a table with no header still renders
    # as a table rather than as one long paragraph.
    separator_after = max(header_rows) if header_rows else 0

    lines: list[str] = []
    for index, row_cells in enumerate(grid):
        lines.append("| " + " | ".join(row_cells) + " |")
        if index == separator_after:
            lines.append("| " + " | ".join("---" for _ in range(cols)) + " |")
    return "\n".join(lines)
