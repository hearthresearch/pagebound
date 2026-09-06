"""Zotero identity to source hash.

Consumers must never hash a library's worth of PDFs to find out whether
the cache holds them; for a 5,900-item library that reads every file from
disk on every run. The sync walks Zotero and knows both sides, so it
records the mapping once and everyone else reads it.

`size` and `mtime` give a repeat sync a skip that costs a stat rather than
a full hash. They are a fast path, not the identity: the hash remains the
thing an artifact is keyed on.

`library_key` is `user` or `group:<id>`, the identity Zotero plugins
already use, so group libraries need no retrofit.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterator, Sequence

from pagebound.errors import UnsupportedFormatError
from pagebound.store import default_root

_DOI_PREFIXES = (
    "https://doi.org/", "http://doi.org/",
    "https://dx.doi.org/", "http://dx.doi.org/",
    "doi:",
)


def normalize_doi(doi: str | None) -> str | None:
    """Lowercase, prefix-stripped DOI, or None for anything empty.

    Public so consumers can normalise on their side before a lookup;
    the index applies it on every write, so both sides always agree.
    """
    if not doi:
        return None
    value = doi.strip().lower()
    for prefix in _DOI_PREFIXES:
        if value.startswith(prefix):
            value = value[len(prefix):]
            break
    return value or None


SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    library_key TEXT NOT NULL,
    item_key    TEXT NOT NULL,
    sha256      TEXT NOT NULL,
    path        TEXT NOT NULL,
    size        INTEGER NOT NULL,
    mtime       REAL NOT NULL,
    title       TEXT NOT NULL DEFAULT '',
    authors     TEXT NOT NULL DEFAULT '[]',
    year        INTEGER,
    doi         TEXT,
    PRIMARY KEY (library_key, item_key)
);
CREATE INDEX IF NOT EXISTS items_sha256 ON items (sha256);
CREATE INDEX IF NOT EXISTS items_doi ON items (doi);
CREATE TABLE IF NOT EXISTS item_collections (
    library_key TEXT NOT NULL,
    item_key    TEXT NOT NULL,
    collection  TEXT NOT NULL,
    PRIMARY KEY (library_key, item_key, collection)
);
CREATE INDEX IF NOT EXISTS item_collections_collection
    ON item_collections (collection);
"""


class Index:
    def __init__(self, root: str | Path | None = None):
        base = Path(root).expanduser() if root else default_root()
        base.mkdir(parents=True, exist_ok=True)
        self.path = base / "index.sqlite"
        self._db = sqlite3.connect(self.path)
        self._db.row_factory = sqlite3.Row
        # The cache is shared across consumer processes, so a concurrent
        # reader must wait for a writer rather than fail with "database
        # is locked".
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._migrate()

    def _migrate(self) -> None:
        """Create the schema on a fresh file; refuse any version but ours.

        The index is a snapshot the sync rebuilds in seconds, so the
        migration for an unknown version is to delete the file and sync
        again. That also covers an index written by a newer pagebound,
        which an in-place upgrade path would have accepted in silence.
        """
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, SCHEMA_VERSION):
            raise UnsupportedFormatError(
                f"{self.path} has schema version {version}; this pagebound reads "
                f"{SCHEMA_VERSION}. Delete the file and run `pagebound sync`."
            )
        self._db.executescript(_SCHEMA)
        # The schema version non-Python readers check before trusting
        # column names. A fresh SQLite file reports 0, so 0 means new.
        self._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self._db.commit()

    def upsert(
        self, library_key: str, item_key: str, sha256: str,
        pdf_path: str | Path, size: int, mtime: float,
        *, title: str = "", authors: Sequence[str] = (),
        year: int | None = None, doi: str | None = None,
        collections: Sequence[str] = (),
    ) -> None:
        self._db.execute(
            "INSERT INTO items (library_key, item_key, sha256, path, size, mtime, "
            "title, authors, year, doi) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (library_key, item_key) DO UPDATE SET "
            "sha256 = excluded.sha256, path = excluded.path, "
            "size = excluded.size, mtime = excluded.mtime, "
            "title = excluded.title, authors = excluded.authors, "
            "year = excluded.year, doi = excluded.doi",
            (library_key, item_key, sha256, str(pdf_path), size, mtime,
             title, json.dumps(list(authors), ensure_ascii=False),
             year, normalize_doi(doi)),
        )
        # Replace, not accumulate: the sync always passes the item's
        # current memberships, so removals in Zotero propagate.
        self._db.execute(
            "DELETE FROM item_collections WHERE library_key = ? AND item_key = ?",
            (library_key, item_key),
        )
        self._db.executemany(
            "INSERT OR IGNORE INTO item_collections VALUES (?, ?, ?)",
            [(library_key, item_key, name) for name in collections],
        )
        self._db.commit()

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict[str, Any]:
        record = dict(row)
        # Stored as JSON so the faithful list survives; decoded here so
        # Python consumers never touch the encoding.
        record["authors"] = json.loads(record["authors"])
        return record

    def lookup(self, library_key: str, item_key: str) -> dict[str, Any] | None:
        row = self._db.execute(
            "SELECT * FROM items WHERE library_key = ? AND item_key = ?",
            (library_key, item_key),
        ).fetchone()
        return self._decode(row) if row else None

    def unchanged(self, library_key: str, item_key: str, pdf_path: str | Path) -> bool:
        """True when size and mtime still match, so hashing can be skipped."""
        row = self.lookup(library_key, item_key)
        if row is None:
            return False
        try:
            stat = Path(pdf_path).stat()
        except OSError:
            return False
        return stat.st_size == row["size"] and stat.st_mtime == row["mtime"]

    def iter_items(self, collection: str | None = None) -> Iterator[dict[str, Any]]:
        if collection is None:
            rows = self._db.execute(
                "SELECT * FROM items ORDER BY library_key, item_key")
        else:
            rows = self._db.execute(
                "SELECT items.* FROM items JOIN item_collections "
                "USING (library_key, item_key) "
                "WHERE item_collections.collection = ? "
                "ORDER BY library_key, item_key",
                (collection,),
            )
        for row in rows:
            yield self._decode(row)

    def count(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM items").fetchone()[0]

    def lookup_doi(self, doi: str | None) -> list[dict[str, Any]]:
        """Every item carrying this DOI; normalised before matching.

        A list, not a row: the same paper can live in two libraries. A
        None DOI matches nothing rather than every DOI-less row.
        """
        normalised = normalize_doi(doi)
        if normalised is None:
            return []
        rows = self._db.execute(
            "SELECT * FROM items WHERE doi = ? ORDER BY library_key, item_key",
            (normalised,),
        )
        return [self._decode(row) for row in rows]

    def lookup_sha(self, sha256: str) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT * FROM items WHERE sha256 = ? ORDER BY library_key, item_key",
            (sha256,),
        )
        return [self._decode(row) for row in rows]

    def lookup_sha_prefix(self, prefix: str) -> list[dict[str, Any]]:
        """Every item whose sha256 starts with the prefix, git style.

        Ordered by sha first so a caller can tell one match from an
        ambiguous prefix by counting distinct hashes.
        """
        rows = self._db.execute(
            "SELECT * FROM items WHERE substr(sha256, 1, ?) = ? "
            "ORDER BY sha256, library_key, item_key",
            (len(prefix), prefix),
        )
        return [self._decode(row) for row in rows]

    def close(self) -> None:
        self._db.close()
