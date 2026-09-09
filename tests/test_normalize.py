import pytest

from pagebound.model import Conversion, FORMAT_VERSION
from pagebound.normalize import normalize, strip_page_images


def conversion() -> Conversion:
    return Conversion(
        converter="docling", converter_version="2.97.0",
        options={"ocr": True}, recipe="a4f21c9e",
        converted_at="2026-08-19T17:12:00Z", pagebound_version="0.1.0",
    )


def text_item(ref, label, text, page=1, top=700.0, layer="body", level=None):
    item = {
        "self_ref": ref,
        "label": label,
        "content_layer": layer,
        "text": text,
        "prov": [{
            "page_no": page,
            "bbox": {"l": 72.0, "t": top, "r": 523.0, "b": top - 12.0,
                     "coord_origin": "BOTTOMLEFT"},
            "charspan": [0, len(text)],
        }],
    }
    if level is not None:
        item["level"] = level
    return item


def a_docling_document() -> dict:
    return {
        "schema_name": "DoclingDocument",
        "version": "1.10.0",
        "pages": {"1": {"page_no": 1, "size": {"width": 595.3, "height": 790.9},
                        "image": {"uri": "data:image/png;base64,AAAA"}}},
        "body": {"self_ref": "#/body", "children": [
            {"$ref": "#/texts/0"}, {"$ref": "#/texts/1"},
            {"$ref": "#/groups/0"}, {"$ref": "#/tables/0"},
        ]},
        "furniture": {"self_ref": "#/furniture", "children": []},
        "groups": [{"self_ref": "#/groups/0", "children": [
            {"$ref": "#/texts/2"}, {"$ref": "#/texts/3"},
        ]}],
        "texts": [
            text_item("#/texts/0", "section_header", "Methods", top=700.0, level=1),
            text_item("#/texts/1", "text", "We did things.", top=680.0),
            text_item("#/texts/2", "list_item", "First point.", top=660.0),
            text_item("#/texts/3", "list_item", "Second point.", top=640.0),
            text_item("#/texts/4", "page_header", "Journal Name", top=760.0,
                      layer="furniture"),
        ],
        "tables": [{
            "self_ref": "#/tables/0",
            "label": "table",
            "content_layer": "body",
            "prov": [{"page_no": 1,
                      "bbox": {"l": 72.0, "t": 600.0, "r": 523.0, "b": 500.0,
                               "coord_origin": "BOTTOMLEFT"},
                      "charspan": [0, 0]}],
            "data": {"num_rows": 1, "num_cols": 1, "table_cells": [
                {"text": "Cell", "start_row_offset_idx": 0,
                 "start_col_offset_idx": 0, "column_header": True},
            ]},
        }],
        "pictures": [],
    }


def test_blocks_follow_the_body_reading_order_then_furniture():
    document, _ = normalize(a_docling_document(), source_sha256="a" * 64,
                            conversion=conversion())
    assert [b.text for b in document.blocks] == [
        "Methods", "We did things.", "First point.", "Second point.",
        "| Cell |\n| --- |", "Journal Name",
    ]


def test_ids_are_sequential_and_match_position():
    document, _ = normalize(a_docling_document(), source_sha256="a" * 64,
                            conversion=conversion())
    assert [b.id for b in document.blocks] == list(range(len(document.blocks)))


def test_labels_map_to_kinds():
    document, _ = normalize(a_docling_document(), source_sha256="a" * 64,
                            conversion=conversion())
    assert [b.kind for b in document.blocks] == [
        "heading", "paragraph", "list_item", "list_item", "table", "paragraph",
    ]


def test_a_running_header_keeps_its_furniture_layer():
    document, _ = normalize(a_docling_document(), source_sha256="a" * 64,
                            conversion=conversion())
    header = document.blocks[-1]
    assert header.layer == "furniture"
    assert header.md_start is None and header.md_end is None


def test_bbox_is_reordered_to_left_bottom_right_top():
    document, _ = normalize(a_docling_document(), source_sha256="a" * 64,
                            conversion=conversion())
    # Docling gives {l: 72.0, t: 700.0, r: 523.0, b: 688.0}; Zotero wants
    # [l, b, r, t] and both use a bottom-left origin, so this is a field
    # reorder and not a geometric transform.
    assert document.blocks[0].bbox == (72.0, 688.0, 523.0, 700.0)


def test_the_heading_path_accumulates():
    document, _ = normalize(a_docling_document(), source_sha256="a" * 64,
                            conversion=conversion())
    assert document.blocks[0].headings == []
    assert document.blocks[1].headings == ["Methods"]
    assert document.blocks[2].headings == ["Methods"]


def test_offsets_index_the_generated_markdown_exactly():
    document, markdown = normalize(a_docling_document(), source_sha256="a" * 64,
                                   conversion=conversion())
    for block in document.blocks:
        if block.md_start is None:
            continue
        assert block.text in markdown[block.md_start:block.md_end]


def test_the_markdown_renders_headings_and_list_items():
    _, markdown = normalize(a_docling_document(), source_sha256="a" * 64,
                            conversion=conversion())
    assert markdown.startswith("# Methods")
    assert "- First point." in markdown
    assert "Journal Name" not in markdown


def test_page_sizes_come_from_the_pages_map():
    document, _ = normalize(a_docling_document(), source_sha256="a" * 64,
                            conversion=conversion())
    assert document.source.pages == 1
    assert document.source.page_sizes[0].width == 595.3
    assert document.format_version == FORMAT_VERSION


def test_strip_page_images_removes_the_renders_and_keeps_the_sizes():
    # Measured on a real paper: 16.23 of 17 MB were base64 page renders,
    # and --image-export-mode referenced does not cover the JSON output.
    stripped = strip_page_images(a_docling_document())
    page = stripped["pages"]["1"]
    assert "image" not in page
    assert page["size"]["width"] == 595.3


def test_strip_page_images_does_not_mutate_its_input():
    original = a_docling_document()
    strip_page_images(original)
    assert "image" in original["pages"]["1"]


def test_an_item_without_provenance_is_skipped_rather_than_crashing():
    payload = a_docling_document()
    payload["texts"][1].pop("prov")
    document, _ = normalize(payload, source_sha256="a" * 64, conversion=conversion())
    assert "We did things." not in [b.text for b in document.blocks]


def test_picture_items_are_rendered_as_image_blocks():
    payload = a_docling_document()
    picture_item = {
        "self_ref": "#/pictures/0",
        "label": "picture",
        "content_layer": "body",
        "prov": [{
            "page_no": 1,
            "bbox": {"l": 72.0, "t": 620.0, "r": 523.0, "b": 510.0,
                     "coord_origin": "BOTTOMLEFT"},
            "charspan": [0, 0],
        }],
    }
    payload["pictures"].append(picture_item)
    payload["body"]["children"].append({"$ref": "#/pictures/0"})

    document, markdown = normalize(payload, source_sha256="a" * 64, conversion=conversion())
    image_block = [b for b in document.blocks if b.kind == "image"][0]
    assert image_block.text == "<!-- image -->"
    assert image_block.md_start is not None
    assert image_block.md_end is not None
    assert markdown[image_block.md_start:image_block.md_end] == "<!-- image -->"


def test_picture_with_caption_text_keeps_its_text_with_exact_offsets():
    payload = a_docling_document()
    picture_item = {
        "self_ref": "#/pictures/0",
        "label": "picture",
        "content_layer": "body",
        "text": "Figure 1: results.",
        "prov": [{
            "page_no": 1,
            "bbox": {"l": 72.0, "t": 620.0, "r": 523.0, "b": 510.0,
                     "coord_origin": "BOTTOMLEFT"},
            "charspan": [0, 0],
        }],
    }
    payload["pictures"].append(picture_item)
    payload["body"]["children"].append({"$ref": "#/pictures/0"})

    document, markdown = normalize(payload, source_sha256="a" * 64, conversion=conversion())
    image_block = [b for b in document.blocks if b.kind == "image"][0]
    assert image_block.text == "Figure 1: results."
    assert image_block.md_start is not None
    assert image_block.md_end is not None
    assert markdown[image_block.md_start:image_block.md_end] == "Figure 1: results."


def test_markup_characters_in_text_reach_the_markdown_unescaped():
    # Docling escapes for markup only in its markdown serializer
    # (html.escape plus underscore escaping); the JSON carries raw text.
    # The markdown here is built from that raw text, so "&" must stay "&".
    # Guards against a refactor that swaps the local rendering for
    # docling's export_to_markdown, which would silently reintroduce
    # "&amp;" and break every byte-exact quote check downstream.
    payload = a_docling_document()
    quoted = "Webster & Watson's <advice> on the p_value."
    payload["texts"][1]["text"] = quoted
    payload["texts"][1]["prov"][0]["charspan"][1] = len(quoted)

    document, markdown = normalize(payload, source_sha256="a" * 64,
                                   conversion=conversion())
    block = [b for b in document.blocks if b.text.startswith("Webster")][0]
    assert block.text == quoted
    assert markdown[block.md_start:block.md_end] == quoted
    assert "&amp;" not in markdown and "\\_" not in markdown


def test_an_embedded_base64_image_is_stripped_before_offsets_are_computed():
    payload = a_docling_document()
    payload["texts"][1]["text"] = (
        "before ![x](data:image/png;base64,AAAA) after"
    )
    payload["texts"][1]["prov"][0]["charspan"][1] = len(payload["texts"][1]["text"])

    document, markdown = normalize(payload, source_sha256="a" * 64, conversion=conversion())
    block = [b for b in document.blocks if b.text.startswith("before")][0]
    assert block.text == "before <!-- image --> after"
    assert markdown[block.md_start:block.md_end] == block.text


def test_the_probe_verdict_lands_in_the_source():
    document, _ = normalize(
        a_docling_document(), source_sha256="a" * 64, conversion=conversion(),
        probe={"pages": 1, "probe_pages": 1, "probe_chars": 0, "has_text": False})
    assert document.source.text_layer is False
    assert document.source.probe_pages == 1
    assert document.source.probe_chars == 0


def test_strip_page_images_rewrites_picture_uris_relative_to_the_object():
    payload = a_docling_document()
    payload["pictures"].append({
        "self_ref": "#/pictures/0", "label": "picture",
        "image": {"uri": "/tmp/xyz/paper_artifacts/image_000003_deadbeef.png"},
    })
    stripped = strip_page_images(payload)
    assert stripped["pictures"][0]["image"]["uri"] == "images/image_000003_deadbeef.png"
    assert payload["pictures"][0]["image"]["uri"].startswith("/tmp/")


def a_captioned_table_document() -> dict:
    """The fixture with a caption hanging off the table, as Docling gives it.

    Docling parents a caption to the item it describes rather than to the
    body: the text's `parent` is the table, and the table names it in
    `captions`. Nothing in `body.children` points at it.
    """
    payload = a_docling_document()
    caption = text_item("#/texts/5", "caption", "Table 1. Concept Matrix",
                        top=620.0)
    caption["parent"] = {"$ref": "#/tables/0"}
    payload["texts"].append(caption)
    payload["tables"][0]["captions"] = [{"$ref": "#/texts/5"}]
    payload["tables"][0]["children"] = [{"$ref": "#/texts/5"}]
    return payload


def test_a_table_caption_becomes_a_block_before_its_table():
    document, _ = normalize(a_captioned_table_document(), source_sha256="a" * 64,
                            conversion=conversion())
    kinds = [(b.kind, b.text) for b in document.blocks]
    assert ("caption", "Table 1. Concept Matrix") in kinds
    assert kinds.index(("caption", "Table 1. Concept Matrix")) == \
        kinds.index(("table", "| Cell |\n| --- |")) - 1


def test_a_table_caption_reaches_the_markdown_with_exact_offsets():
    document, markdown = normalize(a_captioned_table_document(),
                                   source_sha256="a" * 64, conversion=conversion())
    caption = [b for b in document.blocks if b.kind == "caption"][0]
    assert caption.md_start is not None
    assert markdown[caption.md_start:caption.md_end] == "Table 1. Concept Matrix"


def test_a_picture_caption_becomes_a_block_before_its_picture():
    payload = a_docling_document()
    payload["pictures"].append({
        "self_ref": "#/pictures/0",
        "label": "picture",
        "content_layer": "body",
        "captions": [{"$ref": "#/texts/5"}],
        "prov": [{
            "page_no": 1,
            "bbox": {"l": 72.0, "t": 620.0, "r": 523.0, "b": 510.0,
                     "coord_origin": "BOTTOMLEFT"},
            "charspan": [0, 0],
        }],
    })
    caption = text_item("#/texts/5", "caption", "Figure 1. Ontology", top=500.0)
    caption["parent"] = {"$ref": "#/pictures/0"}
    payload["texts"].append(caption)
    payload["body"]["children"].append({"$ref": "#/pictures/0"})

    document, markdown = normalize(payload, source_sha256="a" * 64,
                                   conversion=conversion())
    texts = [b.text for b in document.blocks]
    assert texts.index("Figure 1. Ontology") == texts.index("<!-- image -->") - 1
    assert "Figure 1. Ontology" in markdown


def test_a_caption_already_in_the_body_tree_is_not_emitted_twice():
    # Docling's output is not stable across versions; a release that put
    # the caption in body.children as well as in captions must not double
    # it, which would corrupt every offset after it.
    payload = a_captioned_table_document()
    payload["body"]["children"].insert(3, {"$ref": "#/texts/5"})
    document, _ = normalize(payload, source_sha256="a" * 64, conversion=conversion())
    assert [b.text for b in document.blocks].count("Table 1. Concept Matrix") == 1
