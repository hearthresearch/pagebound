"""DoclingDocument JSON to the pagebound format, plus its markdown.

The markdown is generated here rather than requested from Docling. That
makes `md_start` and `md_end` exact by construction: a consumer that finds
a quote in the text recovers the block, and with it the page and bounding
box, without any fuzzy matching. Docling's own `charspan` cannot serve
this purpose, because it is an offset within each item's own text and not
a document position.

Ordering: body items in Docling's body-tree reading order, then furniture
(running headers and footers) by page and vertical position. Only body
items reach the markdown. Furniture is kept, because the format is
faithful and filtering is the consumer's policy, but a running header
repeated once per page does not belong in the text quotes are checked
against.
"""

from __future__ import annotations

import copy
from pathlib import PurePath
from typing import Any, Iterator

from pagebound.model import (
    FORMAT_VERSION, Block, Conversion, Document, PageSize, Source,
)
from pagebound.store import strip_embedded_images
from pagebound.tables import render_table

LABEL_KINDS: dict[str, str] = {
    "title": "heading",
    "section_header": "heading",
    "text": "paragraph",
    "paragraph": "paragraph",
    "list_item": "list_item",
    "caption": "caption",
    "footnote": "footnote",
    "formula": "formula",
    "code": "code",
    "picture": "image",
    "table": "table",
    "page_header": "paragraph",
    "page_footer": "paragraph",
}

_COLLECTIONS = ("texts", "tables", "pictures", "groups")


def strip_page_images(docling: dict[str, Any]) -> dict[str, Any]:
    """A copy without the base64 page renders.

    `--image-export-mode referenced` governs the markdown only; the JSON
    carries a full render of every page regardless. Removing them was
    measured at a 97.9% reduction, and the result still validates as a
    DoclingDocument and still chunks with HybridChunker.
    """
    out = copy.deepcopy(docling)
    pages = out.get("pages")
    if isinstance(pages, dict):
        for page in pages.values():
            page.pop("image", None)
    elif isinstance(pages, list):
        for page in pages:
            page.pop("image", None)
    # Docling writes picture URIs as absolute paths into its scratch
    # directory, which is gone by the time anyone reads the artifact.
    # The store keeps the same files under images/, so point there.
    for picture in out.get("pictures") or []:
        image = picture.get("image") if isinstance(picture, dict) else None
        uri = image.get("uri") if isinstance(image, dict) else None
        if isinstance(uri, str) and uri and not uri.startswith("data:"):
            image["uri"] = f"images/{PurePath(uri).name}"
    return out


def _resolve(docling: dict[str, Any], ref: str) -> dict[str, Any] | None:
    """Turn "#/texts/3" into the item it names."""
    parts = ref.lstrip("#/").split("/")
    if len(parts) != 2:
        return None
    collection, index = parts
    if collection not in _COLLECTIONS:
        return None
    items = docling.get(collection)
    try:
        if isinstance(items, dict):
            return items[index]
        return items[int(index)]
    except (KeyError, IndexError, ValueError, TypeError):
        return None


def _walk(docling: dict[str, Any], node: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Body-tree order, descending into groups (lists, for example)."""
    for child in node.get("children", []) or []:
        ref = child.get("$ref") if isinstance(child, dict) else None
        if not ref:
            continue
        item = _resolve(docling, ref)
        if item is None:
            continue
        if ref.startswith("#/groups/"):
            yield from _walk(docling, item)
        else:
            yield item


def _bbox(item: dict[str, Any]) -> tuple[int, tuple[float, float, float, float]] | None:
    prov = item.get("prov") or []
    if not prov:
        return None
    box = prov[0].get("bbox") or {}
    try:
        return prov[0]["page_no"], (
            float(box["l"]), float(box["b"]), float(box["r"]), float(box["t"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _text_of(item: dict[str, Any]) -> str:
    """The item's rendered text, with any embedded base64 image stripped.

    Stripped here rather than left to the store-level seatbelt, so that
    `md_start`/`md_end` (computed below from this same string) stay exact:
    if the store stripped it after offsets were computed, a block whose
    own text contained an embedded image would shift the stored markdown
    out from under its own offsets.
    """
    if item.get("label") == "table":
        return strip_embedded_images(render_table(item.get("data") or {}))
    if item.get("label") == "picture" and not item.get("text"):
        return "<!-- image -->"
    return strip_embedded_images(item.get("text", "") or "")


def _render(kind: str, text: str, level: int | None) -> str:
    if kind == "heading":
        return "#" * max(1, min(level or 1, 6)) + " " + text
    if kind == "list_item":
        return "- " + text
    if kind == "code":
        return "```\n" + text + "\n```"
    if kind == "image":
        return text
    return text


def _page_sizes(docling: dict[str, Any]) -> list[PageSize]:
    pages = docling.get("pages") or {}
    values = pages.values() if isinstance(pages, dict) else pages
    sizes = []
    for page in values:
        size = page.get("size") or {}
        sizes.append(PageSize(
            page=page.get("page_no", 0),
            width=float(size.get("width", 0.0)),
            height=float(size.get("height", 0.0)),
        ))
    return sorted(sizes, key=lambda p: p.page)


def normalize(
    docling: dict[str, Any],
    *,
    source_sha256: str,
    conversion: Conversion,
    probe: dict[str, Any] | None = None,
) -> tuple[Document, str]:
    """Return the normalised document and the markdown its offsets index.

    `probe` is the text-layer probe's result, recorded on the source so
    OCR-derived text is never mistaken for a text layer.
    """
    body_items = [i for i in _walk(docling, docling.get("body") or {})]
    body_refs = {id(i) for i in body_items}

    furniture: list[dict[str, Any]] = []
    for collection in ("texts", "tables", "pictures"):
        for item in docling.get(collection) or []:
            if id(item) in body_refs:
                continue
            if item.get("content_layer") == "furniture":
                furniture.append(item)
    furniture.sort(key=lambda i: (_bbox(i)[0] if _bbox(i) else 0,
                                  -(_bbox(i)[1][3] if _bbox(i) else 0.0)))

    blocks: list[Block] = []
    pieces: list[str] = []
    cursor = 0
    heading_stack: list[tuple[int, str]] = []

    def append(item: dict[str, Any], *, in_markdown: bool) -> None:
        nonlocal cursor
        located = _bbox(item)
        if located is None:
            return
        page, box = located
        kind = LABEL_KINDS.get(item.get("label", ""), "paragraph")
        text = _text_of(item)
        if not text:
            return
        level = item.get("level") if kind == "heading" else None

        if kind == "heading":
            while heading_stack and heading_stack[-1][0] >= (level or 1):
                heading_stack.pop()
        headings = [h for _, h in heading_stack]

        md_start = md_end = None
        if in_markdown:
            rendered = _render(kind, text, level)
            if pieces:
                cursor += 2  # the "\n\n" that will join this to the previous
            pieces.append(rendered)
            offset = rendered.index(text) if text in rendered else 0
            md_start = cursor + offset
            md_end = md_start + len(text)
            cursor += len(rendered)

        blocks.append(Block(
            id=len(blocks), kind=kind, text=text, headings=headings,
            layer=item.get("content_layer", "body"), page=page, bbox=box,
            md_start=md_start, md_end=md_end, level=level,
        ))

        if kind == "heading":
            heading_stack.append((level or 1, text))

    for item in body_items:
        append(item, in_markdown=True)
    for item in furniture:
        append(item, in_markdown=False)

    sizes = _page_sizes(docling)
    document = Document(
        format_version=FORMAT_VERSION,
        source=Source(
            sha256=source_sha256, pages=len(sizes), page_sizes=sizes,
            text_layer=None if probe is None else bool(probe["has_text"]),
            probe_pages=None if probe is None else probe.get("probe_pages"),
            probe_chars=None if probe is None else probe.get("probe_chars"),
        ),
        conversion=conversion,
        blocks=blocks,
    )
    return document, "\n\n".join(pieces)
