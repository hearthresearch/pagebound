from pathlib import Path

import pytest

from pagebound.sources.zotero import (
    ZoteroItem, attachment_path, iter_items, library_key,
)


class FakeZotero:
    """Listing methods return only the first page; everything() expands."""

    def __init__(self, top, children, collections=None):
        self._top = top
        self._children = children
        self._collections = collections or []
        self._full: dict[int, list] = {}
        self.top_calls = 0
        self.children_calls: list[str] = []

    def _page(self, full: list) -> list:
        page = full[:1]
        self._full[id(page)] = full
        return page

    def top(self, **kwargs):
        self.top_calls += 1
        return self._page(self._top)

    def collection_items_top(self, _key, **kwargs):
        return self._page(self._top)

    def items(self, **kwargs):
        # The real endpoint answers an itemKey filter with the items
        # asked for; the local API throws in their children too.
        wanted = set((kwargs.get("itemKey") or "").split(","))
        pool = list(self._top) + [child for kids in self._children.values()
                                  for child in kids]
        return self._page([entry for entry in pool
                           if entry["data"]["key"] in wanted
                           or entry["data"].get("parentItem") in wanted])

    def children(self, key, **kwargs):
        self.children_calls.append(key)
        return self._page(self._children.get(key, []))

    def collections(self, **kwargs):
        return self._page(self._collections)

    def everything(self, result):
        return self._full[id(result)]


def an_item(key, title="A paper", **data):
    return {"key": key, "data": {"key": key, "title": title,
                                 "itemType": "journalArticle", **data}}


def a_pdf_attachment(key, filename, parent):
    return {"key": key, "data": {
        "key": key, "itemType": "attachment", "linkMode": "imported_file",
        "contentType": "application/pdf", "filename": filename,
        "parentItem": parent,
    }}


def test_library_key_for_the_user_library():
    assert library_key("user", 12345) == "user"


def test_library_key_for_a_group_library():
    assert library_key("group", 98765) == "group:98765"


def test_attachment_path_joins_storage_key_and_filename(tmp_path):
    storage = tmp_path / "storage" / "AAAA1111"
    storage.mkdir(parents=True)
    (storage / "paper.pdf").write_bytes(b"pdf")
    attachment = a_pdf_attachment("AAAA1111", "paper.pdf", "PARENT01")
    assert attachment_path(attachment, tmp_path / "storage") == storage / "paper.pdf"


def test_attachment_path_is_none_when_the_file_is_absent(tmp_path):
    attachment = a_pdf_attachment("AAAA1111", "paper.pdf", "PARENT01")
    assert attachment_path(attachment, tmp_path / "storage") is None


def test_attachment_path_is_none_for_a_non_pdf(tmp_path):
    attachment = a_pdf_attachment("AAAA1111", "notes.txt", "PARENT01")
    attachment["data"]["contentType"] = "text/plain"
    assert attachment_path(attachment, tmp_path / "storage") is None


def test_a_linked_url_attachment_is_skipped(tmp_path):
    attachment = a_pdf_attachment("AAAA1111", "paper.pdf", "PARENT01")
    attachment["data"]["linkMode"] = "linked_url"
    assert attachment_path(attachment, tmp_path / "storage") is None


def test_iter_items_yields_one_item_per_pdf(tmp_path):
    storage = tmp_path / "storage" / "ATT00001"
    storage.mkdir(parents=True)
    (storage / "paper.pdf").write_bytes(b"pdf")
    zot = FakeZotero(
        top=[an_item("ITEM0001")],
        children={"ITEM0001": [a_pdf_attachment("ATT00001", "paper.pdf", "ITEM0001")]},
    )
    items = list(iter_items(zot, storage_root=tmp_path / "storage",
                            library="user"))
    assert items == [ZoteroItem(library_key="user", item_key="ITEM0001",
                                title="A paper", pdf_path=storage / "paper.pdf")]


def test_an_item_with_no_pdf_is_skipped(tmp_path):
    zot = FakeZotero(top=[an_item("ITEM0001")], children={"ITEM0001": []})
    assert list(iter_items(zot, storage_root=tmp_path, library="user")) == []


def test_a_full_library_walk_reaches_items_past_the_first_page(tmp_path):
    # 40 items: first page has only the first, but everything() expands.
    # PDF attachment is on the LAST item only.
    storage = tmp_path / "storage" / "ATT00040"
    storage.mkdir(parents=True)
    (storage / "paper.pdf").write_bytes(b"pdf")
    items = [an_item(f"ITEM{n:04d}") for n in range(40)]
    zot = FakeZotero(
        top=items,
        children={"ITEM0039": [a_pdf_attachment("ATT00040", "paper.pdf", "ITEM0039")]},
    )
    results = list(iter_items(zot, storage_root=tmp_path / "storage", library="user"))
    assert len(results) == 1
    assert results[0].item_key == "ITEM0039"


def test_an_items_pdf_past_the_first_page_of_children_is_found(tmp_path):
    # One item with multiple children: non-PDF note first, PDF second.
    # A bare children() sees only the note; everything() expands.
    storage = tmp_path / "storage" / "ATT00002"
    storage.mkdir(parents=True)
    (storage / "paper.pdf").write_bytes(b"pdf")
    zot = FakeZotero(
        top=[an_item("ITEM0001")],
        children={
            "ITEM0001": [
                {"key": "ATT00001", "data": {
                    "key": "ATT00001", "itemType": "attachment",
                    "linkMode": "imported_file", "contentType": "text/plain",
                    "filename": "notes.txt", "parentItem": "ITEM0001",
                }},
                a_pdf_attachment("ATT00002", "paper.pdf", "ITEM0001"),
            ],
        },
    )
    results = list(iter_items(zot, storage_root=tmp_path / "storage", library="user"))
    assert len(results) == 1
    assert results[0].item_key == "ITEM0001"


def test_iter_items_with_a_collection_resolves_it(tmp_path):
    storage = tmp_path / "storage" / "ATT00001"
    storage.mkdir(parents=True)
    (storage / "paper.pdf").write_bytes(b"pdf")
    zot = FakeZotero(
        top=[an_item("ITEM0001")],
        children={"ITEM0001": [a_pdf_attachment("ATT00001", "paper.pdf", "ITEM0001")]},
        collections=[{"key": "COLL0001", "data": {"name": "Chapter 3", "parentCollection": False}}],
    )
    results = list(iter_items(zot, storage_root=tmp_path / "storage",
                              library="user", collection="Chapter 3"))
    assert len(results) == 1
    assert results[0].item_key == "ITEM0001"


def test_an_unknown_collection_raises(tmp_path):
    zot = FakeZotero(top=[], children={}, collections=[])
    with pytest.raises(LookupError, match="no Zotero collection named"):
        list(iter_items(zot, storage_root=tmp_path, library="user", collection="Nope"))


def test_iter_items_carries_bibliographic_metadata(tmp_path):
    storage = tmp_path / "storage" / "ATT00001"
    storage.mkdir(parents=True)
    (storage / "paper.pdf").write_bytes(b"pdf")
    zot = FakeZotero(
        top=[an_item(
            "ITEM0001",
            creators=[
                {"creatorType": "author", "firstName": "Ada", "lastName": "Lovelace"},
                {"creatorType": "author", "name": "The Analytical Society"},
                {"creatorType": "editor", "firstName": "Charles", "lastName": "Babbage"},
            ],
            date="2017-06-12",
            DOI="10.5555/3295222",
        )],
        children={"ITEM0001": [a_pdf_attachment("ATT00001", "paper.pdf", "ITEM0001")]},
    )
    item = next(iter_items(zot, storage_root=tmp_path / "storage", library="user"))
    # Editors are still creators; filtering by role is consumer policy.
    assert item.authors == ("Ada Lovelace", "The Analytical Society", "Charles Babbage")
    assert item.year == 2017
    assert item.doi == "10.5555/3295222"


def test_metadata_is_empty_when_zotero_has_none(tmp_path):
    storage = tmp_path / "storage" / "ATT00001"
    storage.mkdir(parents=True)
    (storage / "paper.pdf").write_bytes(b"pdf")
    zot = FakeZotero(
        top=[an_item("ITEM0001")],
        children={"ITEM0001": [a_pdf_attachment("ATT00001", "paper.pdf", "ITEM0001")]},
    )
    item = next(iter_items(zot, storage_root=tmp_path / "storage", library="user"))
    assert item.authors == ()
    assert item.year is None
    assert item.doi is None


def test_year_survives_zotero_free_text_dates(tmp_path):
    storage = tmp_path / "storage" / "ATT00001"
    storage.mkdir(parents=True)
    (storage / "paper.pdf").write_bytes(b"pdf")
    zot = FakeZotero(
        top=[an_item("ITEM0001", date="Spring 1999")],
        children={"ITEM0001": [a_pdf_attachment("ATT00001", "paper.pdf", "ITEM0001")]},
    )
    assert next(iter_items(zot, storage_root=tmp_path / "storage",
                           library="user")).year == 1999


def test_iter_items_maps_collection_keys_to_names(tmp_path):
    storage = tmp_path / "storage" / "ATT00001"
    storage.mkdir(parents=True)
    (storage / "paper.pdf").write_bytes(b"pdf")
    zot = FakeZotero(
        top=[an_item("ITEM0001", collections=["COLL0001", "COLL0002", "GONE9999"])],
        children={"ITEM0001": [a_pdf_attachment("ATT00001", "paper.pdf", "ITEM0001")]},
        collections=[
            {"key": "COLL0001", "data": {"name": "HEART", "parentCollection": False}},
            {"key": "COLL0002", "data": {"name": "Methods", "parentCollection": False}},
        ],
    )
    item = next(iter_items(zot, storage_root=tmp_path / "storage", library="user"))
    # A key with no live collection (deleted in Zotero) is dropped, not kept as a key.
    assert item.collections == ("HEART", "Methods")


def test_collections_default_to_empty(tmp_path):
    storage = tmp_path / "storage" / "ATT00001"
    storage.mkdir(parents=True)
    (storage / "paper.pdf").write_bytes(b"pdf")
    zot = FakeZotero(
        top=[an_item("ITEM0001")],
        children={"ITEM0001": [a_pdf_attachment("ATT00001", "paper.pdf", "ITEM0001")]},
    )
    assert next(iter_items(zot, storage_root=tmp_path / "storage",
                           library="user")).collections == ()


def test_iter_items_with_keys_asks_zotero_for_those_items_only(tmp_path):
    storage = tmp_path / "storage" / "ATT00001"
    storage.mkdir(parents=True)
    (storage / "paper.pdf").write_bytes(b"pdf")
    zot = FakeZotero(
        top=[an_item("ITEM0001"), an_item("ITEM0002")],
        children={"ITEM0001": [a_pdf_attachment("ATT00001", "paper.pdf", "ITEM0001")]},
    )
    results = list(iter_items(zot, storage_root=tmp_path / "storage",
                              library="user", keys=["ITEM0001"]))
    assert [item.item_key for item in results] == ["ITEM0001"]
    # No library walk, and the attachment the local API throws in with
    # the item is not itself treated as an item.
    assert zot.top_calls == 0
    assert zot.children_calls == ["ITEM0001"]


def test_iter_items_with_a_key_zotero_does_not_have_yields_nothing(tmp_path):
    zot = FakeZotero(top=[an_item("ITEM0001")], children={})
    assert list(iter_items(zot, storage_root=tmp_path, library="user",
                           keys=["GONE9999"])) == []


def test_iter_items_keeps_the_order_of_the_keys_asked_for(tmp_path):
    for key in ("ATT00001", "ATT00002"):
        (tmp_path / "storage" / key).mkdir(parents=True)
        (tmp_path / "storage" / key / "paper.pdf").write_bytes(b"pdf")
    zot = FakeZotero(
        top=[an_item("ITEM0001"), an_item("ITEM0002")],
        children={"ITEM0001": [a_pdf_attachment("ATT00001", "paper.pdf", "ITEM0001")],
                  "ITEM0002": [a_pdf_attachment("ATT00002", "paper.pdf", "ITEM0002")]},
    )
    results = list(iter_items(zot, storage_root=tmp_path / "storage",
                              library="user", keys=["ITEM0002", "ITEM0001"]))
    assert [item.item_key for item in results] == ["ITEM0002", "ITEM0001"]


def test_iter_items_refuses_keys_together_with_a_collection(tmp_path):
    zot = FakeZotero(top=[], children={})
    with pytest.raises(ValueError, match="keys and collection"):
        list(iter_items(zot, storage_root=tmp_path, library="user",
                        collection="Chapter 3", keys=["ITEM0001"]))
