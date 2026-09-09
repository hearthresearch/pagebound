"""The content-addressed store.

An object is keyed on the source hash and the conversion recipe, so a
Docling upgrade adds an entry instead of redefining one. A consumer that
indexed under the previous recipe stays coherent until it chooses to
reindex.

Tombstones are recorded per recipe. Without them every sync retries the
same broken PDFs at thirty seconds each, forever; with them, a new
Docling version retries automatically because it is a new recipe.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from pagebound.errors import PageboundError
from pagebound.model import Document

_EMBEDDED_IMAGE = re.compile(r"!\[[^\]]*\]\(data:[^)]*\)")
_HASH_CHUNK = 1024 * 1024


def default_root() -> Path:
    """PAGEBOUND_CACHE if set, otherwise the XDG cache directory."""
    override = os.environ.get("PAGEBOUND_CACHE")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")
    return Path(base).expanduser() / "pagebound"


def sha256_of(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(_HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def strip_embedded_images(markdown: str) -> str:
    """Replace base64 payloads with Docling's own placeholder.

    A seatbelt behind the `referenced` option, for a converter that
    ignores the flag or markdown that came from somewhere else entirely.
    """
    return _EMBEDDED_IMAGE.sub("<!-- image -->", markdown)


@dataclass(frozen=True)
class Artifact:
    """A cached conversion as handed to a consumer.

    `path` is the object directory, where docling.json and images/ live
    beside the two files carried here. `converted` says whether the
    call that produced this Artifact ran the converter, so a batch can
    report hits and misses; a Store read is never a conversion.
    """

    document: Document
    markdown: str
    path: Path
    converted: bool = False


@dataclass(frozen=True)
class RecipeUsage:
    """What one recipe occupies on disk, summed over its objects."""

    objects: int = 0
    artifacts: int = 0
    tombstones: int = 0
    bytes: int = 0
    image_bytes: int = 0
    page_render_bytes: int = 0

    def add(self, other: RecipeUsage) -> RecipeUsage:
        return RecipeUsage(*(a + b for a, b in zip(self.as_tuple(), other.as_tuple())))

    def as_tuple(self) -> tuple[int, ...]:
        return (self.objects, self.artifacts, self.tombstones, self.bytes,
                self.image_bytes, self.page_render_bytes)


@dataclass(frozen=True)
class CleanReport:
    """What a clean removed, or would remove."""

    objects: int = 0
    object_bytes: int = 0
    page_renders: int = 0
    page_render_bytes: int = 0

    @property
    def bytes(self) -> int:
        return self.object_bytes + self.page_render_bytes


@dataclass(frozen=True)
class Tombstone:
    """Why an earlier run gave up on a source under one recipe.

    `tombstoned_at` is None for tombstones written before the field
    existed.
    """

    reason: str
    tombstoned_at: datetime | None


class Store:
    def __init__(self, root: str | Path | None = None):
        self.root = Path(root).expanduser() if root else default_root()

    def object_dir(self, sha: str, recipe_key: str) -> Path:
        return self.root / "objects" / sha[:2] / sha / recipe_key

    def has(self, sha: str, recipe_key: str) -> bool:
        return (self.object_dir(sha, recipe_key) / "document.json").exists()

    def read(self, sha: str, recipe_key: str) -> Artifact:
        path = self.object_dir(sha, recipe_key)
        doc_file = path / "document.json"
        if not doc_file.exists():
            raise PageboundError(
                f"no cached artifact for {sha[:12]}... under recipe {recipe_key}"
            )
        document = Document.from_dict(
            json.loads(doc_file.read_text(encoding="utf-8"))
        )
        markdown = (path / "document.md").read_text(encoding="utf-8")
        return Artifact(document=document, markdown=markdown, path=path)

    def write(
        self,
        sha: str,
        recipe_key: str,
        document: Document,
        markdown: str,
        docling: dict[str, Any],
        images: str | Path | None = None,
    ) -> Path:
        """Write the object, whole, and swap it into place.

        Everything goes into a staging directory first, so a reader
        never sees a torn object and two writers racing on the same
        object each leave a coherent one: the files a reader finds
        together always came from one writer. A tombstone at the target
        goes with the directory it was in.
        """
        target = self.object_dir(sha, recipe_key)
        stage = _stage_for(target, "write")
        stage.mkdir(parents=True)
        (stage / "document.md").write_text(strip_embedded_images(markdown),
                                           encoding="utf-8")
        (stage / "docling.json").write_text(json.dumps(docling, ensure_ascii=False),
                                            encoding="utf-8")
        if images is not None and Path(images).is_dir():
            shutil.copytree(images, stage / "images")
        # document.json is the completeness sentinel: a directory without
        # it is never read, so it is the last thing written here.
        (stage / "document.json").write_text(
            json.dumps(document.to_dict(), ensure_ascii=False, indent=1),
            encoding="utf-8")
        _swap_in(stage, target)
        return target

    def rewrite(
        self, sha: str, recipe_key: str, document: Document, markdown: str
    ) -> Path:
        """Replace an object's document and markdown, keeping the rest.

        For re-normalisation, which rebuilds the two files this layer
        owns from the `docling.json` already on disk. The conversion's
        own output and the figures beside it are left alone: Docling did
        not run again, so `converted_at` and the images still describe
        what happened.

        Staged and swapped like a write, so a reader never sees new
        markdown against old offsets. The staging copy is hardlinked, so
        an object with a hundred megabytes of figures costs no bytes to
        restage; the two files that change are unlinked first and
        written fresh, which leaves the originals' inodes alone.
        """
        target = self.object_dir(sha, recipe_key)
        if not (target / "document.json").exists():
            raise PageboundError(
                f"no cached artifact for {sha[:12]}... under recipe {recipe_key}"
            )
        stage = _stage_for(target, "rewrite")
        shutil.copytree(target, stage, copy_function=os.link)
        for name in ("document.md", "document.json"):
            (stage / name).unlink(missing_ok=True)
        (stage / "document.md").write_text(strip_embedded_images(markdown),
                                           encoding="utf-8")
        (stage / "document.json").write_text(
            json.dumps(document.to_dict(), ensure_ascii=False, indent=1),
            encoding="utf-8")
        _swap_in(stage, target)
        return target

    def adopt(self, source_dir: str | Path, sha: str, recipe_key: str) -> Path:
        """Copy a complete object from another store into this one.

        Staged beside the target and renamed into place, so a reader
        never sees a half-copied object; a tombstone at the target is
        superseded, like a write over it would be.
        """
        target = self.object_dir(sha, recipe_key)
        stage = _stage_for(target, "adopt")
        stage.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source_dir, stage)
        _swap_in(stage, target)
        return target

    def tombstone(self, sha: str, recipe_key: str, reason: str) -> None:
        path = self.object_dir(sha, recipe_key)
        path.mkdir(parents=True, exist_ok=True)
        record = {
            "reason": reason,
            "tombstoned_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        _atomic_write(path / "tombstone.json", json.dumps(record))

    def read_tombstone(self, sha: str, recipe_key: str) -> Tombstone | None:
        marker = self.object_dir(sha, recipe_key) / "tombstone.json"
        if not marker.exists():
            return None
        record = json.loads(marker.read_text(encoding="utf-8"))
        stamp = record.get("tombstoned_at")
        return Tombstone(
            reason=record.get("reason", ""),
            tombstoned_at=datetime.fromisoformat(stamp) if stamp else None,
        )

    def artifacts(self, sha: str) -> list[str]:
        """Recipe keys under which this source has a complete artifact.

        Tombstones and torn writes have no document.json and are left out.
        """
        source = self.root / "objects" / sha[:2] / sha
        if not source.is_dir():
            return []
        return sorted(recipe.name for recipe in _recipe_dirs(source)
                      if (recipe / "document.json").exists())

    def latest_artifact(self, sha: str) -> Path | None:
        """The artifact directory to read: the most recent conversion.

        `sync` always writes under the recipe that runs now, so the newest
        conversion is the current recipe's whenever one exists, without
        asking Docling which recipe that is. Recency comes from the
        artifact's own converted_at, which survives copies between
        machines; the file's mtime is the tiebreak and the fallback for
        an artifact written before the field existed.
        """
        recipes = self.artifacts(sha)
        if not recipes:
            return None

        def recency(recipe: str) -> tuple[str, float]:
            sentinel = self.object_dir(sha, recipe) / "document.json"
            try:
                stamp = json.loads(sentinel.read_text(encoding="utf-8"))
                stamp = stamp["conversion"]["converted_at"] or ""
            except (OSError, ValueError, KeyError, TypeError):
                stamp = ""
            return stamp, sentinel.stat().st_mtime

        return self.object_dir(sha, max(recipes, key=recency))

    def tombstones(self, sha: str) -> list[Tombstone]:
        """Every tombstone recorded for this source, any recipe, by key."""
        source = self.root / "objects" / sha[:2] / sha
        if not source.is_dir():
            return []
        return [stone for recipe in _recipe_dirs(source)
                if (stone := self.read_tombstone(sha, recipe.name)) is not None]

    def usage(self) -> dict[str, RecipeUsage]:
        """Disk usage per recipe key, walking every object once."""
        totals: dict[str, RecipeUsage] = {}
        for sha, recipe in self.iter_objects():
            totals[recipe] = totals.get(recipe, RecipeUsage()).add(
                _usage_of(self.object_dir(sha, recipe)))
        return totals

    def clean(self, *, dry_run: bool = False) -> CleanReport:
        """Keep each source's newest conversion; drop the rest and every page render.

        A source whose only directories are tombstones is left alone:
        they are what stops the next sync from retrying it. Everything
        removed comes back by reconverting, nothing else is lost.
        """
        objects = object_bytes = renders = render_bytes = 0
        for sha in sorted({sha for sha, _ in self.iter_objects()}):
            keep = self.latest_artifact(sha)
            if keep is None:
                continue
            source = keep.parent
            for recipe_dir in _recipe_dirs(source):
                if recipe_dir == keep:
                    continue
                objects += 1
                object_bytes += _usage_of(recipe_dir).bytes
                if not dry_run:
                    shutil.rmtree(recipe_dir, ignore_errors=True)
            for render in (keep / "images").glob("page_*.png") if (keep / "images").is_dir() else []:
                renders += 1
                render_bytes += render.stat().st_size
                if not dry_run:
                    render.unlink()
        return CleanReport(objects, object_bytes, renders, render_bytes)

    def find_sha_prefix(self, prefix: str) -> list[str]:
        """Every object sha starting with the prefix, sorted, any recipe."""
        return sorted({sha for sha, _ in self.iter_objects() if sha.startswith(prefix)})

    def iter_objects(self) -> Iterator[tuple[str, str]]:
        objects = self.root / "objects"
        if not objects.is_dir():
            return
        for shard in sorted(objects.iterdir()):
            for source in sorted(shard.iterdir()) if shard.is_dir() else []:
                for recipe in _recipe_dirs(source) if source.is_dir() else []:
                    yield source.name, recipe.name


def _usage_of(recipe_dir: Path) -> RecipeUsage:
    total = images = renders = 0
    for root, _, files in os.walk(recipe_dir):
        in_images = Path(root).name == "images"
        for name in files:
            size = (Path(root) / name).stat().st_size
            total += size
            if in_images:
                images += size
                if name.startswith("page_"):
                    renders += size
    is_artifact = (recipe_dir / "document.json").exists()
    return RecipeUsage(objects=1, artifacts=int(is_artifact),
                       tombstones=int(not is_artifact and (recipe_dir / "tombstone.json").exists()),
                       bytes=total, image_bytes=images, page_render_bytes=renders)


def _recipe_dirs(source: Path) -> list[Path]:
    """The recipe directories under one source, staging directories excluded."""
    return sorted(p for p in source.iterdir()
                  if p.is_dir() and not p.name.startswith("."))


def _stage_for(target: Path, purpose: str) -> Path:
    """A sibling staging path unique to this writer.

    Dot-prefixed so listings skip it, and unique per process and moment
    so two writers never share one.
    """
    token = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
    return target.with_name(f".{target.name}.{purpose}-{token}")


def _swap_in(stage: Path, target: Path) -> None:
    """Rename a staged directory into place, retiring whatever was there.

    A directory rename is atomic, so a reader sees the old object or the
    new one, never a mix. When another writer lands its own object in
    between, the rename fails on the non-empty target and the loop
    retires that one instead; the last writer wins with a whole object.
    """
    for _ in range(8):
        try:
            os.replace(stage, target)
            return
        except OSError:
            if not target.exists():
                raise
        retired = _stage_for(target, "old")
        try:
            os.replace(target, retired)
        except FileNotFoundError:
            continue  # someone else retired it first; try landing again
        shutil.rmtree(retired, ignore_errors=True)
    raise PageboundError(f"could not swap {stage.name} into {target}")


def _atomic_write(target: Path, text: str) -> None:
    """Write through a sibling temp file so a reader never sees a partial one."""
    tmp = target.with_name(f".{target.name}.{os.getpid()}-{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, target)
