"""Filling the cache from a Zotero library, as a library call.

`sync_items` takes an iterable of `ZoteroItem` and does the work; it can
be driven by the CLI, by a consumer with its own pyzotero client, or by
a test with no Zotero at all. `zotero_client` builds the local-API
client the CLI uses, and is the only place pyzotero is imported, so the
package installs without it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Iterable

from pagebound.api import get_document
from pagebound.errors import PageboundError, TombstonedError
from pagebound.index import Index
from pagebound.recipe import Recipe
from pagebound.sources.zotero import (
    ZoteroItem,
    attachment_parents,
    iter_items,
    library_key,
)
from pagebound.store import Store, sha256_of

__all__ = [
    "ZoteroItem", "attachment_parents", "iter_items", "library_key",
    "sync_items", "zotero_client",
]


def zotero_client(library: str = "user") -> tuple[Any, str]:
    """A read-only pyzotero client on the desktop app's local API.

    `library` is "user" or a group id. Returns the client and the
    library key (`user` or `group:<id>`) that the index records.
    """
    try:
        from pyzotero import zotero as pyzotero
    except ModuleNotFoundError as error:
        raise PageboundError(
            "syncing from Zotero needs pyzotero: uv add \"pagebound[zotero]\""
        ) from error
    library_type = "user" if library == "user" else "group"
    library_id: int | str = 0 if library == "user" else library
    client = pyzotero.Zotero(library_id, library_type, local=True)
    return client, library_key(library_type, library_id)


def sync_items(
    items: Iterable[ZoteroItem],
    *,
    store: Store,
    index: Index,
    recipe: Recipe,
    force: bool = False,
    ocr_scans: bool = False,
    converter: Callable[..., Path] | None = None,
    prober: Callable[..., dict[str, Any]] | None = None,
    on_event: Callable[[str, ZoteroItem, str], None] | None = None,
) -> dict[str, int]:
    """Convert what is missing and record every item in the index."""
    counts = {"converted": 0, "skipped": 0, "failed": 0, "tombstoned": 0}

    for item in items:
        if not force and index.unchanged(item.library_key, item.item_key, item.pdf_path):
            row = index.lookup(item.library_key, item.item_key)
            if row and store.has(row["sha256"], recipe.key):
                index.upsert(item.library_key, item.item_key, row["sha256"], item.pdf_path,
                             row["size"], row["mtime"],
                             title=item.title, authors=item.authors,
                             year=item.year, doi=item.doi,
                             collections=item.collections)
                counts["skipped"] += 1
                if on_event:
                    on_event("skipped", item, row["sha256"])
                continue

        try:
            sha = sha256_of(item.pdf_path)
            stat = item.pdf_path.stat()
        except OSError as error:
            counts["failed"] += 1
            if on_event:
                on_event("failed", item, str(error))
            continue

        # The row goes in before the conversion is attempted: an item
        # whose PDF fails to convert is still an item, and a consumer
        # must be able to find it and see that it is tombstoned rather
        # than wonder why it is missing from the listing.
        index.upsert(item.library_key, item.item_key, sha, item.pdf_path,
                     stat.st_size, stat.st_mtime,
                     title=item.title, authors=item.authors,
                     year=item.year, doi=item.doi,
                     collections=item.collections)
        try:
            artifact = get_document(item.pdf_path, store=store, recipe=recipe,
                                    converter=converter, prober=prober, force=force,
                                    ocr_scans=ocr_scans)
            outcome = "converted" if artifact.converted else "skipped"
            counts[outcome] += 1
            if on_event:
                on_event(outcome, item, sha)
        except TombstonedError as error:
            # Gave up on an earlier run; nothing was retried, so it is
            # not a failure of this run.
            counts["tombstoned"] += 1
            if on_event:
                on_event("tombstoned", item, error.reason)
        except PageboundError as error:
            counts["failed"] += 1
            if on_event:
                on_event("failed", item, str(error))

    return counts
