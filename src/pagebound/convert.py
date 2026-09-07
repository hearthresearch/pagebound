"""PDF to DoclingDocument JSON, with Docling invoked as an external tool.

The heavy pipeline (torch plus layout models, one to two gigabytes) lives
in uv's cache and never enters this package's dependency tree. Three
details are paid for elsewhere and inherited here:

- `uv tool run`, never `uvx`: a consumer that bundles uv as a sidecar
  ships only the `uv` binary, so a bare `uvx` is "command not found".
- `--from docling>=2.67.0`: 2.66 crashes preprocessing PDFs with
  degenerate /MediaBox values.
- a generous timeout: the first conversion on a cold cache downloads the
  whole model set before it can start.

Only `--to json` is requested. The markdown is generated from the
normalised blocks instead, so character offsets into it are exact by
construction rather than recovered by matching.
"""

from __future__ import annotations

import functools
import re
import subprocess
from pathlib import Path
from typing import Any, Callable

from pagebound.errors import ConversionFailedError, DoclingUnavailableError

DOCLING_SPEC = "docling>=2.67.0"
CONVERSION_TIMEOUT_SECONDS = 900

_INSTALL_HINT = (
    'install it with: uv tool install --upgrade "docling-slim[standard]" '
    "(the [standard] extra carries the PDF backend; a bare docling-slim "
    "installs an executable that cannot import pypdfium2)"
)
_VERSION_LINE = re.compile(r"^Docling version:\s*(\S+)", re.MULTILINE)


def _is_crash_exit(returncode: int) -> bool:
    """Signal kills: negative, or 128+signal when uv surfaces one."""
    return returncode < 0 or returncode >= 128


def _base_command(converter_version: str | None = None) -> list[str]:
    """The uv invocation, pinned to one Docling when a recipe demands it.

    Left on the open spec otherwise: the default recipe was resolved
    from what uv runs anyway, and a `==` spec would make uv build a
    second tool environment for the same version.
    """
    spec = f"docling=={converter_version}" if converter_version else DOCLING_SPEC
    return ["uv", "tool", "run", "--from", spec, "docling"]


def resolve_version(*, run: Callable[..., Any] | None = None) -> str:
    """The Docling version that will actually run, not the constraint."""
    runner = run or subprocess.run
    try:
        result = runner(
            [*_base_command(), "--version"],
            capture_output=True, text=True, timeout=CONVERSION_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as error:
        raise DoclingUnavailableError(f"uv was not found on PATH; {_INSTALL_HINT}") from error
    except subprocess.TimeoutExpired as error:
        raise DoclingUnavailableError("docling --version timed out") from error

    match = _VERSION_LINE.search(result.stdout or "")
    if not match:
        raise DoclingUnavailableError(
            f"could not read a version from docling --version; {_INSTALL_HINT}"
        )
    return match.group(1)


def convert(
    pdf_path: str | Path,
    output_dir: str | Path,
    options: dict[str, Any],
    *,
    run: Callable[..., Any] | None = None,
    timeout: float | None = None,
    converter_version: str | None = None,
) -> Path:
    """Convert one PDF and return the path to the DoclingDocument JSON.

    `timeout` is seconds for one Docling run (default
    CONVERSION_TIMEOUT_SECONDS); it does not affect the output, so it
    is not part of the recipe. `converter_version` pins the Docling
    that runs, for a caller replaying an explicit recipe.
    """
    pdf_path = Path(pdf_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    runner = run or subprocess.run
    invoke = functools.partial(
        _invoke, runner, pdf_path, output_dir, options,
        timeout=CONVERSION_TIMEOUT_SECONDS if timeout is None else timeout,
        converter_version=converter_version,
    )

    result = invoke(ocr=options.get("ocr", True))
    if result.returncode != 0 and _is_crash_exit(result.returncode):
        # RapidOCR segfaults on specific embedded images while the same
        # text-layer PDF converts fine without it. One retry, then give up:
        # retrying ordinary failures would double conversion time for nothing.
        result = invoke(ocr=False)

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        if "No executables are provided" in stderr:
            raise DoclingUnavailableError(f"docling is not installed; {_INSTALL_HINT}")
        raise ConversionFailedError(
            f"docling failed on {pdf_path.name} (exit {result.returncode}): {stderr[:400]}"
        )

    produced = sorted(output_dir.glob("*.json"))
    if not produced:
        # Docling intermittently exits clean while logging "failed to
        # convert" and writing nothing; the same command succeeds on the
        # next run (~5% of a real library sync). One retry keeps that
        # transient from becoming a permanent tombstone.
        result = invoke(ocr=options.get("ocr", True))
        produced = sorted(output_dir.glob("*.json"))
    if not produced:
        stderr_tail = (result.stderr or "").strip()[-200:]
        raise ConversionFailedError(
            f"docling reported success but wrote no JSON for {pdf_path.name}"
            + (f" (stderr tail: {stderr_tail})" if stderr_tail else "")
        )
    return produced[0]


def _invoke(runner, pdf_path: Path, output_dir: Path, options: dict[str, Any], *,
            ocr: bool, timeout: float, converter_version: str | None):
    command = [
        *_base_command(converter_version),
        str(pdf_path),
        "--to", "json",
        "--image-export-mode", options.get("image_export_mode", "referenced"),
        "--output", str(output_dir),
    ]
    if not ocr:
        command.append("--no-ocr")
    try:
        return runner(
            command, capture_output=True, text=True,
            timeout=timeout,
        )
    except FileNotFoundError as error:
        raise DoclingUnavailableError(f"uv was not found on PATH; {_INSTALL_HINT}") from error
    except subprocess.TimeoutExpired as error:
        raise ConversionFailedError(
            f"docling timed out after {timeout:g}s on {pdf_path.name}; "
            "the first run downloads the model set, so try again once it is cached"
        ) from error
