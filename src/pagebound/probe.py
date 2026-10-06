"""Ask the PDF directly whether it has text, before paying for conversion.

Three pages tell a text layer from its absence, and the check costs about
16ms. The point is to name a scanned paper as such BEFORE Docling spends
thirty seconds producing nothing from it.

pypdfium2 rather than pdfplumber: an order of magnitude faster, one
dependency against eight, and layout analysis is Docling's job. Two
document-parsing stacks would be one too many.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from pagebound.errors import NoTextLayerError

PROBE_PAGES = 3
PROBE_FLOOR_CHARS = 200


def _open_with_pdfium(path: Path) -> Any:
    import pypdfium2 as pdfium

    return pdfium.PdfDocument(path)


def text_layer(
    pdf_path: str | Path,
    *,
    open_document: Callable[[Path], Any] | None = None,
) -> dict[str, Any]:
    """Return {pages, probe_pages, probe_chars, has_text} for a PDF.

    `open_document` is an injection point for tests; production callers
    omit it and get pypdfium2.
    """
    path = Path(pdf_path)
    opener = open_document or _open_with_pdfium

    try:
        document = opener(path)
    except Exception as error:  # noqa: BLE001 - pdfium raises several types
        raise NoTextLayerError(f"could not open {path.name}: {error}") from error

    pages = len(document)
    probe_pages = min(PROBE_PAGES, pages)
    probe_chars = 0
    for index in range(probe_pages):
        probe_chars += len(document[index].get_textpage().get_text_range())

    return {
        "pages": pages,
        "probe_pages": probe_pages,
        "probe_chars": probe_chars,
        "has_text": probe_chars >= PROBE_FLOOR_CHARS,
    }
