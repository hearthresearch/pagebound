import copy
import json
from pathlib import Path

import pytest

import pagebound.api as api
from pagebound.api import (
    PAGEBOUND_VERSION,
    cache_status,
    current_recipe,
    get_cached_document,
    get_document,
    get_document_for_doi,
    get_document_for_item,
)
from pagebound.errors import (
    ConversionFailedError,
    NoTextLayerError,
    PageboundError,
    SourceUnreadableError,
    TombstonedError,
)
from pagebound.index import Index
from pagebound.model import Conversion
from pagebound.normalize import normalize
from pagebound.recipe import DEFAULT_OPTIONS, Recipe
from pagebound.store import Store, sha256_of


def a_recipe() -> Recipe:
    return Recipe(converter="docling", converter_version="2.97.0",
                  options=dict(DEFAULT_OPTIONS))


def a_pdf(tmp_path) -> Path:
    path = tmp_path / "paper.pdf"
    path.write_bytes(b"%PDF-1.7 pretend")
    return path


DOCLING_JSON = {
    "schema_name": "DoclingDocument", "version": "1.10.0",
    "pages": {"1": {"page_no": 1, "size": {"width": 595.3, "height": 790.9},
                    "image": {"uri": "data:image/png;base64,AAAA"}}},
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


def fake_converter(calls: list):
    def convert(pdf_path, output_dir, options, **kwargs):
        calls.append(pdf_path)
        out = Path(output_dir) / "paper.json"
        out.write_text(json.dumps(DOCLING_JSON))
        return out

    return convert


def has_text(_path, **kwargs):
    return {"pages": 1, "probe_pages": 1, "probe_chars": 999, "has_text": True}


def no_text(_path, **kwargs):
    return {"pages": 1, "probe_pages": 1, "probe_chars": 0, "has_text": False}


def test_a_miss_converts_and_the_result_is_readable(tmp_path):
    pdf = a_pdf(tmp_path)
    store = Store(tmp_path / "cache")
    artifact = get_document(
        pdf, store=store, recipe=a_recipe(),
        converter=fake_converter([]), prober=has_text)
    assert artifact.markdown == "Hello."
    assert artifact.document.blocks[0].page == 1


def test_a_hit_does_not_convert_again(tmp_path):
    pdf = a_pdf(tmp_path)
    store = Store(tmp_path / "cache")
    calls: list = []
    for _ in range(2):
        get_document(pdf, store=store, recipe=a_recipe(),
                     converter=fake_converter(calls), prober=has_text)
    assert len(calls) == 1


def test_the_object_is_keyed_on_the_source_hash_and_recipe(tmp_path):
    pdf = a_pdf(tmp_path)
    store = Store(tmp_path / "cache")
    get_document(pdf, store=store, recipe=a_recipe(),
                 converter=fake_converter([]), prober=has_text)
    assert store.has(sha256_of(pdf), a_recipe().key)


def test_the_stored_docling_json_has_no_page_renders(tmp_path):
    pdf = a_pdf(tmp_path)
    store = Store(tmp_path / "cache")
    get_document(pdf, store=store, recipe=a_recipe(),
                 converter=fake_converter([]), prober=has_text)
    path = store.object_dir(sha256_of(pdf), a_recipe().key) / "docling.json"
    assert "image" not in json.loads(path.read_text())["pages"]["1"]


def test_a_scan_is_tombstoned_and_raises(tmp_path):
    pdf = a_pdf(tmp_path)
    store = Store(tmp_path / "cache")
    with pytest.raises(NoTextLayerError):
        get_document(pdf, store=store, recipe=a_recipe(),
                     converter=fake_converter([]), prober=no_text)
    assert store.read_tombstone(sha256_of(pdf), a_recipe().key) is not None


def test_a_tombstoned_source_is_not_retried(tmp_path):
    pdf = a_pdf(tmp_path)
    store = Store(tmp_path / "cache")
    calls: list = []
    for _ in range(2):
        with pytest.raises(NoTextLayerError):
            get_document(pdf, store=store, recipe=a_recipe(),
                         converter=fake_converter(calls), prober=no_text)
    assert calls == []


def test_a_conversion_failure_is_tombstoned(tmp_path):
    pdf = a_pdf(tmp_path)
    store = Store(tmp_path / "cache")

    def explode(*args, **kwargs):
        raise ConversionFailedError("corrupt")

    with pytest.raises(ConversionFailedError):
        get_document(pdf, store=store, recipe=a_recipe(),
                     converter=explode, prober=has_text)
    assert store.read_tombstone(sha256_of(pdf), a_recipe().key).reason == "corrupt"


def test_a_conversion_failure_tombstone_replays_as_conversion_failed(tmp_path):
    pdf = a_pdf(tmp_path)
    store = Store(tmp_path / "cache")
    calls: list = []

    def explode(*args, **kwargs):
        raise ConversionFailedError("corrupt")

    # First call: converter explodes, failure is tombstoned
    with pytest.raises(ConversionFailedError):
        get_document(pdf, store=store, recipe=a_recipe(),
                     converter=explode, prober=has_text)

    # Second call: with a working converter, but the tombstone replays the error
    with pytest.raises(ConversionFailedError) as exc_info:
        get_document(pdf, store=store, recipe=a_recipe(),
                     converter=fake_converter(calls), prober=has_text)

    # Verify it was never re-converted
    assert calls == []
    # Verify the error message indicates it came from the tombstone replay
    assert "was tombstoned" in str(exc_info.value)


def test_convert_on_miss_false_raises_instead_of_converting(tmp_path):
    pdf = a_pdf(tmp_path)
    store = Store(tmp_path / "cache")
    calls: list = []
    with pytest.raises(ConversionFailedError):
        get_document(pdf, store=store, recipe=a_recipe(), convert_on_miss=False,
                     converter=fake_converter(calls), prober=has_text)
    assert calls == []


def test_a_new_recipe_converts_again_and_both_entries_survive(tmp_path):
    pdf = a_pdf(tmp_path)
    store = Store(tmp_path / "cache")
    calls: list = []
    old = a_recipe()
    new = Recipe(converter="docling", converter_version="2.120.3",
                 options=dict(DEFAULT_OPTIONS))
    for recipe in (old, new):
        get_document(pdf, store=store, recipe=recipe,
                     converter=fake_converter(calls), prober=has_text)
    assert len(calls) == 2
    assert store.has(sha256_of(pdf), old.key)
    assert store.has(sha256_of(pdf), new.key)


def test_a_forced_failure_keeps_the_old_artifact(tmp_path):
    pdf = a_pdf(tmp_path)
    store = Store(tmp_path / "cache")
    recipe = a_recipe()

    # First conversion succeeds
    original = get_document(
        pdf, store=store, recipe=recipe,
        converter=fake_converter([]), prober=has_text)

    sha = sha256_of(pdf)
    assert store.has(sha, recipe.key)

    # Forced reconversion with failing converter
    def explode(*args, **kwargs):
        raise ConversionFailedError("reconversion failed")

    with pytest.raises(ConversionFailedError):
        get_document(pdf, store=store, recipe=recipe, force=True,
                     converter=explode, prober=has_text)

    # Old artifact still exists, and the failure did not tombstone it: the
    # on-disk "tombstone XOR artifact" invariant must hold for a consumer
    # that reads the cache directly.
    assert store.has(sha, recipe.key)
    assert store.read_tombstone(sha, recipe.key) is None

    # Plain get_document (no force, working converter, no calls) returns original
    calls: list = []
    artifact = get_document(
        pdf, store=store, recipe=recipe,
        converter=fake_converter(calls), prober=has_text)
    assert artifact.markdown == original.markdown
    assert calls == []


def test_force_bypasses_a_tombstone(tmp_path):
    pdf = a_pdf(tmp_path)
    store = Store(tmp_path / "cache")
    recipe = a_recipe()
    calls: list = []

    # First: tombstone with no-text probe
    with pytest.raises(NoTextLayerError):
        get_document(pdf, store=store, recipe=recipe,
                     converter=fake_converter(calls), prober=no_text)

    # Force=True bypasses the tombstone and reconverts
    artifact = get_document(
        pdf, store=store, recipe=recipe, force=True,
        converter=fake_converter(calls), prober=has_text)

    assert artifact.markdown == "Hello."
    assert artifact.document.blocks[0].page == 1
    assert len(calls) == 1  # Converter was called


def test_current_recipe_memoises_the_resolved_docling_version(monkeypatch):
    api._default_resolved_version.cache_clear()
    calls: list = []

    def fake_resolve_version() -> str:
        calls.append(1)
        return "2.97.0"

    monkeypatch.setattr(api.convert_module, "resolve_version", fake_resolve_version)
    try:
        first = current_recipe()
        second = current_recipe()
    finally:
        api._default_resolved_version.cache_clear()

    assert len(calls) == 1
    assert first is second
    assert first.converter_version == "2.97.0"


def an_indexed_pdf(tmp_path, index: Index, **metadata) -> Path:
    pdf = a_pdf(tmp_path)
    index.upsert("user", "ITEM0001", sha256_of(pdf), pdf,
                 pdf.stat().st_size, pdf.stat().st_mtime, **metadata)
    return pdf


def test_an_unchanged_indexed_item_is_read_without_hashing(tmp_path, monkeypatch):
    store, index = Store(tmp_path / "cache"), Index(tmp_path / "cache")
    pdf = an_indexed_pdf(tmp_path, index)
    get_document(pdf, store=store, recipe=a_recipe(),
                 converter=fake_converter([]), prober=has_text)

    # The fast path is the point: a stat instead of reading the file.
    def refuse_to_hash(_path):
        raise AssertionError("sha256_of must not run on the unchanged path")

    monkeypatch.setattr(api, "sha256_of", refuse_to_hash)
    artifact = get_document_for_item(
        "user", "ITEM0001", store=store, index=index, recipe=a_recipe())
    assert artifact.markdown == "Hello."


def test_a_changed_pdf_falls_back_to_the_hashing_path(tmp_path):
    store, index = Store(tmp_path / "cache"), Index(tmp_path / "cache")
    pdf = an_indexed_pdf(tmp_path, index)
    calls: list = []
    pdf.write_bytes(b"%PDF-1.7 a completely different and longer document")
    artifact = get_document_for_item(
        "user", "ITEM0001", store=store, index=index, recipe=a_recipe(),
        converter=fake_converter(calls), prober=has_text)
    assert artifact.markdown == "Hello."
    assert len(calls) == 1
    assert artifact.document.source.sha256 == sha256_of(pdf)


def test_force_bypasses_the_index_fast_path_and_reconverts(tmp_path):
    store, index = Store(tmp_path / "cache"), Index(tmp_path / "cache")
    pdf = an_indexed_pdf(tmp_path, index)
    calls: list = []
    get_document_for_item("user", "ITEM0001", store=store, index=index,
                          recipe=a_recipe(), converter=fake_converter(calls),
                          prober=has_text)
    assert len(calls) == 1
    assert store.has(sha256_of(pdf), a_recipe().key)

    artifact = get_document_for_item(
        "user", "ITEM0001", store=store, index=index, recipe=a_recipe(),
        force=True, converter=fake_converter(calls), prober=has_text)
    assert artifact.markdown == "Hello."
    assert len(calls) == 2


def test_an_item_absent_from_the_index_raises(tmp_path):
    with pytest.raises(PageboundError, match="no index entry"):
        get_document_for_item("user", "NOPE",
                              store=Store(tmp_path / "cache"),
                              index=Index(tmp_path / "cache"),
                              recipe=a_recipe())


def test_an_unchanged_item_missing_from_the_store_is_converted(tmp_path):
    # Indexed and untouched on disk, but the artifact was gc'd: the
    # fast path must not pretend it is still there.
    store, index = Store(tmp_path / "cache"), Index(tmp_path / "cache")
    an_indexed_pdf(tmp_path, index)
    calls: list = []
    artifact = get_document_for_item(
        "user", "ITEM0001", store=store, index=index, recipe=a_recipe(),
        converter=fake_converter(calls), prober=has_text)
    assert artifact.markdown == "Hello."
    assert len(calls) == 1


def test_a_conversion_records_its_duration(tmp_path):
    pdf = a_pdf(tmp_path)
    store = Store(tmp_path / "cache")
    artifact = get_document(pdf, store=store, recipe=a_recipe(),
                            converter=fake_converter([]), prober=has_text)
    assert artifact.document.conversion.duration_seconds is not None
    assert artifact.document.conversion.duration_seconds >= 0.0


def test_a_tombstone_replays_as_a_typed_error_carrying_its_record(tmp_path):
    pdf = a_pdf(tmp_path)
    store = Store(tmp_path / "cache")

    def explode(*args, **kwargs):
        raise ConversionFailedError("corrupt")

    with pytest.raises(ConversionFailedError):
        get_document(pdf, store=store, recipe=a_recipe(),
                     converter=explode, prober=has_text)
    with pytest.raises(TombstonedError) as caught:
        get_document(pdf, store=store, recipe=a_recipe(),
                     converter=fake_converter([]), prober=has_text)
    assert isinstance(caught.value, ConversionFailedError)
    assert caught.value.reason == "corrupt"
    assert caught.value.tombstoned_at is not None


def test_a_tombstoned_scan_is_both_a_scan_and_a_tombstone(tmp_path):
    pdf = a_pdf(tmp_path)
    store = Store(tmp_path / "cache")
    with pytest.raises(NoTextLayerError):
        get_document(pdf, store=store, recipe=a_recipe(),
                     converter=fake_converter([]), prober=no_text)
    with pytest.raises(NoTextLayerError) as caught:
        get_document(pdf, store=store, recipe=a_recipe(),
                     converter=fake_converter([]), prober=has_text)
    assert isinstance(caught.value, TombstonedError)
    assert caught.value.reason == "no text layer"


def test_a_fresh_failure_is_not_a_tombstoned_error(tmp_path):
    pdf = a_pdf(tmp_path)
    store = Store(tmp_path / "cache")
    with pytest.raises(NoTextLayerError) as caught:
        get_document(pdf, store=store, recipe=a_recipe(),
                     converter=fake_converter([]), prober=no_text)
    assert not isinstance(caught.value, TombstonedError)


def test_the_artifact_says_whether_this_call_converted(tmp_path):
    pdf = a_pdf(tmp_path)
    store = Store(tmp_path / "cache")
    first = get_document(pdf, store=store, recipe=a_recipe(),
                         converter=fake_converter([]), prober=has_text)
    second = get_document(pdf, store=store, recipe=a_recipe(),
                          converter=fake_converter([]), prober=has_text)
    assert first.converted is True
    assert second.converted is False
    assert first.path == second.path == store.object_dir(sha256_of(pdf), a_recipe().key)
    assert (first.path / "document.md").exists()
    assert second.document.source.sha256 == sha256_of(pdf)


def test_cache_status_tells_cached_tombstoned_and_absent_apart(tmp_path):
    store = Store(tmp_path / "cache")
    pdf = a_pdf(tmp_path)
    assert cache_status(pdf, store=store, recipe=a_recipe()).state == "absent"

    get_document(pdf, store=store, recipe=a_recipe(),
                 converter=fake_converter([]), prober=has_text)
    cached = cache_status(pdf, store=store, recipe=a_recipe())
    assert cached.state == "cached"
    assert cached.sha256 == sha256_of(pdf)
    assert cached.path == store.object_dir(sha256_of(pdf), a_recipe().key)
    assert cached.tombstone is None

    scan = tmp_path / "scan.pdf"
    scan.write_bytes(b"%PDF-1.7 pixels only")
    with pytest.raises(NoTextLayerError):
        get_document(scan, store=store, recipe=a_recipe(),
                     converter=fake_converter([]), prober=no_text)
    stoned = cache_status(scan, store=store, recipe=a_recipe())
    assert stoned.state == "tombstoned"
    assert stoned.tombstone.reason == "no text layer"
    assert stoned.path is None


def test_cache_status_never_converts(tmp_path):
    calls: list = []
    store = Store(tmp_path / "cache")
    cache_status(a_pdf(tmp_path), store=store, recipe=a_recipe())
    assert calls == []
    assert list(store.iter_objects()) == []


def test_get_document_for_doi_reaches_the_item_through_the_index(tmp_path):
    store, index = Store(tmp_path / "cache"), Index(tmp_path / "cache")
    pdf = an_indexed_pdf(tmp_path, index, doi="10.5555/3295222")
    calls: list = []
    artifact = get_document_for_doi(
        "https://doi.org/10.5555/3295222", store=store, index=index,
        recipe=a_recipe(), converter=fake_converter(calls), prober=has_text)
    assert artifact.markdown == "Hello."
    assert artifact.document.source.sha256 == sha256_of(pdf)
    assert len(calls) == 1


def test_get_document_for_doi_prefers_the_user_library(tmp_path):
    store, index = Store(tmp_path / "cache"), Index(tmp_path / "cache")
    group_pdf = tmp_path / "group.pdf"
    group_pdf.write_bytes(b"%PDF-1.7 the group copy")
    user_pdf = tmp_path / "user.pdf"
    user_pdf.write_bytes(b"%PDF-1.7 the user copy")
    for library, path, key in (("group:1", group_pdf, "G1"), ("user", user_pdf, "U1")):
        index.upsert(library, key, sha256_of(path), path,
                     path.stat().st_size, path.stat().st_mtime, doi="10.1/x")
    artifact = get_document_for_doi(
        "10.1/x", store=store, index=index, recipe=a_recipe(),
        converter=fake_converter([]), prober=has_text)
    assert artifact.document.source.sha256 == sha256_of(user_pdf)


def test_an_unknown_doi_raises(tmp_path):
    with pytest.raises(PageboundError, match="no index entry"):
        get_document_for_doi("10.9999/nope", store=Store(tmp_path / "cache"),
                             index=Index(tmp_path / "cache"), recipe=a_recipe())


def test_the_version_stamped_into_artifacts_is_the_installed_one():
    from importlib.metadata import version

    import pagebound
    assert PAGEBOUND_VERSION == version("pagebound") == pagebound.__version__
    assert PAGEBOUND_VERSION != "0.0.0"


def docling_is_unavailable(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("Docling must not be consulted on a read path")
    monkeypatch.setattr(api, "current_recipe", refuse)


def test_get_cached_document_reads_the_newest_artifact_without_a_recipe(tmp_path, monkeypatch):
    pdf = a_pdf(tmp_path)
    store = Store(tmp_path / "cache")
    old = Recipe(converter="docling", converter_version="2.100.0",
                 options=dict(DEFAULT_OPTIONS))
    get_document(pdf, store=store, recipe=old,
                 converter=fake_converter([]), prober=has_text)
    get_document(pdf, store=store, recipe=a_recipe(),
                 converter=fake_converter([]), prober=has_text)

    docling_is_unavailable(monkeypatch)
    artifact = get_cached_document(pdf, store=store)
    assert artifact is not None
    assert artifact.markdown == "Hello."
    assert artifact.converted is False
    assert artifact.path == store.object_dir(sha256_of(pdf), a_recipe().key)


def test_get_cached_document_is_none_for_an_unknown_or_tombstoned_pdf(tmp_path, monkeypatch):
    docling_is_unavailable(monkeypatch)
    store = Store(tmp_path / "cache")
    pdf = a_pdf(tmp_path)
    assert get_cached_document(pdf, store=store) is None
    store.tombstone(sha256_of(pdf), "r1", "no text layer")
    assert get_cached_document(pdf, store=store) is None


def test_cache_status_without_a_recipe_looks_across_recipes(tmp_path, monkeypatch):
    pdf = a_pdf(tmp_path)
    store = Store(tmp_path / "cache")
    get_document(pdf, store=store, recipe=a_recipe(),
                 converter=fake_converter([]), prober=has_text)
    docling_is_unavailable(monkeypatch)
    status = cache_status(pdf, store=store, recipe=None)
    assert status.state == "cached"
    assert status.path == store.object_dir(sha256_of(pdf), a_recipe().key)

    scan = tmp_path / "scan.pdf"
    scan.write_bytes(b"%PDF-1.7 pixels")
    store.tombstone(sha256_of(scan), "r1", "no text layer")
    assert cache_status(scan, store=store, recipe=None).state == "tombstoned"
    assert cache_status(scan, store=store, recipe=None).tombstone.reason == "no text layer"


def test_a_fallback_hit_is_copied_into_the_local_store(tmp_path):
    pdf = a_pdf(tmp_path)
    shared, local = Store(tmp_path / "shared"), Store(tmp_path / "local")
    get_document(pdf, store=shared, recipe=a_recipe(),
                 converter=fake_converter([]), prober=has_text)

    def explode(*args, **kwargs):
        raise AssertionError("a fallback hit must not convert")

    artifact = get_document(pdf, store=local, fallback_stores=[shared],
                            recipe=a_recipe(), converter=explode, prober=has_text)
    sha = sha256_of(pdf)
    assert artifact.converted is False
    assert artifact.path == local.object_dir(sha, a_recipe().key)
    assert local.has(sha, a_recipe().key)
    assert (artifact.path / "docling.json").exists()
    assert shared.has(sha, a_recipe().key)  # the fallback is read, never moved


def test_a_fallback_tombstone_replays_without_writing_locally(tmp_path):
    pdf = a_pdf(tmp_path)
    shared, local = Store(tmp_path / "shared"), Store(tmp_path / "local")
    shared.tombstone(sha256_of(pdf), a_recipe().key, "corrupt")
    with pytest.raises(TombstonedError) as caught:
        get_document(pdf, store=local, fallback_stores=[shared], recipe=a_recipe(),
                     converter=fake_converter([]), prober=has_text)
    assert caught.value.reason == "corrupt"
    assert list(local.iter_objects()) == []


def test_a_miss_everywhere_converts_into_the_local_store_only(tmp_path):
    pdf = a_pdf(tmp_path)
    shared, local = Store(tmp_path / "shared"), Store(tmp_path / "local")
    artifact = get_document(pdf, store=local, fallback_stores=[shared],
                            recipe=a_recipe(), converter=fake_converter([]),
                            prober=has_text)
    assert artifact.converted is True
    assert local.has(sha256_of(pdf), a_recipe().key)
    assert list(shared.iter_objects()) == []


def test_force_ignores_fallbacks_and_reconverts(tmp_path):
    pdf = a_pdf(tmp_path)
    shared, local = Store(tmp_path / "shared"), Store(tmp_path / "local")
    get_document(pdf, store=shared, recipe=a_recipe(),
                 converter=fake_converter([]), prober=has_text)
    calls: list = []
    artifact = get_document(pdf, store=local, fallback_stores=[shared],
                            recipe=a_recipe(), force=True,
                            converter=fake_converter(calls), prober=has_text)
    assert artifact.converted is True
    assert len(calls) == 1


def test_get_cached_document_and_cache_status_consult_fallbacks(tmp_path, monkeypatch):
    pdf = a_pdf(tmp_path)
    shared, local = Store(tmp_path / "shared"), Store(tmp_path / "local")
    get_document(pdf, store=shared, recipe=a_recipe(),
                 converter=fake_converter([]), prober=has_text)
    docling_is_unavailable(monkeypatch)
    assert cache_status(pdf, store=local, fallback_stores=[shared],
                        recipe=None).state == "cached"
    artifact = get_cached_document(pdf, store=local, fallback_stores=[shared])
    assert artifact is not None
    assert artifact.path == local.object_dir(sha256_of(pdf), a_recipe().key)


def test_a_text_pdf_records_that_it_had_a_text_layer(tmp_path):
    artifact = get_document(a_pdf(tmp_path), store=Store(tmp_path / "cache"),
                            recipe=a_recipe(), converter=fake_converter([]),
                            prober=has_text)
    assert artifact.document.source.text_layer is True
    assert artifact.document.source.probe_chars == 999


def test_ocr_scans_converts_a_scan_and_marks_the_artifact(tmp_path):
    pdf = a_pdf(tmp_path)
    store = Store(tmp_path / "cache")
    calls: list = []
    artifact = get_document(pdf, store=store, recipe=a_recipe(), ocr_scans=True,
                            converter=fake_converter(calls), prober=no_text)
    assert len(calls) == 1
    assert artifact.converted is True
    assert artifact.document.source.text_layer is False
    assert artifact.document.source.probe_chars == 0
    assert store.read_tombstone(sha256_of(pdf), a_recipe().key) is None


def test_ocr_scans_retries_a_scan_tombstone_but_not_a_failure_tombstone(tmp_path):
    scan = a_pdf(tmp_path)
    store = Store(tmp_path / "cache")
    with pytest.raises(NoTextLayerError):
        get_document(scan, store=store, recipe=a_recipe(),
                     converter=fake_converter([]), prober=no_text)
    calls: list = []
    artifact = get_document(scan, store=store, recipe=a_recipe(), ocr_scans=True,
                            converter=fake_converter(calls), prober=no_text)
    assert len(calls) == 1
    assert artifact.document.source.text_layer is False

    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.7 broken")
    store.tombstone(sha256_of(broken), a_recipe().key, "corrupt")
    with pytest.raises(TombstonedError):
        get_document(broken, store=store, recipe=a_recipe(), ocr_scans=True,
                     converter=fake_converter(calls), prober=no_text)
    assert len(calls) == 1


def test_ocr_scans_needs_a_recipe_with_ocr_on(tmp_path):
    recipe = Recipe(converter="docling", converter_version="2.97.0",
                    options={**DEFAULT_OPTIONS, "ocr": False})
    with pytest.raises(ValueError, match="ocr"):
        get_document(a_pdf(tmp_path), store=Store(tmp_path / "cache"), recipe=recipe,
                     ocr_scans=True, converter=fake_converter([]), prober=no_text)


def recording_converter(seen: list):
    def convert(pdf_path, output_dir, options, **kwargs):
        seen.append(kwargs)
        out = Path(output_dir) / "paper.json"
        out.write_text(json.dumps(DOCLING_JSON))
        return out

    return convert


def test_a_missing_pdf_is_a_pagebound_error_everywhere(tmp_path):
    store = Store(tmp_path / "cache")
    missing = tmp_path / "gone.pdf"
    for call in (
        lambda: get_document(missing, store=store, recipe=a_recipe()),
        lambda: cache_status(missing, store=store, recipe=a_recipe()),
        lambda: get_cached_document(missing, store=store),
    ):
        with pytest.raises(SourceUnreadableError) as caught:
            call()
        assert isinstance(caught.value, PageboundError)
        assert "gone.pdf" in str(caught.value)


def test_the_timeout_reaches_the_converter(tmp_path):
    seen: list = []
    get_document(a_pdf(tmp_path), store=Store(tmp_path / "cache"), recipe=a_recipe(),
                 converter=recording_converter(seen), prober=has_text, timeout=7)
    assert seen[0]["timeout"] == 7


def test_an_explicit_recipe_pins_its_version_on_the_converter(tmp_path, monkeypatch):
    seen: list = []
    get_document(a_pdf(tmp_path), store=Store(tmp_path / "cache"), recipe=a_recipe(),
                 converter=recording_converter(seen), prober=has_text)
    assert seen[0]["converter_version"] == "2.97.0"

    # The current recipe was resolved from what uv would run anyway, so
    # pinning it would only make uv build a second tool environment.
    monkeypatch.setattr(api, "current_recipe", a_recipe)
    other = tmp_path / "other.pdf"
    other.write_bytes(b"%PDF-1.7 other")
    get_document(other, store=Store(tmp_path / "cache"),
                 converter=recording_converter(seen), prober=has_text)
    assert seen[1]["converter_version"] is None


def converter_with_artifacts(calls: list):
    """Writes the JSON plus a Docling-style artifacts dir: figures and page renders."""
    def convert(pdf_path, output_dir, options, **kwargs):
        calls.append(pdf_path)
        out = Path(output_dir) / "paper.json"
        payload = dict(DOCLING_JSON)
        payload["pictures"] = [{"self_ref": "#/pictures/0", "label": "picture",
                                "image": {"uri": str(Path(output_dir) / "paper_artifacts" / "image_000000_abc.png")}}]
        out.write_text(json.dumps(payload))
        artifacts = Path(output_dir) / "paper_artifacts"
        artifacts.mkdir()
        (artifacts / "image_000000_abc.png").write_bytes(b"figure")
        (artifacts / "page_1_def.png").write_bytes(b"render" * 1000)
        (artifacts / "page_2_012.png").write_bytes(b"render" * 1000)
        return out

    return convert


def test_page_renders_are_not_stored_but_figures_are(tmp_path):
    store = Store(tmp_path / "cache")
    artifact = get_document(a_pdf(tmp_path), store=store, recipe=a_recipe(),
                            converter=converter_with_artifacts([]), prober=has_text)
    stored = sorted(p.name for p in (artifact.path / "images").iterdir())
    assert stored == ["image_000000_abc.png"]


def test_stored_picture_uris_point_into_the_object_directory(tmp_path):
    store = Store(tmp_path / "cache")
    artifact = get_document(a_pdf(tmp_path), store=store, recipe=a_recipe(),
                            converter=converter_with_artifacts([]), prober=has_text)
    docling = json.loads((artifact.path / "docling.json").read_text())
    assert docling["pictures"][0]["image"]["uri"] == "images/image_000000_abc.png"
    assert (artifact.path / "images" / "image_000000_abc.png").exists()


DOCLING_WITH_CAPTION = {
    "schema_name": "DoclingDocument", "version": "1.10.0",
    "pages": {"1": {"page_no": 1, "size": {"width": 595.3, "height": 790.9}}},
    "body": {"children": [{"$ref": "#/texts/0"}, {"$ref": "#/tables/0"}]},
    "texts": [
        {"self_ref": "#/texts/0", "label": "text", "content_layer": "body",
         "text": "Hello.",
         "prov": [{"page_no": 1,
                   "bbox": {"l": 1.0, "t": 20.0, "r": 2.0, "b": 10.0,
                            "coord_origin": "BOTTOMLEFT"}, "charspan": [0, 6]}]},
        {"self_ref": "#/texts/1", "label": "caption", "content_layer": "body",
         "text": "Table 1. Concept Matrix", "parent": {"$ref": "#/tables/0"},
         "prov": [{"page_no": 1,
                   "bbox": {"l": 1.0, "t": 40.0, "r": 2.0, "b": 30.0,
                            "coord_origin": "BOTTOMLEFT"}, "charspan": [0, 23]}]},
    ],
    "tables": [{
        "self_ref": "#/tables/0", "label": "table", "content_layer": "body",
        "captions": [{"$ref": "#/texts/1"}],
        "prov": [{"page_no": 1,
                  "bbox": {"l": 1.0, "t": 60.0, "r": 2.0, "b": 50.0,
                           "coord_origin": "BOTTOMLEFT"}, "charspan": [0, 0]}],
        "data": {"num_rows": 1, "num_cols": 1, "table_cells": [
            {"text": "Cell", "start_row_offset_idx": 0,
             "start_col_offset_idx": 0, "column_header": True}]},
    }],
    "pictures": [],
}


def a_stale_artifact(store, sha="a" * 64, recipe_key="r1"):
    """An artifact as the normalisation that dropped captions left it.

    The stored docling.json carries the caption; the document and
    markdown beside it were built from a payload without it, which is
    exactly the shape of every artifact in a store written before the
    walk reached a table's captions.
    """
    full = copy.deepcopy(DOCLING_WITH_CAPTION)
    without = copy.deepcopy(full)
    without["tables"][0].pop("captions")
    conversion = Conversion(
        converter="docling", converter_version="2.97.0", options={},
        recipe=recipe_key, converted_at="2026-08-19T17:12:00Z",
        pagebound_version="0.2.1")
    document, markdown = normalize(without, source_sha256=sha,
                                   conversion=conversion)
    store.write(sha, recipe_key, document, markdown, full)
    return sha, recipe_key


def test_renormalize_recovers_a_caption_an_older_normalisation_dropped(tmp_path):
    store = Store(tmp_path)
    sha, recipe_key = a_stale_artifact(store)
    assert "Table 1. Concept Matrix" not in store.read(sha, recipe_key).markdown

    report = api.renormalize(store=store)

    artifact = store.read(sha, recipe_key)
    assert report.rewritten == 1 and report.scanned == 1
    assert "Table 1. Concept Matrix" in artifact.markdown
    caption = [b for b in artifact.document.blocks if b.kind == "caption"][0]
    assert artifact.markdown[caption.md_start:caption.md_end] == caption.text


def test_renormalize_keeps_converted_at_and_stamps_the_pagebound_that_rewrote_it(tmp_path):
    # Docling did not run again, so the conversion's timestamp still says
    # when it did, and `latest_artifact` picks the same directory as before.
    store = Store(tmp_path)
    sha, recipe_key = a_stale_artifact(store)
    api.renormalize(store=store)
    conversion = store.read(sha, recipe_key).document.conversion
    assert conversion.converted_at == "2026-08-19T17:12:00Z"
    assert conversion.pagebound_version == PAGEBOUND_VERSION


def test_renormalize_rewrites_nothing_on_a_second_run(tmp_path):
    store = Store(tmp_path)
    a_stale_artifact(store)
    api.renormalize(store=store)
    report = api.renormalize(store=store)
    assert report.rewritten == 0 and report.unchanged == 1


def test_renormalize_dry_run_leaves_the_store_alone(tmp_path):
    store = Store(tmp_path)
    sha, recipe_key = a_stale_artifact(store)
    report = api.renormalize(store=store, dry_run=True)
    assert report.rewritten == 1
    assert "Table 1. Concept Matrix" not in store.read(sha, recipe_key).markdown


def test_renormalize_skips_a_directory_that_holds_only_a_tombstone(tmp_path):
    store = Store(tmp_path)
    store.tombstone("b" * 64, "r1", "no text layer")
    report = api.renormalize(store=store)
    assert report.scanned == 0 and report.rewritten == 0
