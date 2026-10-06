"""Zotero as a source of PDFs, through the local HTTP API.

Read-only, and the only endpoint touched is localhost. pyzotero is the
supported surface; reading zotero.sqlite directly would be faster but
would couple this package to Zotero's schema.

Pagination pitfall: pyzotero listing calls return only the first page,
about 25 items, by default. Every listing call here goes through
`zot.everything(...)`. Never call a listing method bare.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Collection, Iterator, Sequence

_PDF_LINK_MODES = {"imported_file", "imported_url"}


@dataclass(frozen=True)
class ZoteroItem:
    library_key: str
    item_key: str
    title: str
    pdf_path: Path
    authors: tuple[str, ...] = ()
    year: int | None = None
    doi: str | None = None
    collections: tuple[str, ...] = ()


def library_key(library_type: str, library_id: int | str) -> str:
    """`user` or `group:<id>`, the identity Zotero plugins already use."""
    return "user" if library_type == "user" else f"group:{library_id}"


def attachment_path(attachment: dict[str, Any], storage_root: Path) -> Path | None:
    """The PDF on disk for a stored attachment, or None."""
    data = attachment.get("data") or {}
    if data.get("contentType") != "application/pdf":
        return None
    if data.get("linkMode") not in _PDF_LINK_MODES:
        return None
    filename = data.get("filename")
    key = data.get("key")
    if not filename or not key:
        return None
    candidate = Path(storage_root) / key / filename
    return candidate if candidate.is_file() else None


_YEAR = re.compile(r"\b(\d{4})\b")


def _creator_names(creators: list[dict[str, Any]] | None) -> tuple[str, ...]:
    """Every creator, in Zotero order; filtering by role is consumer policy."""
    names = []
    for creator in creators or []:
        single = creator.get("name")
        joined = " ".join(part for part in (creator.get("firstName"),
                                            creator.get("lastName")) if part)
        if single or joined:
            names.append(single or joined)
    return tuple(names)


def _year_of(date: str | None) -> int | None:
    """First 4-digit run in Zotero's free-text date field, if any."""
    match = _YEAR.search(date or "")
    return int(match.group(1)) if match else None


def _resolve_collection(listing: list[dict[str, Any]], name: str) -> str:
    """A collection key from an unambiguous name or a `/`-separated path.

    The whole name is tried first, because a name can hold a slash
    itself ("AI / Data Science"). Only when no collection carries it is
    it read as a path, matched against the end of each collection's
    path from the library root, so "PhD/Methods" tells apart two
    collections named Methods.
    """
    def norm(text: str) -> str:
        return text.strip().lower()

    by_key = {c["key"]: c.get("data") or {} for c in listing}

    def path(key: str) -> str:
        parts, seen = [], set()
        while key in by_key and key not in seen:
            seen.add(key)
            parts.append(by_key[key].get("name", ""))
            key = by_key[key].get("parentCollection") or ""
        return "/".join(reversed(parts))

    wanted = norm(name)
    matches = [key for key, data in by_key.items() if norm(data.get("name", "")) == wanted]
    if not matches and "/" in wanted:
        paths = {key: norm(path(key)) for key in by_key}
        matches = [key for key, full in paths.items()
                   if full == wanted or full.endswith("/" + wanted)]
    if not matches:
        raise LookupError(f"no Zotero collection named {name!r}")
    if len(matches) > 1:
        raise LookupError(f"the name {name!r} matches {len(matches)} collections; "
                          "give its path, such as 'Parent/Name'")
    return matches[0]


def _subtrees(listing: list[dict[str, Any]], roots: Sequence[str]) -> set[str]:
    """These collection keys and the keys of every collection below them."""
    children: dict[str, list[str]] = {}
    for c in listing:
        parent = (c.get("data") or {}).get("parentCollection")
        if parent:  # False, not None, marks a top-level collection
            children.setdefault(parent, []).append(c["key"])
    found: set[str] = set()
    pending = list(roots)
    while pending:
        key = pending.pop()
        if key not in found:
            found.add(key)
            pending.extend(children.get(key, ()))
    return found


def _items_by_key(zot: Any, keys: Sequence[str]) -> list[dict[str, Any]]:
    """The items these keys name, in the order asked, skipping the unknown.

    The items endpoint under an `itemKey` filter answers a key it does
    not have with nothing, where fetching that key on its own would
    raise; asking for several costs one request either way. The local
    API also throws each item's children into the answer, so the
    response is filtered back down to the keys asked for.
    """
    wanted = list(dict.fromkeys(keys))
    if not wanted:
        return []
    found = {}
    for entry in zot.everything(zot.items(itemKey=",".join(wanted))):
        data = entry.get("data") or {}
        key = data.get("key") or entry.get("key")
        if key in wanted:
            found[key] = entry
    return [found[key] for key in wanted if key in found]


def attachment_parents(zot: Any, keys: Sequence[str]) -> dict[str, str]:
    """For each key that names an attachment, the item key it hangs from.

    An attachment key looks exactly like an item key, and it is the one
    the storage directory is named after, so it is the key at hand when
    the PDF is what you are looking at. Keys naming an item, or naming
    nothing, are absent from the result.
    """
    wanted = list(dict.fromkeys(keys))
    if not wanted:
        return {}
    parents = {}
    for entry in zot.everything(zot.items(itemKey=",".join(wanted))):
        data = entry.get("data") or {}
        key, parent = data.get("key"), data.get("parentItem")
        if key in wanted and parent:
            parents[key] = parent
    return parents


def iter_items(
    zot: Any,
    *,
    storage_root: str | Path,
    library: str,
    collection: str | None = None,
    keys: Sequence[str] | None = None,
    exclude_types: Collection[str] = (),
    exclude_collections: Sequence[str] = (),
) -> Iterator[ZoteroItem]:
    """Yield one ZoteroItem per top-level item that has a PDF on disk.

    `keys` names the items to fetch, one request instead of a walk;
    `collection` scopes a walk to one collection. They are alternatives,
    and neither means the whole library.

    A walk passes over items whose Zotero type is in `exclude_types`
    ("book"), and over items filed anywhere under a collection in
    `exclude_collections`, subcollections included: a shelf such as
    "Books" usually holds its items in subcollections only. Being
    under an excluded collection is enough, even for an item also
    filed elsewhere. Both are refused beside `keys`, which name items
    on purpose.

    Arguments are checked and collections resolved at the call, so a
    collection Zotero does not have raises LookupError before the
    first item is asked for.
    """
    if keys is not None and collection:
        raise ValueError("keys and collection are alternatives, not a pair")
    if keys is not None and (exclude_types or exclude_collections):
        raise ValueError("keys and exclusions are alternatives, not a pair")
    storage_root = Path(storage_root)
    # One listing serves the scope resolution, the exclusions and the
    # key-to-name map for each item's own memberships (data.collections
    # holds keys).
    listing = zot.everything(zot.collections())
    names = {c["key"]: (c.get("data") or {}).get("name", "") for c in listing}
    excluded = _subtrees(listing, [_resolve_collection(listing, name)
                                   for name in exclude_collections])
    if keys is not None:
        tops = _items_by_key(zot, keys)
    elif collection:
        key = _resolve_collection(listing, collection)
        tops = zot.everything(zot.collection_items_top(key))
    else:
        tops = zot.everything(zot.top())
    return _walk(zot, tops, storage_root=storage_root, library=library, names=names,
                 exclude_types=exclude_types, excluded=excluded)


def _walk(
    zot: Any,
    tops: list[dict[str, Any]],
    *,
    storage_root: Path,
    library: str,
    names: dict[str, str],
    exclude_types: Collection[str],
    excluded: set[str],
) -> Iterator[ZoteroItem]:
    for item in tops:
        data = item.get("data") or {}
        item_key = data.get("key") or item.get("key")
        if not item_key:
            continue
        if (data.get("itemType") in exclude_types
                or excluded.intersection(data.get("collections") or ())):
            continue
        for attachment in zot.everything(zot.children(item_key)):
            path = attachment_path(attachment, storage_root)
            if path is None:
                continue
            yield ZoteroItem(
                library_key=library,
                item_key=item_key,
                title=data.get("title", "") or "",
                pdf_path=path,
                authors=_creator_names(data.get("creators")),
                year=_year_of(data.get("date")),
                doi=data.get("DOI") or None,
                # A key with no live collection (deleted in Zotero) is
                # dropped rather than surfaced as an opaque key.
                collections=tuple(names[k] for k in data.get("collections", [])
                                  if names.get(k)),
            )
            break  # one PDF per item is enough
