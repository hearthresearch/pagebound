import sqlite3

import pytest

from pagebound.errors import UnsupportedFormatError
from pagebound.index import SCHEMA_VERSION, Index, normalize_doi


def a_pdf(tmp_path, name="paper.pdf", content=b"pdf bytes"):
    path = tmp_path / name
    path.write_bytes(content)
    return path


def test_lookup_returns_none_for_an_unknown_item(tmp_path):
    assert Index(tmp_path).lookup("user", "ABCD1234") is None


def test_an_upserted_item_is_found(tmp_path):
    pdf = a_pdf(tmp_path)
    index = Index(tmp_path)
    index.upsert("user", "ABCD1234", "a" * 64, pdf,
                 pdf.stat().st_size, pdf.stat().st_mtime)
    row = index.lookup("user", "ABCD1234")
    assert row["sha256"] == "a" * 64
    assert row["path"] == str(pdf)


def test_upsert_replaces_rather_than_duplicates(tmp_path):
    pdf = a_pdf(tmp_path)
    index = Index(tmp_path)
    for sha in ("a" * 64, "b" * 64):
        index.upsert("user", "ABCD1234", sha, pdf,
                     pdf.stat().st_size, pdf.stat().st_mtime)
    assert index.count() == 1
    assert index.lookup("user", "ABCD1234")["sha256"] == "b" * 64


def test_a_group_library_is_a_separate_item(tmp_path):
    pdf = a_pdf(tmp_path)
    index = Index(tmp_path)
    for library in ("user", "group:12345"):
        index.upsert(library, "ABCD1234", "a" * 64, pdf,
                     pdf.stat().st_size, pdf.stat().st_mtime)
    assert index.count() == 2
    assert index.lookup("group:12345", "ABCD1234") is not None


def test_unchanged_is_true_when_size_and_mtime_still_match(tmp_path):
    pdf = a_pdf(tmp_path)
    index = Index(tmp_path)
    index.upsert("user", "ABCD1234", "a" * 64, pdf,
                 pdf.stat().st_size, pdf.stat().st_mtime)
    assert index.unchanged("user", "ABCD1234", pdf) is True


def test_unchanged_is_false_after_the_pdf_is_replaced(tmp_path):
    # This is the failure mode that motivates the whole project: tools
    # that key on filename leave a stale artifact behind silently.
    pdf = a_pdf(tmp_path)
    index = Index(tmp_path)
    index.upsert("user", "ABCD1234", "a" * 64, pdf,
                 pdf.stat().st_size, pdf.stat().st_mtime)
    pdf.write_bytes(b"a completely different and longer document")
    assert index.unchanged("user", "ABCD1234", pdf) is False


def test_unchanged_is_false_for_an_unknown_item(tmp_path):
    assert Index(tmp_path).unchanged("user", "NOPE", a_pdf(tmp_path)) is False


def test_unchanged_is_false_when_the_file_is_gone(tmp_path):
    pdf = a_pdf(tmp_path)
    index = Index(tmp_path)
    index.upsert("user", "ABCD1234", "a" * 64, pdf,
                 pdf.stat().st_size, pdf.stat().st_mtime)
    pdf.unlink()
    assert index.unchanged("user", "ABCD1234", pdf) is False


def test_wal_journal_mode_is_enabled(tmp_path):
    # The cache is shared across consumer processes, so a concurrent
    # reader must wait rather than fail with "database is locked".
    index = Index(tmp_path)
    assert index._db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_the_index_survives_being_reopened(tmp_path):
    pdf = a_pdf(tmp_path)
    first = Index(tmp_path)
    first.upsert("user", "ABCD1234", "a" * 64, pdf,
                 pdf.stat().st_size, pdf.stat().st_mtime)
    first.close()
    assert Index(tmp_path).lookup("user", "ABCD1234")["sha256"] == "a" * 64


def test_iter_items_yields_every_row(tmp_path):
    pdf = a_pdf(tmp_path)
    index = Index(tmp_path)
    for key in ("AAAA1111", "BBBB2222"):
        index.upsert("user", key, "a" * 64, pdf,
                     pdf.stat().st_size, pdf.stat().st_mtime)
    assert sorted(r["item_key"] for r in index.iter_items()) == ["AAAA1111", "BBBB2222"]


def test_normalize_doi_lowercases_and_strips_prefixes():
    assert normalize_doi("10.1234/ABC.5") == "10.1234/abc.5"
    assert normalize_doi("https://doi.org/10.1234/abc.5") == "10.1234/abc.5"
    assert normalize_doi("http://dx.doi.org/10.1234/abc.5") == "10.1234/abc.5"
    assert normalize_doi("doi:10.1234/abc.5") == "10.1234/abc.5"
    assert normalize_doi("  10.1234/abc.5  ") == "10.1234/abc.5"


def test_normalize_doi_maps_empty_to_none():
    assert normalize_doi(None) is None
    assert normalize_doi("") is None
    assert normalize_doi("doi:") is None


def test_upsert_records_metadata_and_lookup_returns_it(tmp_path):
    pdf = a_pdf(tmp_path)
    index = Index(tmp_path)
    index.upsert("user", "ABCD1234", "a" * 64, pdf,
                 pdf.stat().st_size, pdf.stat().st_mtime,
                 title="Attention Is All You Need",
                 authors=["Ashish Vaswani", "Noam Shazeer"],
                 year=2017, doi="https://doi.org/10.5555/3295222")
    row = index.lookup("user", "ABCD1234")
    assert row["title"] == "Attention Is All You Need"
    assert row["authors"] == ["Ashish Vaswani", "Noam Shazeer"]
    assert row["year"] == 2017
    assert row["doi"] == "10.5555/3295222"  # normalised at write time


def test_metadata_defaults_keep_old_call_sites_working(tmp_path):
    pdf = a_pdf(tmp_path)
    index = Index(tmp_path)
    index.upsert("user", "ABCD1234", "a" * 64, pdf,
                 pdf.stat().st_size, pdf.stat().st_mtime)
    row = index.lookup("user", "ABCD1234")
    assert row["title"] == ""
    assert row["authors"] == []
    assert row["year"] is None
    assert row["doi"] is None


def test_lookup_doi_finds_the_same_paper_in_two_libraries(tmp_path):
    # The same paper can live in the user library and a group library;
    # returning "the first" would hide that, so the lookup is a list.
    pdf = a_pdf(tmp_path)
    index = Index(tmp_path)
    for library in ("user", "group:12345"):
        index.upsert(library, "ABCD1234", "a" * 64, pdf,
                     pdf.stat().st_size, pdf.stat().st_mtime,
                     doi="10.1234/abc.5")
    rows = index.lookup_doi("https://doi.org/10.1234/ABC.5")
    assert sorted(r["library_key"] for r in rows) == ["group:12345", "user"]


def test_lookup_doi_is_empty_for_an_unknown_or_null_doi(tmp_path):
    index = Index(tmp_path)
    assert index.lookup_doi("10.9999/nope") == []
    assert index.lookup_doi(None) == []


def test_lookup_sha_finds_every_item_sharing_the_file(tmp_path):
    pdf = a_pdf(tmp_path)
    index = Index(tmp_path)
    for key in ("AAAA1111", "BBBB2222"):
        index.upsert("user", key, "a" * 64, pdf,
                     pdf.stat().st_size, pdf.stat().st_mtime)
    assert len(index.lookup_sha("a" * 64)) == 2
    assert index.lookup_sha("b" * 64) == []


def test_upsert_replaces_collection_memberships(tmp_path):
    # Replace, not accumulate: the sync always passes the item's current
    # memberships, so a removal in Zotero propagates on the next sync.
    pdf = a_pdf(tmp_path)
    index = Index(tmp_path)
    index.upsert("user", "ABCD1234", "a" * 64, pdf,
                 pdf.stat().st_size, pdf.stat().st_mtime,
                 collections=["HEART", "Methods"])
    assert [r["item_key"] for r in index.iter_items(collection="HEART")] == ["ABCD1234"]
    index.upsert("user", "ABCD1234", "a" * 64, pdf,
                 pdf.stat().st_size, pdf.stat().st_mtime,
                 collections=["Methods"])
    assert list(index.iter_items(collection="HEART")) == []
    assert [r["item_key"] for r in index.iter_items(collection="Methods")] == ["ABCD1234"]


def test_iter_items_with_an_unknown_collection_is_empty(tmp_path):
    pdf = a_pdf(tmp_path)
    index = Index(tmp_path)
    index.upsert("user", "ABCD1234", "a" * 64, pdf,
                 pdf.stat().st_size, pdf.stat().st_mtime)
    assert list(index.iter_items(collection="Nope")) == []


def test_lookup_sha_prefix_finds_every_item_under_the_prefix(tmp_path):
    pdf = a_pdf(tmp_path)
    index = Index(tmp_path)
    index.upsert("user", "AAAA1111", "abc" + "0" * 61, pdf,
                 pdf.stat().st_size, pdf.stat().st_mtime)
    index.upsert("user", "BBBB2222", "abd" + "0" * 61, pdf,
                 pdf.stat().st_size, pdf.stat().st_mtime)
    assert [row["item_key"] for row in index.lookup_sha_prefix("abc")] == ["AAAA1111"]
    assert len(index.lookup_sha_prefix("ab")) == 2
    assert index.lookup_sha_prefix("ff") == []


def test_a_fresh_index_reports_schema_version_1(tmp_path):
    index = Index(tmp_path)
    assert index._db.execute("PRAGMA user_version").fetchone()[0] == 1
    assert SCHEMA_VERSION == 1


def test_an_index_of_another_version_is_refused_with_the_remedy(tmp_path):
    db = sqlite3.connect(tmp_path / "index.sqlite")
    db.execute("PRAGMA user_version = 7")
    db.commit()
    db.close()
    with pytest.raises(UnsupportedFormatError, match="schema version 7.*pagebound sync"):
        Index(tmp_path)
