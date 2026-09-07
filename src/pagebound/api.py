"""What consumers call: give me this PDF's document, converting if needed.

The order matters. Probe before hashing nothing expensive, hash before
converting, and check the store before both, because the common case by
far is a hit.
"""

from __future__ import annotations

import json
import tempfile
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Callable, Iterable, Literal

from pagebound import convert as convert_module
from pagebound import probe as probe_module
from pagebound.errors import (
    ConversionFailedError,
    NoTextLayerError,
    PageboundError,
    SourceUnreadableError,
    TombstonedError,
    TombstonedNoTextLayerError,
)
from pagebound.index import Index
from pagebound.model import Conversion
from pagebound.normalize import normalize, strip_page_images
from pagebound.recipe import DEFAULT_OPTIONS, Recipe
from pagebound.store import Artifact, Store, Tombstone, sha256_of


def _installed_version() -> str:
    """pyproject.toml is the one place the version is written.

    Every artifact carries this as conversion.pagebound_version, so it
    has to be the number the package was installed under, not a literal
    that can drift from it.
    """
    try:
        return version("pagebound")
    except PackageNotFoundError:  # running from a checkout without install
        return "0.0.0+uninstalled"


PAGEBOUND_VERSION = _installed_version()
_NO_TEXT_LAYER_REASON = "no text layer"


@dataclass(frozen=True)
class CacheStatus:
    """What the cache holds for a PDF, without converting.

    `cached` carries `path`; `tombstoned` carries `tombstone`; `absent`
    carries neither. Hashing the file is the whole cost, so a batch can
    announce "12 cached, 3 to convert, 1 gave up" before paying for any
    conversion.
    """

    sha256: str
    state: Literal["cached", "tombstoned", "absent"]
    path: Path | None = None
    tombstone: Tombstone | None = None


@lru_cache(maxsize=1)
def _cached_recipe(converter_version: str) -> Recipe:
    return Recipe(converter="docling", converter_version=converter_version,
                  options=dict(DEFAULT_OPTIONS))


@lru_cache(maxsize=1)
def _default_resolved_version() -> str:
    return convert_module.resolve_version()


def current_recipe(*, run: Callable[..., Any] | None = None) -> Recipe:
    """The recipe for the Docling that would actually run right now."""
    if run is not None:
        return _cached_recipe(convert_module.resolve_version(run=run))
    return _cached_recipe(_default_resolved_version())


def _hash(pdf_path: str | Path) -> str:
    """sha256 of the PDF, with an unreadable file reported as pagebound's own error."""
    try:
        return sha256_of(pdf_path)
    except OSError as error:
        raise SourceUnreadableError(f"cannot read {Path(pdf_path).name}: {error}") from error


def _drop_page_renders(artifacts: Path) -> None:
    """Remove Docling's full-page renders from the artifacts directory.

    `--image-export-mode referenced` writes a PNG of every page beside
    the figures. Nothing references them, and measured on a real
    library they were 97% of images/ and 94% of the whole cache: about
    12 MB per paper against 1 MB for everything else together.
    """
    for render in artifacts.glob("page_*.png"):
        render.unlink()


def _replay(pdf_path: Path, stone: Tombstone) -> None:
    """Raise the tombstone as the error the original failure raised.

    Recorded per recipe, so a new Docling version retries by itself. A
    scan replays as a NoTextLayerError too, so callers can keep telling
    scans from broken PDFs on cached misses.
    """
    kind = (TombstonedNoTextLayerError if stone.reason == _NO_TEXT_LAYER_REASON
            else TombstonedError)
    raise kind(f"{pdf_path.name} was tombstoned: {stone.reason}",
               reason=stone.reason, tombstoned_at=stone.tombstoned_at)


def _stores(store: Store | None, fallback_stores: Iterable[Store]) -> list[Store]:
    """The writable store first, then the read-only fallbacks, in order."""
    return [store or Store(), *fallback_stores]


def cache_status(
    pdf_path: str | Path,
    *,
    store: Store | None = None,
    fallback_stores: Iterable[Store] = (),
    recipe: Recipe | None = None,
) -> CacheStatus:
    """Hash the PDF and report what the caches hold for it, never converting.

    With `recipe=None` the answer spans every recipe: cached when any
    store holds a complete artifact for the file, tombstoned when one
    holds only a tombstone. With a recipe, only that recipe counts.
    Passing a recipe you already hold keeps Docling out of it.
    """
    sha = _hash(pdf_path)
    stores = _stores(store, fallback_stores)
    if recipe is None:
        for candidate in stores:
            if (found := candidate.latest_artifact(sha)) is not None:
                return CacheStatus(sha, "cached", path=found)
        for candidate in stores:
            if stones := candidate.tombstones(sha):
                return CacheStatus(sha, "tombstoned", tombstone=stones[-1])
        return CacheStatus(sha, "absent")

    for candidate in stores:
        if candidate.has(sha, recipe.key):
            return CacheStatus(sha, "cached", path=candidate.object_dir(sha, recipe.key))
    for candidate in stores:
        if (stone := candidate.read_tombstone(sha, recipe.key)) is not None:
            return CacheStatus(sha, "tombstoned", tombstone=stone)
    return CacheStatus(sha, "absent")


def get_cached_document(
    pdf_path: str | Path,
    *,
    store: Store | None = None,
    fallback_stores: Iterable[Store] = (),
) -> Artifact | None:
    """The newest complete artifact for a PDF, or None; never converts.

    Reads across every recipe and asks Docling nothing, so it works on a
    machine without Docling and costs one hash. A hit in a fallback is
    copied into `store` first, so the caller's cache ends up holding
    what it read.
    """
    sha = _hash(pdf_path)
    local, *fallbacks = _stores(store, fallback_stores)
    if (found := local.latest_artifact(sha)) is not None:
        return local.read(sha, found.name)
    for candidate in fallbacks:
        if (found := candidate.latest_artifact(sha)) is not None:
            local.adopt(found, sha, found.name)
            return local.read(sha, found.name)
    return None


def get_document(
    pdf_path: str | Path,
    *,
    store: Store | None = None,
    fallback_stores: Iterable[Store] = (),
    recipe: Recipe | None = None,
    convert_on_miss: bool = True,
    converter: Callable[..., Path] | None = None,
    prober: Callable[..., dict[str, Any]] | None = None,
    force: bool = False,
    ocr_scans: bool = False,
    timeout: float | None = None,
) -> Artifact:
    """The Artifact for a PDF, converting on a miss unless told not to.

    `fallback_stores` are read-only caches consulted after `store`: a
    hit there is copied into `store`, a tombstone there replays, and
    nothing is ever written to them. `force=True` reconverts even over
    a cached artifact or a tombstone, and skips the fallbacks. A
    tombstone under this recipe replays as TombstonedError (or its
    NoTextLayerError twin for a scan) rather than retrying.

    A PDF with no text layer is refused and tombstoned unless
    `ocr_scans=True`, which lets Docling OCR it under the same recipe
    and retries an earlier no-text-layer tombstone; the artifact's
    `source.text_layer` records that its text came out of OCR.

    `timeout` is seconds for one Docling run; OCR of a long scan can
    need more than the default. An explicit `recipe` pins its Docling
    version on the converter, so the provenance recorded is the Docling
    that ran.
    """
    pdf_path = Path(pdf_path)
    store = store or Store()
    pinned_version = recipe.converter_version if recipe is not None else None
    recipe = recipe or current_recipe()
    run_convert = converter or convert_module.convert
    run_probe = prober or probe_module.text_layer
    if ocr_scans and not recipe.options.get("ocr", True):
        raise ValueError("ocr_scans=True needs a recipe with the ocr option on")

    sha = _hash(pdf_path)

    if not force:
        if store.has(sha, recipe.key):
            return store.read(sha, recipe.key)
        for fallback in fallback_stores:
            if fallback.has(sha, recipe.key):
                store.adopt(fallback.object_dir(sha, recipe.key), sha, recipe.key)
                return store.read(sha, recipe.key)
        for candidate in (store, *fallback_stores):
            stone = candidate.read_tombstone(sha, recipe.key)
            if stone is None:
                continue
            if ocr_scans and stone.reason == _NO_TEXT_LAYER_REASON:
                break  # the verdict this call was asked to override
            _replay(pdf_path, stone)

    if not convert_on_miss:
        raise ConversionFailedError(
            f"no cached artifact for {pdf_path.name} and conversion is disabled"
        )

    probed = run_probe(pdf_path)
    if not probed["has_text"] and not ocr_scans:
        store.tombstone(sha, recipe.key, _NO_TEXT_LAYER_REASON)
        raise NoTextLayerError(
            f"{pdf_path.name} has no extractable text; pass ocr_scans=True to OCR it"
        )

    with tempfile.TemporaryDirectory() as scratch:
        started = time.monotonic()
        try:
            produced = run_convert(pdf_path, scratch, recipe.options,
                                   timeout=timeout, converter_version=pinned_version)
            duration = time.monotonic() - started
        except ConversionFailedError as error:
            if not store.has(sha, recipe.key):
                store.tombstone(sha, recipe.key, str(error))
            raise

        docling = json.loads(Path(produced).read_text(encoding="utf-8"))
        conversion = Conversion(
            converter=recipe.converter,
            converter_version=recipe.converter_version,
            options=dict(recipe.options),
            recipe=recipe.key,
            converted_at=datetime.now(UTC).isoformat(timespec="seconds"),
            pagebound_version=PAGEBOUND_VERSION,
            duration_seconds=round(duration, 3),
        )
        document, markdown = normalize(docling, source_sha256=sha,
                                       conversion=conversion, probe=probed)
        artifacts = next(Path(scratch).glob("*_artifacts"), None)
        if artifacts is not None:
            _drop_page_renders(artifacts)
        store.write(sha, recipe.key, document, markdown,
                    strip_page_images(docling), images=artifacts)

    return replace(store.read(sha, recipe.key), converted=True)


def get_document_for_item(
    library_key: str,
    item_key: str,
    *,
    store: Store | None = None,
    index: Index | None = None,
    recipe: Recipe | None = None,
    convert_on_miss: bool = True,
    converter: Callable[..., Path] | None = None,
    prober: Callable[..., dict[str, Any]] | None = None,
    force: bool = False,
) -> Artifact:
    """The Artifact for a Zotero item, without hashing when it can be helped.

    When a stat shows the PDF unchanged, the recorded sha goes straight
    to the store. A changed file takes the normal hashing path; the
    index entry itself is the sync's to refresh, not this function's.
    """
    store = store or Store()
    index = index or Index()
    recipe = recipe or current_recipe()

    row = index.lookup(library_key, item_key)
    if row is None:
        raise PageboundError(f"no index entry for {library_key}/{item_key}")

    if (not force and index.unchanged(library_key, item_key, row["path"])
            and store.has(row["sha256"], recipe.key)):
        return store.read(row["sha256"], recipe.key)

    return get_document(row["path"], store=store, recipe=recipe,
                        convert_on_miss=convert_on_miss, converter=converter,
                        prober=prober, force=force)


def get_document_for_doi(
    doi: str,
    *,
    store: Store | None = None,
    index: Index | None = None,
    recipe: Recipe | None = None,
    convert_on_miss: bool = True,
    converter: Callable[..., Path] | None = None,
    prober: Callable[..., dict[str, Any]] | None = None,
    force: bool = False,
) -> Artifact:
    """The Artifact for a DOI, through the index rather than a live Zotero.

    The index is a snapshot of the last sync, so this is the fallback
    for when Zotero is closed, not a substitute for a live join. A DOI
    held in more than one library resolves to the user library's copy.
    """
    index = index or Index()
    rows = index.lookup_doi(doi)
    if not rows:
        raise PageboundError(f"no index entry for DOI {doi}")
    row = min(rows, key=lambda r: (r["library_key"] != "user", r["library_key"]))
    return get_document_for_item(
        row["library_key"], row["item_key"], store=store, index=index,
        recipe=recipe, convert_on_miss=convert_on_miss, converter=converter,
        prober=prober, force=force)
