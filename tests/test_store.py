import json
import os
from datetime import UTC, datetime

import pytest

from pagebound.errors import PageboundError
from pagebound.model import Conversion, Document, PageSize, Source
from pagebound.store import (
    Store,
    default_root,
    sha256_of,
    strip_embedded_images,
)


def a_document(sha="a" * 64) -> Document:
    return Document(
        format_version=1,
        source=Source(sha256=sha, pages=1,
                      page_sizes=[PageSize(page=1, width=595.3, height=790.9)]),
        conversion=Conversion(
            converter="docling", converter_version="2.97.0", options={},
            recipe="a4f21c9e", converted_at="2026-08-19T17:12:00Z",
            pagebound_version="0.1.0"),
        blocks=[],
    )


def test_default_root_honours_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("PAGEBOUND_CACHE", str(tmp_path / "elsewhere"))
    assert default_root() == tmp_path / "elsewhere"


def test_default_root_falls_back_to_xdg_cache(monkeypatch):
    monkeypatch.delenv("PAGEBOUND_CACHE", raising=False)
    assert default_root().name == "pagebound"


def test_objects_shard_on_the_first_two_hash_characters(tmp_path):
    store = Store(tmp_path)
    assert store.object_dir("ab" + "c" * 62, "r1") == (
        tmp_path / "objects" / "ab" / ("ab" + "c" * 62) / "r1"
    )


def test_a_written_object_round_trips(tmp_path):
    store = Store(tmp_path)
    store.write("a" * 64, "r1", a_document(), "# Title", {"schema_name": "x"})
    artifact = store.read("a" * 64, "r1")
    document, markdown = artifact.document, artifact.markdown
    assert markdown == "# Title"
    assert document.source.sha256 == "a" * 64


def test_the_docling_json_is_written_beside_the_document(tmp_path):
    store = Store(tmp_path)
    path = store.write("a" * 64, "r1", a_document(), "# T", {"schema_name": "x"})
    assert json.loads((path / "docling.json").read_text())["schema_name"] == "x"


def test_has_is_false_before_a_write_and_true_after(tmp_path):
    store = Store(tmp_path)
    assert store.has("a" * 64, "r1") is False
    store.write("a" * 64, "r1", a_document(), "", {})
    assert store.has("a" * 64, "r1") is True


def test_two_recipes_of_one_source_coexist(tmp_path):
    # A Docling upgrade must produce a new entry, not redefine the old one.
    store = Store(tmp_path)
    store.write("a" * 64, "old", a_document(), "old text", {})
    store.write("a" * 64, "new", a_document(), "new text", {})
    assert store.read("a" * 64, "old").markdown == "old text"
    assert store.read("a" * 64, "new").markdown == "new text"


def test_a_tombstone_is_recorded_and_readable(tmp_path):
    store = Store(tmp_path)
    store.tombstone("a" * 64, "r1", "no text layer")
    assert store.read_tombstone("a" * 64, "r1").reason == "no text layer"
    assert store.has("a" * 64, "r1") is False


def test_a_tombstone_under_one_recipe_does_not_block_another(tmp_path):
    store = Store(tmp_path)
    store.tombstone("a" * 64, "old", "docling crashed")
    assert store.read_tombstone("a" * 64, "new") is None


def test_images_are_copied_into_the_object(tmp_path):
    source = tmp_path / "artifacts"
    source.mkdir()
    (source / "image_000000.png").write_bytes(b"PNG")
    store = Store(tmp_path / "cache")
    path = store.write("a" * 64, "r1", a_document(), "", {}, images=source)
    assert (path / "images" / "image_000000.png").read_bytes() == b"PNG"


def test_embedded_base64_images_are_stripped_from_the_markdown(tmp_path):
    store = Store(tmp_path)
    store.write("a" * 64, "r1", a_document(),
                "before ![alt](data:image/png;base64,AAAA) after", {})
    markdown = store.read("a" * 64, "r1").markdown
    assert "base64" not in markdown
    assert markdown == "before <!-- image --> after"


def test_iter_objects_lists_every_source_and_recipe(tmp_path):
    store = Store(tmp_path)
    store.write("a" * 64, "r1", a_document(), "", {})
    store.write("b" * 64, "r2", a_document(sha="b" * 64), "", {})
    assert sorted(store.iter_objects()) == [("a" * 64, "r1"), ("b" * 64, "r2")]


def test_sha256_of_a_file_is_the_hex_digest(tmp_path):
    target = tmp_path / "f.bin"
    target.write_bytes(b"hello")
    expected = "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    assert sha256_of(target) == expected


def test_strip_embedded_images_keeps_position_and_drops_payload():
    assert strip_embedded_images("a ![x](data:image/png;base64,ZZ) b") == (
        "a <!-- image --> b"
    )


def test_read_on_a_never_written_object_raises_pagebound_error(tmp_path):
    store = Store(tmp_path)
    with pytest.raises(PageboundError, match="no cached artifact"):
        store.read("a" * 64, "r1")


def test_a_write_over_an_existing_tombstone_clears_it(tmp_path):
    store = Store(tmp_path)
    store.tombstone("a" * 64, "r1", "docling crashed")
    assert store.read_tombstone("a" * 64, "r1").reason == "docling crashed"
    store.write("a" * 64, "r1", a_document(), "", {})
    assert store.read_tombstone("a" * 64, "r1") is None
    assert store.has("a" * 64, "r1") is True


def test_find_sha_prefix_lists_matching_objects(tmp_path):
    store = Store(tmp_path)
    for sha in ("abc" + "0" * 61, "abd" + "0" * 61, "ff" + "0" * 62):
        (store.object_dir(sha, "r1")).mkdir(parents=True)
    assert store.find_sha_prefix("abc") == ["abc" + "0" * 61]
    assert store.find_sha_prefix("ab") == ["abc" + "0" * 61, "abd" + "0" * 61]
    assert store.find_sha_prefix("ee") == []


def an_artifact(store, sha, recipe_key, converted_at="2026-01-01T00:00:00+00:00"):
    path = store.object_dir(sha, recipe_key)
    path.mkdir(parents=True, exist_ok=True)
    (path / "document.md").write_text("text")
    (path / "document.json").write_text(json.dumps(
        {"conversion": {"converted_at": converted_at}}))
    return path


def test_artifacts_lists_only_recipes_holding_a_document(tmp_path):
    store = Store(tmp_path)
    sha = "a" * 64
    an_artifact(store, sha, "r1")
    store.tombstone(sha, "r2", "broken")
    store.object_dir(sha, "r3").mkdir(parents=True)  # torn write, no sentinel
    assert store.artifacts(sha) == ["r1"]
    assert store.artifacts("b" * 64) == []


def test_latest_artifact_is_the_newest_conversion(tmp_path):
    store = Store(tmp_path)
    sha = "a" * 64
    an_artifact(store, sha, "zz-old", "2025-01-01T00:00:00+00:00")
    an_artifact(store, sha, "aa-new", "2026-01-01T00:00:00+00:00")
    assert store.latest_artifact(sha) == store.object_dir(sha, "aa-new")
    assert store.latest_artifact("b" * 64) is None


def test_latest_artifact_falls_back_to_mtime_without_a_timestamp(tmp_path):
    store = Store(tmp_path)
    sha = "a" * 64
    older = an_artifact(store, sha, "older", converted_at="")
    an_artifact(store, sha, "newer", converted_at="")
    os.utime(older / "document.json", (1, 1))
    assert store.latest_artifact(sha) == store.object_dir(sha, "newer")


def test_a_tombstone_records_when_it_was_written(tmp_path):
    store = Store(tmp_path)
    before = datetime.now(UTC).replace(microsecond=0)
    store.tombstone("a" * 64, "r1", "docling crashed")
    stone = store.read_tombstone("a" * 64, "r1")
    assert stone.reason == "docling crashed"
    assert stone.tombstoned_at is not None
    assert before <= stone.tombstoned_at <= datetime.now(UTC)


def test_a_tombstone_written_before_timestamps_reads_with_none(tmp_path):
    store = Store(tmp_path)
    path = store.object_dir("a" * 64, "r1")
    path.mkdir(parents=True)
    (path / "tombstone.json").write_text(json.dumps({"reason": "old"}))
    stone = store.read_tombstone("a" * 64, "r1")
    assert stone.reason == "old"
    assert stone.tombstoned_at is None


def test_tombstones_lists_every_recipe_that_gave_up(tmp_path):
    store = Store(tmp_path)
    store.tombstone("a" * 64, "r1", "first")
    store.tombstone("a" * 64, "r2", "second")
    an_artifact(store, "a" * 64, "r3")
    assert [s.reason for s in store.tombstones("a" * 64)] == ["first", "second"]


def test_read_returns_an_artifact_that_knows_its_directory(tmp_path):
    store = Store(tmp_path)
    store.write("a" * 64, "r1", a_document(), "text", {})
    artifact = store.read("a" * 64, "r1")
    assert artifact.markdown == "text"
    assert artifact.path == store.object_dir("a" * 64, "r1")
    assert artifact.converted is False


def test_adopt_copies_an_object_from_another_store(tmp_path):
    source, target = Store(tmp_path / "a"), Store(tmp_path / "b")
    source.write("a" * 64, "r1", a_document(), "text", {"k": 1})
    (source.object_dir("a" * 64, "r1") / "images").mkdir()
    (source.object_dir("a" * 64, "r1") / "images" / "p.png").write_bytes(b"png")
    target.tombstone("a" * 64, "r1", "stale")
    target.adopt(source.object_dir("a" * 64, "r1"), "a" * 64, "r1")
    assert target.has("a" * 64, "r1")
    assert target.read("a" * 64, "r1").markdown == "text"
    assert (target.object_dir("a" * 64, "r1") / "images" / "p.png").read_bytes() == b"png"
    assert target.read_tombstone("a" * 64, "r1") is None
    assert source.has("a" * 64, "r1")


def test_a_write_leaves_no_staging_directory_behind(tmp_path):
    store = Store(tmp_path)
    images = tmp_path / "imgs"
    images.mkdir()
    (images / "p.png").write_bytes(b"png")
    store.write("a" * 64, "r1", a_document(), "text", {}, images=images)
    source = store.object_dir("a" * 64, "r1").parent
    assert sorted(p.name for p in source.iterdir()) == ["r1"]
    assert (store.object_dir("a" * 64, "r1") / "images" / "p.png").exists()


def test_staging_directories_are_invisible_to_listings(tmp_path):
    store = Store(tmp_path)
    an_artifact(store, "a" * 64, "r1")
    stage = store.object_dir("a" * 64, "r1").with_name(".r2.write-999")
    stage.mkdir()
    (stage / "document.json").write_text("{}")
    store.tombstone("a" * 64, "r3", "x")
    assert store.artifacts("a" * 64) == ["r1"]
    assert list(store.iter_objects()) == [("a" * 64, "r1"), ("a" * 64, "r3")]
    assert [s.reason for s in store.tombstones("a" * 64)] == ["x"]


def test_two_writers_of_one_object_leave_one_coherent_object(tmp_path):
    import threading

    store = Store(tmp_path)
    errors: list = []

    def writer(tag: str):
        try:
            images = tmp_path / f"imgs-{tag}"
            images.mkdir(exist_ok=True)
            (images / "p.png").write_bytes(tag.encode())
            for _ in range(15):
                store.write("a" * 64, "r1", a_document(), tag, {"writer": tag},
                            images=images)
        except Exception as error:  # noqa: BLE001 - surfaced below
            errors.append(error)

    threads = [threading.Thread(target=writer, args=(t,)) for t in ("A", "B")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    path = store.object_dir("a" * 64, "r1")
    markdown = (path / "document.md").read_text()
    docling = json.loads((path / "docling.json").read_text())
    image = (path / "images" / "p.png").read_bytes().decode()
    assert markdown == docling["writer"] == image
    assert sorted(p.name for p in path.parent.iterdir()) == ["r1"]
