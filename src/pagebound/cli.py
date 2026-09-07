"""The pagebound command line.

The logic lives in `pagebound.sync` and `pagebound.api`; the typer
commands are the thin wiring that turns arguments into those calls.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import typer
from rich import box
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from pagebound.api import PAGEBOUND_VERSION, current_recipe, get_document
from pagebound.errors import PageboundError
from pagebound.index import Index
from pagebound.store import Store
from pagebound.sync import (
    ZoteroItem,
    iter_items,
    library_key,
    sync_items,
    zotero_client,
)

APP_HELP = """Shared cache of PDFs converted to text with page and bbox provenance.

To read a paper: `pagebound list --json` finds it by title, author, year
or DOI and gives its sha256; `pagebound path <sha256 prefix>` prints the
directory holding its converted text; read document.md there.

Every command exits 0 on success, 1 on a cache or conversion problem
(reason on stderr), 2 on a usage error. The cache root is $PAGEBOUND_CACHE
when set, else ~/.cache/pagebound; --cache overrides both.
"""

app = typer.Typer(
    help=APP_HELP,
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)


def _print_version(value: bool) -> None:
    if value:
        typer.echo(f"pagebound {PAGEBOUND_VERSION}")
        raise typer.Exit()


@app.callback()
def _root(
    version: bool = typer.Option(
        False, "--version", "-V", callback=_print_version, is_eager=True,
        help="Print the version and exit.",
    ),
) -> None:
    pass

# Human output goes through Rich; machine output (--json, paths) through
# typer.echo, so a pipe never receives a table.
console = Console(highlight=False)
errors = Console(stderr=True, highlight=False)

_STATE_STYLE = {"cached": "green", "tombstoned": "yellow", "absent": "red"}


def _table(*columns: str, numeric: tuple[str, ...] = ()) -> Table:
    table = Table(box=box.SIMPLE_HEAD, pad_edge=False, show_edge=False,
                  header_style="bold")
    for name in columns:
        # One row per item: a long title is cut with an ellipsis rather
        # than wrapped, and it takes whatever width the others leave.
        table.add_column(name, justify="right" if name in numeric else "left",
                         no_wrap=True, overflow="ellipsis",
                         ratio=1 if name == "Title" else None)
    table.expand = "Title" in columns
    return table


def _styled_state(state: str) -> str:
    return f"[{_STATE_STYLE.get(state, 'white')}]{state}[/]"

CacheOption = typer.Option(
    None, "--cache", metavar="DIR",
    help="Cache root. Default: $PAGEBOUND_CACHE, else ~/.cache/pagebound.",
)
RefArgument = typer.Argument(None, metavar="[REF | LIBRARY ITEM]")
SyncRefsArgument = typer.Argument(
    None, metavar="[REF]...",
    help="Sync only these papers: a Zotero item key, a DOI, or a sha256 "
         "prefix, the same shapes `pagebound path` takes.",
)
PdfArgument = typer.Argument(..., metavar="PDF", help="Path to a PDF file.")
OcrScansOption = typer.Option(
    False, "--ocr-scans",
    help="OCR a PDF that has no text layer instead of refusing it; the "
         "artifact records that its text came out of OCR. Slow.",
)

# The shapes a bare REF can take. A DOI carries a slash or a known
# prefix; a sha prefix is lowercase hex, six characters or more; a
# Zotero item key is eight uppercase alphanumerics. Lowercase hex and
# uppercase keys cannot collide, so one argument serves all three.
_DOI_SHAPE = re.compile(r"^(10\.|https?://|doi:)|/")
_SHA_PREFIX = re.compile(r"^[0-9a-f]{6,64}$")
_ITEM_KEY = re.compile(r"^[A-Z0-9]{8}$")


@app.command()
def sync(
    refs: list[str] = SyncRefsArgument,
    collection: str = typer.Option(
        None, "--collection", metavar="NAME",
        help="Only this Zotero collection: an unambiguous name, or a "
             "'/'-separated path such as 'PhD/Methods' when names repeat.",
    ),
    library: str = typer.Option(
        "user", "--library", metavar="ID",
        help="'user' for the personal library, or a numeric group id.",
    ),
    storage: Path = typer.Option(
        Path.home() / "Zotero" / "storage", "--storage", metavar="DIR",
        help="Where Zotero keeps attachment files.",
    ),
    force: bool = typer.Option(
        False, "--force",
        help="Reconvert every item, even unchanged or tombstoned ones.",
    ),
    ocr_scans: bool = OcrScansOption,
    cache: Path = CacheOption,
) -> None:
    """Walk a Zotero library, convert every PDF in it, and fill the index.

    Talks to the Zotero desktop app over its local API, so Zotero must
    be running, and needs the pyzotero extra: uv add "pagebound\\[zotero]".

    For each item with a PDF attachment: skips it when the
    PDF is unchanged and already cached under the current recipe,
    otherwise hashes and converts it; either way records the item's
    metadata (title, authors, year, DOI, collections) in the index.

    \b
    With one or more REFs, only those papers are fetched, by key rather
    than by walking anything, which is the way to pick up a PDF that
    was replaced in Zotero:
      pagebound sync 10.17705/1cais.03708
      pagebound sync DMV43UZK --force
    A DOI or sha prefix is looked up in the index, so it names a paper
    already synced once; an item key is taken at face value and reaches
    an item pagebound has never seen. REFs and --collection are
    alternatives (exit 2), and a REF nothing answers is exit 1.

    A PDF that fails to convert, or has no text layer, is reported on
    stderr and tombstoned so later runs do not retry it under the same
    recipe; on those later runs it counts as "tombstoned", not "failed".
    With --ocr-scans a PDF without a text layer is OCR'd instead of
    refused, and an earlier no-text-layer tombstone is retried; the
    artifact records that its text came out of OCR. The walk continues
    past failures.

    Prints one line, "N converted, N skipped, N failed, N tombstoned",
    and exits 0 when the walk completed even if some items failed.
    Aborts before the walk when Zotero or Docling cannot be reached.
    """
    refs = refs or []
    if refs and collection:
        typer.echo("give REF or --collection, not both", err=True)
        raise typer.Exit(code=2)
    for ref in refs:
        if not (_DOI_SHAPE.search(ref) or _SHA_PREFIX.match(ref)
                or _ITEM_KEY.match(ref)):
            typer.echo(f"{ref} is not a sha prefix, an item key, or a DOI", err=True)
            raise typer.Exit(code=2)

    store, index = Store(cache), Index(cache)

    # Resolving first means a REF nothing answers costs neither a
    # running Zotero nor the five seconds `current_recipe` pays.
    pairs = None
    if refs:
        wanted_library = library_key("user" if library == "user" else "group", library)
        try:
            pairs = item_refs(refs, index, wanted_library)
        except PageboundError as error:
            typer.echo(str(error), err=True)
            raise typer.Exit(code=1) from error
        resolved = {ref for ref, _ in pairs}
        if not resolved.issuperset(refs):
            for ref in refs:
                if ref not in resolved:
                    typer.echo(f"nothing indexed for {ref}", err=True)
            raise typer.Exit(code=1)

    try:
        client, key = zotero_client(library=library)
    except PageboundError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(code=1) from error

    recipe = current_recipe()

    seen = {"converted": 0, "skipped": 0, "failed": 0, "tombstoned": 0}
    progress = Progress(
        SpinnerColumn(finished_text="[green]✔[/]"),
        TextColumn("{task.description}"),
        TextColumn("[dim]{task.fields[counts]}[/]"),
        console=console, transient=True, disable=not console.is_terminal,
    )

    def tally() -> str:
        return "  ".join(f"{n} {label}" for label, n in seen.items() if n)

    def report(event: str, item: ZoteroItem, detail: str) -> None:
        seen[event] += 1
        progress.update(task, description=f"{event} {item.title[:50]}", counts=tally())
        if event == "failed":
            errors.print(f"[red]failed[/] {item.title[:60]}: {detail}")

    keys = [item_key for _, item_key in pairs] if pairs is not None else None
    items = iter_items(client, storage_root=storage, library=key,
                       collection=collection, keys=keys)
    missing: list[str] = []
    if pairs is not None:
        # A REF the index answers can still name an item Zotero has
        # since lost, or one whose PDF attachment is gone.
        items = list(items)
        found = {item.item_key for item in items}
        missing = [ref for ref, item_key in pairs if item_key not in found]

    with progress:
        task = progress.add_task("syncing", counts="")
        counts = sync_items(
            items, store=store, index=index, recipe=recipe, force=force,
            ocr_scans=ocr_scans, on_event=report,
        )
    for ref in missing:
        errors.print(f"[red]no Zotero item with a PDF for {ref}[/]")
    console.print(
        f"[bold green]{counts['converted']}[/] converted  "
        f"[bold]{counts['skipped']}[/] skipped  "
        f"[bold {'red' if counts['failed'] else 'white'}]{counts['failed']}[/] failed  "
        f"[bold {'yellow' if counts['tombstoned'] else 'white'}]{counts['tombstoned']}[/] tombstoned"
    )
    if missing:
        raise typer.Exit(code=1)


@app.command()
def convert(
    pdf: Path = PdfArgument,
    ocr_scans: bool = OcrScansOption,
    cache: Path = CacheOption,
) -> None:
    """Convert one PDF outside Zotero and print its object directory.

    Hashes the file and converts it under the current recipe unless an
    artifact already exists. Writes no index row, so the result is
    reachable afterwards only by sha256 prefix: `pagebound path <prefix>`,
    where the prefix is the start of the printed directory's parent name.

    Exit 1, with the reason on stderr, when the PDF has no text layer
    (a scan, unless --ocr-scans) or Docling fails; the failure is
    tombstoned so the next call fails fast instead of reconverting.
    """
    store = Store(cache)
    try:
        artifact = get_document(pdf, store=store, ocr_scans=ocr_scans)
    except PageboundError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(code=1) from error
    typer.echo(artifact.path)


@app.command(name="list")
def list_items(
    as_json: bool = typer.Option(
        False, "--json",
        help="A JSON array instead of lines; see the fields above.",
    ),
    collection: str = typer.Option(
        None, "--collection", metavar="NAME",
        help="Only items in this Zotero collection, by exact name as recorded "
             "at sync time.",
    ),
    cache: Path = CacheOption,
) -> None:
    """List every indexed item with its metadata and cache state.

    Reads the index only, so it is fast and needs neither Zotero nor
    Docling. Items appear here after a `pagebound sync`; PDFs converted
    with `pagebound convert` have no index row and are not listed.

    \b
    Default output is a table for people: item key, year, title and
    state (cached, tombstoned or absent). Scripts and agents use --json.

    \b
    With --json, an array of objects with these fields:
      library_key  "user" or "group:<id>"
      item_key     Zotero item key, 8 characters
      sha256       hash of the PDF; the key for `pagebound path`
      path         the PDF on disk
      size, mtime  as of the last sync
      title        string
      authors      array of full names, Zotero order
      year         integer or null
      doi          lowercase, prefix-stripped, or null
      cached       true when an artifact exists under any recipe
      state        "cached", "tombstoned" (every conversion attempted
                   under some recipe gave up; no artifact), or "absent"

    "cached" does not say which recipe; `pagebound path` reports the
    newest conversion.
    """
    store, index = Store(cache), Index(cache)
    objects: dict[str, str] = {}
    for sha, recipe in store.iter_objects():
        if store.has(sha, recipe):
            objects[sha] = "cached"
        elif store.read_tombstone(sha, recipe) is not None:
            objects.setdefault(sha, "tombstoned")
    rows = [dict(row, cached=objects.get(row["sha256"]) == "cached",
                 state=objects.get(row["sha256"], "absent"))
            for row in index.iter_items(collection=collection)]
    if as_json:
        typer.echo(json.dumps(rows, ensure_ascii=False, indent=1))
        return
    table = _table("Item", "Year", "Title", "State", numeric=("Year",))
    for row in rows:
        table.add_row(f"{row['library_key']}/{row['item_key']}",
                      str(row["year"] or ""), row["title"], _styled_state(row["state"]))
    console.print(table)
    console.print(f"[dim]{len(rows)} items[/]")


def resolve_ref(ref: str, store: Store, index: Index) -> list[str]:
    """The sha256s a bare REF names: by DOI, sha prefix, or item key.

    A sha prefix is looked up in the store as well as the index, so an
    object written by `pagebound convert` without a Zotero item still
    resolves. A prefix matching more than one hash is an error rather
    than a guess.
    """
    if _DOI_SHAPE.search(ref):
        return [row["sha256"] for row in index.lookup_doi(ref)]
    shas: list[str] = []
    if _SHA_PREFIX.match(ref):
        matches = set(store.find_sha_prefix(ref))
        matches.update(row["sha256"] for row in index.lookup_sha_prefix(ref))
        if len(matches) > 1:
            raise PageboundError(f"ambiguous prefix {ref}: {len(matches)} objects match")
        shas.extend(matches)
    if _ITEM_KEY.match(ref):
        row = index.lookup("user", ref)
        if row:
            shas.append(row["sha256"])
    return shas


def item_refs(refs: list[str], index: Index,
              library_key: str) -> list[tuple[str, str]]:
    """The (REF, Zotero item key) pairs a list of REFs names, in order.

    An item key stands for itself, so an item Zotero has but the index
    has never seen still syncs; a DOI or a sha prefix is looked up in
    the index, which is where the mapping from a paper to its Zotero
    item lives, and only rows of this library answer. A REF nothing
    answers yields no pair, which is the caller's to report.
    """
    pairs: list[tuple[str, str]] = []
    for ref in refs:
        if _DOI_SHAPE.search(ref):
            rows = index.lookup_doi(ref)
        elif _ITEM_KEY.match(ref):
            pairs.append((ref, ref))
            continue
        else:
            rows = index.lookup_sha_prefix(ref)
            if len({row["sha256"] for row in rows}) > 1:
                raise PageboundError(
                    f"ambiguous prefix {ref}: {len(rows)} objects match")
        pairs.extend((ref, row["item_key"]) for row in rows
                     if row["library_key"] == library_key)
    return pairs


def artifact_dir(store: Store, sha: str) -> Path:
    """The directory holding this source's converted text, or why there is none."""
    directory = store.latest_artifact(sha)
    if directory is None:
        stones = store.tombstones(sha)
        detail = f": {stones[-1].reason}" if stones else "; run `pagebound sync`"
        raise PageboundError(f"no converted text for {sha[:12]}{detail}")
    return directory


@app.command()
def path(
    refs: list[str] = RefArgument,
    doi: str = typer.Option(
        None, "--doi", metavar="DOI",
        help="Same as giving the DOI as REF; kept for existing callers.",
    ),
    cache: Path = CacheOption,
) -> None:
    """Print the directory holding a paper's converted text.

    The directory contains document.md (the text to read), document.json
    (blocks with page and bbox), docling.json, and images/. When the PDF
    was converted under more than one Docling recipe, the newest
    conversion is the one printed. Reads the cache only; needs neither
    Zotero nor Docling.

    \b
    REF is matched by shape:
      sha256 prefix    lowercase hex, 6 or more chars     6cbb126970f0
      Zotero item key  8 uppercase letters or digits      BAK9F4R4
      DOI              contains "/" or starts with "10."  10.1108/eb024320

    \b
    Two positionals are LIBRARY ITEM, for group libraries:
      pagebound path group:12345 ITEM0002

    Prints one absolute path per match; a DOI shared by two libraries
    prints two lines. Exit 0 on a match, 1 when nothing matches, the
    paper is indexed but has no converted text (a tombstone's reason is
    shown), or a prefix fits more than one object, 2 on a REF of no
    known shape.
    Use `pagebound list --json` to find a paper's sha256, item key or
    DOI from its title, authors or year.
    """
    refs = refs or []
    by_item = len(refs) == 2 and not doi
    by_ref = len(refs) == 1 and not doi
    usage_error = not (by_item or by_ref or (doi and not refs))
    if usage_error:
        typer.echo("give REF, LIBRARY ITEM, or --doi", err=True)
        raise typer.Exit(code=2)

    store, index = Store(cache), Index(cache)
    try:
        if by_item:
            library, item = refs
            row = index.lookup(library, item)
            shas = [row["sha256"]] if row else []
            wanted = f"{library}/{item}"
        elif by_ref:
            ref = refs[0]
            if not (_DOI_SHAPE.search(ref) or _SHA_PREFIX.match(ref)
                    or _ITEM_KEY.match(ref)):
                typer.echo(f"{ref} is not a sha prefix, an item key, or a DOI", err=True)
                raise typer.Exit(code=2)
            shas = resolve_ref(ref, store, index)
            wanted = ref
        else:
            shas = [row["sha256"] for row in index.lookup_doi(doi)]
            wanted = doi
        if not shas:
            raise PageboundError(f"no cache entry for {wanted}")
        for sha in shas:
            typer.echo(artifact_dir(store, sha))
    except PageboundError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(code=1) from error


@app.command()
def status(cache: Path = CacheOption) -> None:
    """Report what the cache holds.

    \b
    Three lines:
      N objects, N indexed items
      recipes: KEY, KEY, ...
      root: /path/to/cache

    An object is one (PDF, recipe) artifact or tombstone, so the count
    can exceed the item count when more than one recipe is present. A
    recipe key names a Docling version plus options. `pagebound stats`
    adds sizes; `pagebound clean` drops superseded recipes.
    """
    store, index = Store(cache), Index(cache)
    objects = list(store.iter_objects())
    recipes = sorted({recipe for _, recipe in objects})
    console.print(f"[bold]{len(objects)}[/] objects, [bold]{index.count()}[/] indexed items")
    console.print(f"recipes: {', '.join(recipes) if recipes else 'none'}")
    console.print(f"[dim]root: {store.root}[/]")


def _human(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1000 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1000
    return f"{size:.1f} TB"


@app.command()
def stats(
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
    cache: Path = CacheOption,
) -> None:
    """Report what the cache costs on disk, per recipe, and what `clean` would free.

    \b
    Per recipe: objects, artifacts, tombstones, total size, the share
    held by images/, and the share of that held by page renders (full-
    page PNGs an older pagebound stored; nothing reads them). Then the
    index count and what `pagebound clean` would free.

    \b
    With --json: {"root", "indexed_items", "recipes": {KEY: {objects,
    artifacts, tombstones, bytes, image_bytes, page_render_bytes}},
    "removable_bytes"}.

    Walks every file, so it takes a second or two on a large cache.
    """
    store, index = Store(cache), Index(cache)
    usage = store.usage()
    would_free = store.clean(dry_run=True)
    if as_json:
        typer.echo(json.dumps({
            "root": str(store.root),
            "indexed_items": index.count(),
            "recipes": {key: vars(value) for key, value in sorted(usage.items())},
            "removable_bytes": would_free.bytes,
        }, indent=1))
        return
    table = _table("Recipe", "Objects", "Artifacts", "Tombstones", "Size", "Images",
                   "Page renders",
                   numeric=("Objects", "Artifacts", "Tombstones", "Size", "Images",
                            "Page renders"))
    for key, value in sorted(usage.items()):
        renders = _human(value.page_render_bytes)
        table.add_row(key, str(value.objects), str(value.artifacts),
                      str(value.tombstones) if not value.tombstones
                      else f"[yellow]{value.tombstones}[/]",
                      _human(value.bytes), _human(value.image_bytes),
                      renders if not value.page_render_bytes else f"[yellow]{renders}[/]")
    console.print(table)
    console.print(f"[bold]{index.count()}[/] indexed items  [dim]root {store.root}[/]")
    if would_free.bytes:
        console.print(f"[yellow]`pagebound clean` would free {_human(would_free.bytes)}[/]: "
                      f"{would_free.objects} superseded objects and "
                      f"{would_free.page_renders} page renders")
    else:
        console.print("[green]nothing for `pagebound clean` to free[/]")


@app.command()
def clean(
    dry_run: bool = typer.Option(False, "--dry-run", help="Report only; delete nothing."),
    cache: Path = CacheOption,
) -> None:
    """Free the space a Docling upgrade leaves behind.

    Every source keeps its newest conversion, the one `pagebound path`
    prints; every other recipe directory of that source goes, and so do
    page renders inside the kept one. A source that has only tombstones
    is left alone, since they are what stops the next sync from retrying
    it. Nothing is lost that a reconversion would not bring back, and
    the index is not touched.

    Prints "freed N: M superseded objects and K page renders"; with
    --dry-run, "would free".
    """
    report = Store(cache).clean(dry_run=dry_run)
    verb = "would free" if dry_run else "freed"
    console.print(f"[bold]{verb} {_human(report.bytes)}[/]: {report.objects} superseded "
                  f"objects and {report.page_renders} page renders")


@app.command(name="help")
def help_command(
    ctx: typer.Context,
    command: str = typer.Argument(None, metavar="[COMMAND]",
                                  help="A command name; omit for the overview."),
) -> None:
    """Show help for a command, the same as `pagebound COMMAND --help`."""
    root = ctx.parent or ctx
    group = root.command
    if command is None:
        typer.echo(group.get_help(root))
        return
    target = group.get_command(root, command)  # type: ignore[attr-defined]
    if target is None:
        typer.echo(f"no such command: {command}", err=True)
        raise typer.Exit(code=2)
    with typer.Context(target, info_name=command, parent=root) as sub:
        typer.echo(target.get_help(sub))


def main() -> None:
    app()
