"""The pagebound document format.

Two representations of one document travel together: these blocks, and a
markdown rendering generated from them. `md_start` and `md_end` index into
that markdown, so a consumer that finds a quote in the text can recover
the page and bounding box it came from.

Coordinates stay in the PDF's own space with the origin at the bottom
left, serialised as `[l, b, r, t]`. That is Zotero's convention for
annotation rects, so a consumer that wants to highlight a passage needs a
field reorder and no geometry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pagebound.errors import UnsupportedFormatError

FORMAT_VERSION = 1

# Block kinds. `furniture` layer covers running headers and footers, which
# Docling separates from the body for us; they are kept rather than
# dropped, because the format is faithful and filtering is the consumer's
# policy.
KINDS = frozenset({
    "heading", "paragraph", "list_item", "table", "caption",
    "footnote", "formula", "code", "image",
})
LAYERS = frozenset({"body", "furniture"})


@dataclass(frozen=True)
class PageSize:
    page: int
    width: float
    height: float

    def to_dict(self) -> dict[str, Any]:
        return {"page": self.page, "width": self.width, "height": self.height}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PageSize:
        return cls(page=d["page"], width=d["width"], height=d["height"])


@dataclass(frozen=True)
class Block:
    id: int
    kind: str
    text: str
    headings: list[str]
    layer: str
    page: int
    bbox: tuple[float, float, float, float]  # [l, b, r, t], BOTTOMLEFT
    md_start: int | None
    md_end: int | None
    level: int | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "text": self.text,
            "headings": list(self.headings),
            "layer": self.layer,
            "page": self.page,
            "bbox": list(self.bbox),
            "md_start": self.md_start,
            "md_end": self.md_end,
        }
        if self.level is not None:
            d["level"] = self.level
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Block:
        return cls(
            id=d["id"],
            kind=d["kind"],
            text=d["text"],
            headings=list(d["headings"]),
            layer=d["layer"],
            page=d["page"],
            bbox=tuple(d["bbox"]),  # type: ignore[arg-type]
            md_start=d["md_start"],
            md_end=d["md_end"],
            level=d.get("level"),
        )


@dataclass(frozen=True)
class Source:
    sha256: str
    pages: int
    page_sizes: list[PageSize]
    # The text-layer probe's verdict on the PDF itself. False means the
    # text in this artifact came out of OCR, so a consumer can weigh it
    # accordingly. None on artifacts written before the probe was
    # recorded.
    text_layer: bool | None = None
    probe_pages: int | None = None
    probe_chars: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "sha256": self.sha256,
            "pages": self.pages,
            "page_sizes": [p.to_dict() for p in self.page_sizes],
            "text_layer": self.text_layer,
            "probe_pages": self.probe_pages,
            "probe_chars": self.probe_chars,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Source:
        return cls(
            sha256=d["sha256"],
            pages=d["pages"],
            page_sizes=[PageSize.from_dict(p) for p in d["page_sizes"]],
            text_layer=d.get("text_layer"),
            probe_pages=d.get("probe_pages"),
            probe_chars=d.get("probe_chars"),
        )


@dataclass(frozen=True)
class Conversion:
    converter: str
    converter_version: str
    options: dict[str, Any]
    recipe: str
    converted_at: str
    pagebound_version: str
    # Wall-clock seconds the converter ran; None on artifacts written
    # before the field existed. Per recipe like the rest of the block, so
    # a new Docling's speed profile never blends into the old one's.
    duration_seconds: float | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "converter": self.converter,
            "converter_version": self.converter_version,
            "options": dict(self.options),
            "recipe": self.recipe,
            "converted_at": self.converted_at,
            "pagebound_version": self.pagebound_version,
        }
        if self.duration_seconds is not None:
            d["duration_seconds"] = self.duration_seconds
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Conversion:
        return cls(
            converter=d["converter"],
            converter_version=d["converter_version"],
            options=dict(d["options"]),
            recipe=d["recipe"],
            converted_at=d["converted_at"],
            pagebound_version=d["pagebound_version"],
            duration_seconds=d.get("duration_seconds"),
        )


@dataclass(frozen=True)
class Document:
    format_version: int
    source: Source
    conversion: Conversion
    blocks: list[Block] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "format_version": self.format_version,
            "source": self.source.to_dict(),
            "conversion": self.conversion.to_dict(),
            "blocks": [b.to_dict() for b in self.blocks],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Document:
        version = d["format_version"]
        if version > FORMAT_VERSION:
            raise UnsupportedFormatError(
                f"artifact declares format_version {version}, "
                f"but this pagebound understands at most {FORMAT_VERSION}"
            )
        return cls(
            format_version=version,
            source=Source.from_dict(d["source"]),
            conversion=Conversion.from_dict(d["conversion"]),
            blocks=[Block.from_dict(b) for b in d["blocks"]],
        )
