from pagebound.model import (
    FORMAT_VERSION, Block, Conversion, Document, PageSize, Source,
)


def a_document() -> Document:
    return Document(
        format_version=FORMAT_VERSION,
        source=Source(
            sha256="a" * 64,
            pages=1,
            page_sizes=[PageSize(page=1, width=595.3, height=790.9)],
        ),
        conversion=Conversion(
            converter="docling",
            converter_version="2.97.0",
            options={"ocr": True},
            recipe="a4f21c9e",
            converted_at="2026-08-19T17:12:00Z",
            pagebound_version="0.1.0",
        ),
        blocks=[
            Block(
                id=0, kind="heading", text="Introduction", headings=[],
                layer="body", page=1, bbox=(72.0, 108.5, 523.3, 124.1),
                md_start=0, md_end=16, level=1,
            ),
            Block(
                id=1, kind="paragraph", text="Body text.",
                headings=["Introduction"], layer="body", page=1,
                bbox=(72.0, 90.0, 523.3, 104.0), md_start=18, md_end=28,
            ),
        ],
    )


def test_round_trip_preserves_everything():
    original = a_document()
    assert Document.from_dict(original.to_dict()) == original


def test_bbox_serialises_as_a_four_element_list():
    block = a_document().to_dict()["blocks"][0]
    assert block["bbox"] == [72.0, 108.5, 523.3, 124.1]


def test_bbox_is_bottom_left_so_the_second_value_is_below_the_fourth():
    left, bottom, right, top = a_document().blocks[0].bbox
    assert bottom < top and left < right


def test_optional_level_is_absent_when_unset():
    paragraph = a_document().to_dict()["blocks"][1]
    assert "level" not in paragraph


def test_from_dict_rejects_a_future_format_version():
    import pytest
    from pagebound.errors import UnsupportedFormatError

    payload = a_document().to_dict()
    payload["format_version"] = FORMAT_VERSION + 1
    with pytest.raises(UnsupportedFormatError):
        Document.from_dict(payload)


def test_conversion_round_trips_duration_seconds():
    conversion = Conversion(
        converter="docling", converter_version="2.97.0", options={},
        recipe="a4f21c9e", converted_at="2026-08-30T16:00:00Z",
        pagebound_version="0.1.0", duration_seconds=12.345,
    )
    assert Conversion.from_dict(conversion.to_dict()).duration_seconds == 12.345


def test_a_conversion_without_duration_reads_back_as_none():
    # Artifacts written before the field existed must keep loading.
    d = a_document().to_dict()
    assert "duration_seconds" not in d["conversion"]
    assert Document.from_dict(d).conversion.duration_seconds is None


def test_source_round_trips_the_probe_verdict():
    source = Source(sha256="a" * 64, pages=3, page_sizes=[],
                    text_layer=False, probe_pages=3, probe_chars=0)
    assert Source.from_dict(source.to_dict()) == source


def test_a_source_written_before_the_probe_fields_reads_them_as_none():
    source = Source.from_dict({"sha256": "a" * 64, "pages": 1, "page_sizes": []})
    assert source.text_layer is None
    assert source.probe_pages is None
    assert source.probe_chars is None
