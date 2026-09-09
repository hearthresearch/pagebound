import copy
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pagebound import cli
from pagebound.cli import app, item_refs
from pagebound.errors import (
    ConversionFailedError,
    DoclingUnavailableError,
    PageboundError,
)
from pagebound.index import Index
from pagebound.model import Conversion
from pagebound.normalize import normalize
from pagebound.recipe import DEFAULT_OPTIONS, Recipe
from pagebound.sources.zotero import ZoteroItem
from pagebound.store import Store, sha256_of
from pagebound.sync import sync_items

runner = CliRunner()

DOCLING_JSON = {
    "schema_name": "DoclingDocument", "version": "1.10.0",
    "pages": {"1": {"page_no": 1, "size": {"width": 595.3, "height": 790.9}}},
    "body": {"children": [{"$ref": "#/texts/0"}]},
    "texts": [{
        "self_ref": "#/texts/0", "label": "text", "content_layer": "body",
        "text": "Hello.",
        "prov": [{"page_no": 1,
                  "bbox": {"l": 1.0, "t": 20.0, "r": 2.0, "b": 10.0,
                           "coord_origin": "BOTTOMLEFT"},
                  "charspan": [0, 6]}],
    }],
    "tables": [], "pictures": [],
}


def a_recipe() -> Recipe:
    return Recipe(converter="docling", converter_version="2.97.0",
                  options=dict(DEFAULT_OPTIONS))


def fake_converter(calls: list):
    def convert(pdf_path, output_dir, options, **kwargs):
        calls.append(Path(pdf_path))
        out = Path(output_dir) / "paper.json"
        out.write_text(json.dumps(DOCLING_JSON))
        return out

    return convert


def has_text(_path, **kwargs):
    return {"pages": 1, "probe_pages": 1, "probe_chars": 999, "has_text": True}


def no_text(_path, **kwargs):
    return {"pages": 1, "probe_pages": 1, "probe_chars": 0, "has_text": False}


def an_item(tmp_path, key="ITEM0001", name="paper.pdf", **metadata) -> ZoteroItem:
    pdf = tmp_path / name
    pdf.write_bytes(b"%PDF-1.7 " + name.encode())
    return ZoteroItem(library_key="user", item_key=key, title="A paper",
                      pdf_path=pdf, **metadata)


def test_sync_converts_each_item_once(tmp_path):
    calls: list = []
    result = sync_items(
        [an_item(tmp_path, "ITEM0001", "a.pdf"), an_item(tmp_path, "ITEM0002", "b.pdf")],
        store=Store(tmp_path / "cache"), index=Index(tmp_path / "cache"),
        recipe=a_recipe(), converter=fake_converter(calls), prober=has_text)
    assert result == {"converted": 2, "skipped": 0, "failed": 0, "tombstoned": 0}
    assert len(calls) == 2


def test_a_second_sync_skips_unchanged_items(tmp_path):
    calls: list = []
    items = [an_item(tmp_path)]
    store, index = Store(tmp_path / "cache"), Index(tmp_path / "cache")
    for _ in range(2):
        result = sync_items(items, store=store, index=index, recipe=a_recipe(),
                            converter=fake_converter(calls), prober=has_text)
    assert result == {"converted": 0, "skipped": 1, "failed": 0, "tombstoned": 0}
    assert len(calls) == 1


def test_a_replaced_pdf_is_converted_again(tmp_path):
    calls: list = []
    item = an_item(tmp_path)
    store, index = Store(tmp_path / "cache"), Index(tmp_path / "cache")
    sync_items([item], store=store, index=index, recipe=a_recipe(),
               converter=fake_converter(calls), prober=has_text)
    item.pdf_path.write_bytes(b"%PDF-1.7 a completely different document")
    sync_items([item], store=store, index=index, recipe=a_recipe(),
               converter=fake_converter(calls), prober=has_text)
    assert len(calls) == 2


def test_sync_records_the_index_entry(tmp_path):
    item = an_item(tmp_path)
    index = Index(tmp_path / "cache")
    sync_items([item], store=Store(tmp_path / "cache"), index=index,
               recipe=a_recipe(), converter=fake_converter([]), prober=has_text)
    assert index.lookup("user", "ITEM0001")["sha256"] == sha256_of(item.pdf_path)


def test_sync_records_the_metadata_in_the_index(tmp_path):
    item = an_item(tmp_path, authors=("Ada Lovelace",), year=2017,
                   doi="https://doi.org/10.5555/3295222")
    index = Index(tmp_path / "cache")
    sync_items([item], store=Store(tmp_path / "cache"), index=index,
               recipe=a_recipe(), converter=fake_converter([]), prober=has_text)
    row = index.lookup("user", "ITEM0001")
    assert row["title"] == "A paper"
    assert row["authors"] == ["Ada Lovelace"]
    assert row["year"] == 2017
    assert row["doi"] == "10.5555/3295222"


def test_the_skip_path_still_upserts_fresh_metadata(tmp_path):
    item = an_item(tmp_path, year=2017, doi="10.5555/3295222")
    store, index = Store(tmp_path / "cache"), Index(tmp_path / "cache")
    sync_items([item], store=store, index=index, recipe=a_recipe(),
               converter=fake_converter([]), prober=has_text)

    # Same PDF, untouched, but Zotero's metadata changed underneath it. A
    # migrated v0 cache is the same shape: unchanged path, empty metadata.
    updated = ZoteroItem(library_key=item.library_key, item_key=item.item_key,
                         title="A paper (revised)", pdf_path=item.pdf_path,
                         authors=("New Author",), year=2018, doi=item.doi)
    result = sync_items([updated], store=store, index=index, recipe=a_recipe(),
                        converter=fake_converter([]), prober=has_text)
    assert result == {"converted": 0, "skipped": 1, "failed": 0, "tombstoned": 0}
    row = index.lookup("user", "ITEM0001")
    assert row["title"] == "A paper (revised)"
    assert row["authors"] == ["New Author"]
    assert row["year"] == 2018


def test_a_failure_is_counted_and_does_not_stop_the_run(tmp_path):
    def explode(*args, **kwargs):
        raise ConversionFailedError("corrupt")

    result = sync_items(
        [an_item(tmp_path, "ITEM0001", "a.pdf"), an_item(tmp_path, "ITEM0002", "b.pdf")],
        store=Store(tmp_path / "cache"), index=Index(tmp_path / "cache"),
        recipe=a_recipe(), converter=explode, prober=has_text)
    assert result == {"converted": 0, "skipped": 0, "failed": 2, "tombstoned": 0}


def test_force_reconverts_an_unchanged_item(tmp_path):
    calls: list = []
    items = [an_item(tmp_path)]
    store, index = Store(tmp_path / "cache"), Index(tmp_path / "cache")
    sync_items(items, store=store, index=index, recipe=a_recipe(),
               converter=fake_converter(calls), prober=has_text)
    sync_items(items, store=store, index=index, recipe=a_recipe(), force=True,
               converter=fake_converter(calls), prober=has_text)
    assert len(calls) == 2


def test_status_reports_an_empty_cache(tmp_path):
    result = runner.invoke(app, ["status", "--cache", str(tmp_path)])
    assert result.exit_code == 0
    assert "0 objects" in result.stdout


def test_status_counts_objects_and_indexed_items(tmp_path):
    item = an_item(tmp_path)
    sync_items([item], store=Store(tmp_path / "cache"),
               index=Index(tmp_path / "cache"), recipe=a_recipe(),
               converter=fake_converter([]), prober=has_text)
    result = runner.invoke(app, ["status", "--cache", str(tmp_path / "cache")])
    assert "1 objects" in result.stdout
    assert "1 indexed" in result.stdout


def test_path_prints_the_object_directory(tmp_path):
    item = an_item(tmp_path)
    sync_items([item], store=Store(tmp_path / "cache"),
               index=Index(tmp_path / "cache"), recipe=a_recipe(),
               converter=fake_converter([]), prober=has_text)
    result = runner.invoke(app, ["path", "user", "ITEM0001",
                                 "--cache", str(tmp_path / "cache")])
    assert result.exit_code == 0
    assert sha256_of(item.pdf_path) in result.stdout


def test_path_exits_nonzero_for_an_unknown_item(tmp_path):
    result = runner.invoke(app, ["path", "user", "NOPE", "--cache", str(tmp_path)])
    assert result.exit_code != 0


def test_list_shows_indexed_items_with_metadata(tmp_path):
    item = an_item(tmp_path, year=2017, doi="10.5555/3295222")
    sync_items([item], store=Store(tmp_path / "cache"),
               index=Index(tmp_path / "cache"), recipe=a_recipe(),
               converter=fake_converter([]), prober=has_text)
    result = runner.invoke(app, ["list", "--cache", str(tmp_path / "cache")])
    assert result.exit_code == 0
    assert "user/ITEM0001" in result.stdout
    assert "A paper" in result.stdout
    assert "2017" in result.stdout
    assert "cached" in result.stdout


def test_list_json_carries_the_rows_and_cache_state(tmp_path):
    item = an_item(tmp_path, doi="10.5555/3295222")
    sync_items([item], store=Store(tmp_path / "cache"),
               index=Index(tmp_path / "cache"), recipe=a_recipe(),
               converter=fake_converter([]), prober=has_text)
    result = runner.invoke(app, ["list", "--json", "--cache", str(tmp_path / "cache")])
    rows = json.loads(result.stdout)
    assert rows[0]["item_key"] == "ITEM0001"
    assert rows[0]["doi"] == "10.5555/3295222"
    assert rows[0]["cached"] is True


def test_list_marks_an_item_whose_artifact_is_gone(tmp_path):
    # Indexed but never converted (or gc'd): the listing must say so
    # instead of promising an artifact that is not there.
    index = Index(tmp_path / "cache")
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.7 x")
    index.upsert("user", "ITEM0001", "a" * 64, pdf,
                 pdf.stat().st_size, pdf.stat().st_mtime)
    result = runner.invoke(app, ["list", "--json", "--cache", str(tmp_path / "cache")])
    assert json.loads(result.stdout)[0]["cached"] is False


def test_list_json_shows_a_tombstone_only_sha_as_not_cached(tmp_path):
    store, index = Store(tmp_path / "cache"), Index(tmp_path / "cache")
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.7 x")
    sha = sha256_of(pdf)
    index.upsert("user", "ITEM0001", sha, pdf, pdf.stat().st_size, pdf.stat().st_mtime)
    store.tombstone(sha, a_recipe().key, "no text layer")
    result = runner.invoke(app, ["list", "--json", "--cache", str(tmp_path / "cache")])
    assert json.loads(result.stdout)[0]["cached"] is False


def test_path_resolves_a_doi(tmp_path):
    item = an_item(tmp_path, doi="10.5555/3295222")
    sync_items([item], store=Store(tmp_path / "cache"),
               index=Index(tmp_path / "cache"), recipe=a_recipe(),
               converter=fake_converter([]), prober=has_text)
    result = runner.invoke(app, ["path", "--doi", "https://doi.org/10.5555/3295222",
                                 "--cache", str(tmp_path / "cache")])
    assert result.exit_code == 0
    assert sha256_of(item.pdf_path) in result.stdout


def test_path_with_an_unknown_doi_exits_nonzero(tmp_path):
    result = runner.invoke(app, ["path", "--doi", "10.9999/nope",
                                 "--cache", str(tmp_path)])
    assert result.exit_code != 0


def test_path_requires_exactly_one_of_item_or_doi(tmp_path):
    neither = runner.invoke(app, ["path", "--cache", str(tmp_path)])
    both = runner.invoke(app, ["path", "user", "ITEM0001",
                               "--doi", "10.1/x", "--cache", str(tmp_path)])
    assert neither.exit_code != 0
    assert both.exit_code != 0


def test_path_rejects_doi_together_with_a_stray_positional(tmp_path):
    # A bare LIBRARY with --doi passes the by_item/bool(doi) guard but
    # still silently discards the positional; it must be a usage error too.
    result = runner.invoke(app, ["path", "user", "--doi", "10.1/x",
                                 "--cache", str(tmp_path)])
    assert result.exit_code == 2
    assert "give REF, LIBRARY ITEM, or --doi" in result.stderr


def test_path_prints_one_line_per_library_for_a_shared_doi(tmp_path):
    store, index = Store(tmp_path / "cache"), Index(tmp_path / "cache")
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.7 shared")
    same_doi = "10.5555/3295222"
    items = [
        ZoteroItem(library_key="user", item_key="ITEM0001", title="A paper",
                  pdf_path=pdf, doi=same_doi),
        ZoteroItem(library_key="group:12345", item_key="ITEM0002", title="A paper",
                  pdf_path=pdf, doi=same_doi),
    ]
    sync_items(items, store=store, index=index, recipe=a_recipe(),
               converter=fake_converter([]), prober=has_text)
    result = runner.invoke(app, ["path", "--doi", same_doi,
                                 "--cache", str(tmp_path / "cache")])
    assert result.exit_code == 0
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) == 2


def test_list_filters_by_collection(tmp_path):
    inside = an_item(tmp_path, "ITEM0001", "a.pdf", collections=("HEART",))
    outside = an_item(tmp_path, "ITEM0002", "b.pdf", collections=("Methods",))
    sync_items([inside, outside], store=Store(tmp_path / "cache"),
               index=Index(tmp_path / "cache"), recipe=a_recipe(),
               converter=fake_converter([]), prober=has_text)
    result = runner.invoke(app, ["list", "--collection", "HEART",
                                 "--cache", str(tmp_path / "cache")])
    assert result.exit_code == 0
    assert "ITEM0001" in result.stdout
    assert "ITEM0002" not in result.stdout


def synced_item(tmp_path, **metadata):
    item = an_item(tmp_path, **metadata)
    sync_items([item], store=Store(tmp_path / "cache"),
               index=Index(tmp_path / "cache"), recipe=a_recipe(),
               converter=fake_converter([]), prober=has_text)
    return item


def test_path_resolves_a_sha_prefix(tmp_path):
    item = synced_item(tmp_path)
    sha = sha256_of(item.pdf_path)
    result = runner.invoke(app, ["path", sha[:12], "--cache", str(tmp_path / "cache")])
    assert result.exit_code == 0
    assert sha in result.stdout


def test_path_resolves_a_bare_item_key_in_the_user_library(tmp_path):
    item = synced_item(tmp_path, key="MH23L252")
    result = runner.invoke(app, ["path", "MH23L252", "--cache", str(tmp_path / "cache")])
    assert result.exit_code == 0
    assert sha256_of(item.pdf_path) in result.stdout


def test_path_resolves_a_bare_doi(tmp_path):
    item = synced_item(tmp_path, doi="10.5555/3295222")
    result = runner.invoke(app, ["path", "10.5555/3295222",
                                 "--cache", str(tmp_path / "cache")])
    assert result.exit_code == 0
    assert sha256_of(item.pdf_path) in result.stdout


def an_artifact(store, sha, recipe_key, converted_at="2026-01-01T00:00:00+00:00"):
    path = store.object_dir(sha, recipe_key)
    path.mkdir(parents=True, exist_ok=True)
    (path / "document.md").write_text("text")
    (path / "document.json").write_text(json.dumps(
        {"conversion": {"converted_at": converted_at}}))
    return path


def docling_is_unavailable(monkeypatch):
    def refuse():
        raise DoclingUnavailableError("no uv here")
    monkeypatch.setattr("pagebound.cli.current_recipe", refuse)


def test_path_resolves_a_sha_prefix_for_an_object_outside_the_index(tmp_path):
    # `pagebound convert` writes an object without a Zotero item; the
    # prefix must still resolve, straight off the store.
    store = Store(tmp_path / "cache")
    sha = "c0ffee" + "0" * 58
    an_artifact(store, sha, a_recipe().key)
    result = runner.invoke(app, ["path", "c0ffee", "--cache", str(tmp_path / "cache")])
    assert result.exit_code == 0
    assert sha in result.stdout


def test_path_rejects_an_ambiguous_sha_prefix(tmp_path):
    store = Store(tmp_path / "cache")
    for sha in ("c0ffee0" + "0" * 57, "c0ffee1" + "0" * 57):
        an_artifact(store, sha, a_recipe().key)
    result = runner.invoke(app, ["path", "c0ffee", "--cache", str(tmp_path / "cache")])
    assert result.exit_code == 1
    assert "ambiguous" in result.stderr


def test_path_rejects_a_ref_of_no_known_shape(tmp_path):
    result = runner.invoke(app, ["path", "not a ref", "--cache", str(tmp_path)])
    assert result.exit_code == 2


def test_path_needs_no_docling_when_one_recipe_holds_the_artifact(tmp_path, monkeypatch):
    docling_is_unavailable(monkeypatch)
    synced_item(tmp_path)
    result = runner.invoke(app, ["path", "ITEM0001", "--cache", str(tmp_path / "cache")])
    assert result.exit_code == 0, result.stderr
    assert (Path(result.stdout.strip()) / "document.md").exists()
    assert a_recipe().key in result.stdout


def test_path_picks_the_newest_conversion_without_asking_docling(tmp_path, monkeypatch):
    docling_is_unavailable(monkeypatch)
    store = Store(tmp_path / "cache")
    sha = "c0ffee" + "0" * 58
    # The newest conversion sits under the alphabetically earlier key, so
    # sorting by name would pick the wrong one.
    an_artifact(store, sha, "zz-older", "2025-01-01T00:00:00+00:00")
    an_artifact(store, sha, "aa-newer", "2026-01-01T00:00:00+00:00")
    result = runner.invoke(app, ["path", "c0ffee", "--cache", str(tmp_path / "cache")])
    assert result.exit_code == 0, result.stderr
    assert result.stdout.strip().endswith("aa-newer")


def test_path_reports_a_tombstoned_paper_instead_of_a_bare_directory(tmp_path, monkeypatch):
    docling_is_unavailable(monkeypatch)
    store, index = Store(tmp_path / "cache"), Index(tmp_path / "cache")
    item = an_item(tmp_path)
    sha = sha256_of(item.pdf_path)
    store.tombstone(sha, a_recipe().key, "no text layer")
    index.upsert("user", "ITEM0001", sha, item.pdf_path, 1, 1.0)
    result = runner.invoke(app, ["path", "ITEM0001", "--cache", str(tmp_path / "cache")])
    assert result.exit_code == 1
    assert "no text layer" in result.stderr


def test_a_tombstoned_item_is_counted_apart_from_a_fresh_failure(tmp_path):
    store, index = Store(tmp_path / "cache"), Index(tmp_path / "cache")
    item = an_item(tmp_path)
    first = sync_items([item], store=store, index=index, recipe=a_recipe(),
                       converter=fake_converter([]), prober=no_text)
    second = sync_items([item], store=store, index=index, recipe=a_recipe(),
                        converter=fake_converter([]), prober=no_text)
    assert first == {"converted": 0, "skipped": 0, "failed": 1, "tombstoned": 0}
    assert second == {"converted": 0, "skipped": 0, "failed": 0, "tombstoned": 1}


def test_sync_can_convert_scans_with_ocr(tmp_path):
    store, index = Store(tmp_path / "cache"), Index(tmp_path / "cache")
    item = an_item(tmp_path)
    refused = sync_items([item], store=store, index=index, recipe=a_recipe(),
                         converter=fake_converter([]), prober=no_text)
    converted = sync_items([item], store=store, index=index, recipe=a_recipe(),
                           converter=fake_converter([]), prober=no_text, ocr_scans=True)
    assert refused["failed"] == 1
    assert converted["converted"] == 1
    artifact = store.read(sha256_of(item.pdf_path), a_recipe().key)
    assert artifact.document.source.text_layer is False


def test_a_failed_item_still_gets_an_index_row(tmp_path):
    store, index = Store(tmp_path / "cache"), Index(tmp_path / "cache")
    item = an_item(tmp_path, doi="10.1/scan")
    sync_items([item], store=store, index=index, recipe=a_recipe(),
               converter=fake_converter([]), prober=no_text)
    row = index.lookup("user", "ITEM0001")
    assert row is not None
    assert row["sha256"] == sha256_of(item.pdf_path)
    assert row["doi"] == "10.1/scan"


def test_list_reports_a_tombstoned_item_as_such(tmp_path):
    store, index = Store(tmp_path / "cache"), Index(tmp_path / "cache")
    item = an_item(tmp_path)
    sync_items([item], store=store, index=index, recipe=a_recipe(),
               converter=fake_converter([]), prober=no_text)
    result = runner.invoke(app, ["list", "--json", "--cache", str(tmp_path / "cache")])
    row = json.loads(result.stdout)[0]
    assert row["cached"] is False
    assert row["state"] == "tombstoned"
    text = runner.invoke(app, ["list", "--cache", str(tmp_path / "cache")])
    assert "tombstoned" in text.stdout


def test_a_rebuilt_index_counts_cached_items_as_skipped_not_converted(tmp_path):
    store = Store(tmp_path / "cache")
    item = an_item(tmp_path)
    sync_items([item], store=store, index=Index(tmp_path / "cache"), recipe=a_recipe(),
               converter=fake_converter([]), prober=has_text)
    # A fresh index: no row, so no stat fast path, but the store has the artifact.
    (tmp_path / "cache" / "index.sqlite").unlink()
    calls: list = []
    counts = sync_items([item], store=store, index=Index(tmp_path / "cache"),
                        recipe=a_recipe(), converter=fake_converter(calls), prober=has_text)
    assert calls == []
    assert counts == {"converted": 0, "skipped": 1, "failed": 0, "tombstoned": 0}


def two_recipes(tmp_path):
    """One PDF converted under an old and a new recipe, the old one holding a page render."""
    store = Store(tmp_path / "cache")
    item = an_item(tmp_path)
    old = Recipe(converter="docling", converter_version="2.67.0", options=dict(DEFAULT_OPTIONS))
    for recipe in (old, a_recipe()):
        sync_items([item], store=store, index=Index(tmp_path / "cache"), recipe=recipe,
                   force=True, converter=fake_converter([]), prober=has_text)
    sha = sha256_of(item.pdf_path)
    legacy = store.object_dir(sha, a_recipe().key) / "images"
    legacy.mkdir()
    (legacy / "page_1_abc.png").write_bytes(b"x" * 5000)
    (legacy / "image_000000_abc.png").write_bytes(b"y" * 10)
    return store, sha, old


def test_stats_reports_size_per_recipe_and_what_images_cost(tmp_path):
    _store, _sha, old = two_recipes(tmp_path)
    result = runner.invoke(app, ["stats", "--cache", str(tmp_path / "cache")])
    assert result.exit_code == 0, result.stderr
    assert old.key in result.stdout and a_recipe().key in result.stdout
    assert "Page renders" in result.stdout
    as_json = runner.invoke(app, ["stats", "--json", "--cache", str(tmp_path / "cache")])
    data = json.loads(as_json.stdout)
    current = data["recipes"][a_recipe().key]
    assert current["objects"] == 1 and current["artifacts"] == 1
    assert current["page_render_bytes"] == 5000
    assert current["image_bytes"] == 5010
    assert data["removable_bytes"] >= 5000


def test_clean_keeps_only_the_newest_conversion_and_drops_page_renders(tmp_path):
    store, sha, old = two_recipes(tmp_path)
    dry = runner.invoke(app, ["clean", "--dry-run", "--cache", str(tmp_path / "cache")])
    assert dry.exit_code == 0
    assert store.has(sha, old.key)  # dry run touches nothing
    result = runner.invoke(app, ["clean", "--cache", str(tmp_path / "cache")])
    assert result.exit_code == 0, result.stderr
    assert [r for _, r in store.iter_objects()] == [a_recipe().key]
    kept = sorted(p.name for p in (store.object_dir(sha, a_recipe().key) / "images").iterdir())
    assert kept == ["image_000000_abc.png"]
    assert "freed" in result.stdout


def test_clean_never_removes_a_source_that_has_only_tombstones(tmp_path):
    store = Store(tmp_path / "cache")
    store.tombstone("a" * 64, "old", "no text layer")
    store.tombstone("a" * 64, "new", "no text layer")
    runner.invoke(app, ["clean", "--cache", str(tmp_path / "cache")])
    assert len(store.tombstones("a" * 64)) == 2


def test_help_command_shows_the_app_help(tmp_path):
    result = runner.invoke(app, ["help"])
    assert result.exit_code == 0
    assert "Usage: " in result.stdout and "Commands" in result.stdout


def test_help_command_shows_one_commands_help(tmp_path):
    result = runner.invoke(app, ["help", "path"])
    assert result.exit_code == 0
    assert "REF is matched by shape" in result.stdout


def test_help_command_rejects_an_unknown_command(tmp_path):
    result = runner.invoke(app, ["help", "nope"])
    assert result.exit_code == 2
    assert "nope" in result.stderr


def test_a_bare_invocation_shows_help_and_exits_as_a_usage_error():
    result = runner.invoke(app, [])
    assert "Usage: " in result.stdout and "Commands" in result.stdout
    assert result.exit_code == 2  # no command is a usage error, help or not


def test_h_is_a_spelling_of_help():
    assert "Usage: " in runner.invoke(app, ["-h"]).stdout
    assert "REF is matched by shape" in runner.invoke(app, ["path", "-h"]).stdout


def test_version_prints_the_installed_version():
    from pagebound import __version__

    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == f"pagebound {__version__}"


def test_shell_completion_is_offered():
    assert "--install-completion" in runner.invoke(app, ["--help"]).stdout


class FakeZotero:
    """Enough of a pyzotero client for the sync command's wiring."""

    def __init__(self, items=(), children=None):
        self._items = list(items)
        self._children = children or {}
        self._full: dict[int, list] = {}
        self.top_calls = 0
        self.asked_for: list[str] = []

    def _page(self, full: list) -> list:
        page = full[:1]
        self._full[id(page)] = full
        return page

    def collections(self, **kwargs):
        return self._page([])

    def top(self, **kwargs):
        self.top_calls += 1
        return self._page(self._items)

    def items(self, **kwargs):
        keys = (kwargs.get("itemKey") or "").split(",")
        self.asked_for.extend(keys)
        return self._page([entry for entry in self._items
                           if entry["data"]["key"] in keys])

    def children(self, key, **kwargs):
        return self._page(self._children.get(key, []))

    def everything(self, result):
        return self._full[id(result)]


def a_zotero_item(key, attachment, filename):
    item = {"key": key, "data": {"key": key, "title": "A paper",
                                 "itemType": "journalArticle"}}
    child = {"key": attachment, "data": {
        "key": attachment, "itemType": "attachment",
        "linkMode": "imported_file", "contentType": "application/pdf",
        "filename": filename, "parentItem": key,
    }}
    return item, child


def test_a_doi_resolves_to_its_zotero_item(tmp_path):
    synced_item(tmp_path, key="MH23L252", doi="10.5555/3295222")
    pairs = item_refs(["10.5555/3295222"], Index(tmp_path / "cache"), "user")
    assert pairs == [("10.5555/3295222", "MH23L252")]


def test_a_sha_prefix_resolves_to_its_zotero_item(tmp_path):
    item = synced_item(tmp_path, key="MH23L252")
    prefix = sha256_of(item.pdf_path)[:12]
    pairs = item_refs([prefix], Index(tmp_path / "cache"), "user")
    assert pairs == [(prefix, "MH23L252")]


def test_an_item_key_resolves_without_an_index_row(tmp_path):
    # An item Zotero has but pagebound has never seen still syncs.
    assert item_refs(["MH23L252"], Index(tmp_path / "cache"), "user") == [
        ("MH23L252", "MH23L252")]


def test_a_doi_indexed_under_another_library_does_not_resolve(tmp_path):
    index = Index(tmp_path / "cache")
    index.upsert("group:12345", "MH23L252", "a" * 64, tmp_path / "p.pdf", 1, 1.0,
                 doi="10.5555/3295222")
    assert item_refs(["10.5555/3295222"], index, "user") == []


def test_an_ambiguous_sha_prefix_is_refused(tmp_path):
    index = Index(tmp_path / "cache")
    for n, key in enumerate(("MH23L252", "MH23L253")):
        index.upsert("user", key, f"c0ffee{n}" + "0" * 57, tmp_path / "p.pdf", 1, 1.0)
    with pytest.raises(PageboundError, match="ambiguous"):
        item_refs(["c0ffee"], index, "user")


def test_sync_rejects_a_ref_together_with_a_collection(tmp_path):
    result = runner.invoke(app, ["sync", "MH23L252", "--collection", "HEART",
                                 "--cache", str(tmp_path)])
    assert result.exit_code == 2
    assert "not both" in result.stderr


def test_sync_rejects_a_ref_of_no_known_shape(tmp_path):
    result = runner.invoke(app, ["sync", "not a ref", "--cache", str(tmp_path)])
    assert result.exit_code == 2


def test_sync_exits_nonzero_for_a_ref_nothing_indexes(tmp_path):
    result = runner.invoke(app, ["sync", "10.9999/nope", "--cache", str(tmp_path)])
    assert result.exit_code == 1
    assert "10.9999/nope" in result.stderr


def test_sync_with_a_ref_asks_zotero_for_that_item_only(tmp_path, monkeypatch):
    (tmp_path / "ATT00001").mkdir()
    synced_item(tmp_path, key="MH23L252", name="ATT00001/paper.pdf")
    item, child = a_zotero_item("MH23L252", "ATT00001", "paper.pdf")
    other, _ = a_zotero_item("ITEM0002", "ATT00002", "other.pdf")
    zot = FakeZotero(items=[item, other], children={"MH23L252": [child]})
    monkeypatch.setattr(cli, "zotero_client", lambda library="user": (zot, "user"))
    monkeypatch.setattr(cli, "current_recipe", a_recipe)
    result = runner.invoke(app, ["sync", "MH23L252", "--storage", str(tmp_path),
                                 "--cache", str(tmp_path / "cache")])
    assert result.exit_code == 0, result.stderr
    assert "1 skipped" in result.stdout
    assert zot.top_calls == 0
    assert zot.asked_for == ["MH23L252"]


def test_sync_reports_a_ref_zotero_no_longer_has(tmp_path, monkeypatch):
    synced_item(tmp_path, key="MH23L252")
    zot = FakeZotero(items=[], children={})
    monkeypatch.setattr(cli, "zotero_client", lambda library="user": (zot, "user"))
    monkeypatch.setattr(cli, "current_recipe", a_recipe)
    result = runner.invoke(app, ["sync", "MH23L252", "--storage", str(tmp_path),
                                 "--cache", str(tmp_path / "cache")])
    assert result.exit_code == 1
    assert "MH23L252" in result.stderr


def test_sync_points_an_attachment_key_at_the_item_it_hangs_from(tmp_path, monkeypatch):
    # The storage directory is named after the attachment, so that is
    # the key at hand when the PDF is what you are looking at.
    item, child = a_zotero_item("MH23L252", "ATT00001", "paper.pdf")
    zot = FakeZotero(items=[item, child], children={"MH23L252": [child]})
    monkeypatch.setattr(cli, "zotero_client", lambda library="user": (zot, "user"))
    monkeypatch.setattr(cli, "current_recipe", a_recipe)
    result = runner.invoke(app, ["sync", "ATT00001", "--storage", str(tmp_path),
                                 "--cache", str(tmp_path / "cache")])
    assert result.exit_code == 1
    assert "ATT00001 is an attachment of MH23L252" in result.stderr


def test_sync_still_reports_a_key_that_names_no_attachment(tmp_path, monkeypatch):
    zot = FakeZotero(items=[], children={})
    monkeypatch.setattr(cli, "zotero_client", lambda library="user": (zot, "user"))
    monkeypatch.setattr(cli, "current_recipe", a_recipe)
    result = runner.invoke(app, ["sync", "MH23L252", "--storage", str(tmp_path),
                                 "--cache", str(tmp_path / "cache")])
    assert result.exit_code == 1
    assert "no Zotero item with a PDF for MH23L252" in result.stderr


def a_stale_object(tmp_path):
    """An artifact whose stored docling.json has a caption its markdown lost."""
    full = copy.deepcopy(DOCLING_JSON)
    full["texts"].append({
        "self_ref": "#/texts/1", "label": "caption", "content_layer": "body",
        "text": "Table 1. Concept Matrix", "parent": {"$ref": "#/tables/0"},
        "prov": [{"page_no": 1,
                  "bbox": {"l": 1.0, "t": 40.0, "r": 2.0, "b": 30.0,
                           "coord_origin": "BOTTOMLEFT"}, "charspan": [0, 23]}]})
    full["tables"] = [{
        "self_ref": "#/tables/0", "label": "table", "content_layer": "body",
        "captions": [{"$ref": "#/texts/1"}],
        "prov": [{"page_no": 1,
                  "bbox": {"l": 1.0, "t": 60.0, "r": 2.0, "b": 50.0,
                           "coord_origin": "BOTTOMLEFT"}, "charspan": [0, 0]}],
        "data": {"num_rows": 1, "num_cols": 1, "table_cells": [
            {"text": "Cell", "start_row_offset_idx": 0,
             "start_col_offset_idx": 0, "column_header": True}]}}]
    full["body"]["children"] = [{"$ref": "#/texts/0"}, {"$ref": "#/tables/0"}]

    without = copy.deepcopy(full)
    without["tables"][0].pop("captions")
    store = Store(tmp_path / "cache")
    conversion = Conversion(
        converter="docling", converter_version="2.97.0", options={},
        recipe="r1", converted_at="2026-08-19T17:12:00Z",
        pagebound_version="0.2.1")
    document, markdown = normalize(without, source_sha256="a" * 64,
                                   conversion=conversion)
    store.write("a" * 64, "r1", document, markdown, full)
    return store


def test_renormalize_rebuilds_the_documents_and_says_how_many(tmp_path):
    store = a_stale_object(tmp_path)
    result = runner.invoke(app, ["renormalize", "--cache", str(tmp_path / "cache")])
    assert result.exit_code == 0, result.stderr
    assert "Table 1. Concept Matrix" in store.read("a" * 64, "r1").markdown
    assert "1" in result.stdout and "rebuilt" in result.stdout


def test_renormalize_dry_run_touches_nothing(tmp_path):
    store = a_stale_object(tmp_path)
    result = runner.invoke(app, ["renormalize", "--dry-run",
                                 "--cache", str(tmp_path / "cache")])
    assert result.exit_code == 0, result.stderr
    assert "Table 1. Concept Matrix" not in store.read("a" * 64, "r1").markdown
    assert "would rebuild" in result.stdout
