import contextlib
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

from pagebound.convert import (
    CONVERSION_TIMEOUT_SECONDS, DOCLING_SPEC, convert, resolve_version,
)
from pagebound.errors import ConversionFailedError, DoclingUnavailableError
from pagebound.recipe import DEFAULT_OPTIONS


class Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def runner(*results, record=None):
    """A fake subprocess.run yielding the given results in order."""
    queue = list(results)

    def run(command, **kwargs):
        if record is not None:
            record.append(command)
        return queue.pop(0) if queue else Result()

    return run


def test_the_command_never_uses_uvx(tmp_path):
    # A consumer that bundles uv as a sidecar ships only the `uv` binary,
    # so a bare `uvx` is "command not found" on a clean machine.
    seen: list[list[str]] = []
    (tmp_path / "paper.json").write_text("{}")
    convert(tmp_path / "paper.pdf", tmp_path, DEFAULT_OPTIONS,
            run=runner(Result(), record=seen))
    assert seen[0][0] == "uv"
    assert seen[0][:4] == ["uv", "tool", "run", "--from"]
    assert "uvx" not in seen[0]


def test_the_command_pins_the_docling_spec(tmp_path):
    seen: list[list[str]] = []
    (tmp_path / "paper.json").write_text("{}")
    convert(tmp_path / "paper.pdf", tmp_path, DEFAULT_OPTIONS,
            run=runner(Result(), record=seen))
    assert DOCLING_SPEC in seen[0]
    assert "--to" in seen[0] and "json" in seen[0]


def test_a_crash_exit_retries_once_without_ocr(tmp_path):
    # RapidOCR segfaults on specific embedded images while the same
    # text-layer PDF converts fine without OCR.
    seen: list[list[str]] = []
    (tmp_path / "paper.json").write_text("{}")
    convert(tmp_path / "paper.pdf", tmp_path, DEFAULT_OPTIONS,
            run=runner(Result(returncode=139), Result(), record=seen))
    assert len(seen) == 2
    assert "--no-ocr" in seen[1]


def test_an_ordinary_failure_is_not_retried(tmp_path):
    seen: list[list[str]] = []
    with pytest.raises(ConversionFailedError):
        convert(tmp_path / "paper.pdf", tmp_path, DEFAULT_OPTIONS,
                run=runner(Result(returncode=1, stderr="bad pdf"), record=seen))
    assert len(seen) == 1


def test_no_executables_in_stderr_is_environmental(tmp_path):
    with pytest.raises(DoclingUnavailableError) as caught:
        convert(tmp_path / "paper.pdf", tmp_path, DEFAULT_OPTIONS,
                run=runner(Result(returncode=1, stderr="No executables are provided")))
    assert "docling-slim[standard]" in str(caught.value)


def test_a_missing_uv_is_environmental(tmp_path):
    def run(command, **kwargs):
        raise FileNotFoundError("uv")

    with pytest.raises(DoclingUnavailableError) as caught:
        convert(tmp_path / "paper.pdf", tmp_path, DEFAULT_OPTIONS, run=run)
    assert "docling-slim[standard]" in str(caught.value)


def test_a_timeout_fails_the_document_not_the_run(tmp_path):
    def run(command, **kwargs):
        raise subprocess.TimeoutExpired(command, CONVERSION_TIMEOUT_SECONDS)

    with pytest.raises(ConversionFailedError):
        convert(tmp_path / "paper.pdf", tmp_path, DEFAULT_OPTIONS, run=run)


def test_success_without_json_output_is_a_failure(tmp_path):
    with pytest.raises(ConversionFailedError):
        convert(tmp_path / "paper.pdf", tmp_path, DEFAULT_OPTIONS,
                run=runner(Result()))


def test_convert_returns_the_json_path(tmp_path):
    produced = tmp_path / "paper.json"
    produced.write_text("{}")
    assert convert(tmp_path / "paper.pdf", tmp_path, DEFAULT_OPTIONS,
                   run=runner(Result())) == produced


def test_resolve_version_reads_the_reported_version():
    stdout = "Docling version: 2.120.3\nDocling Core version: 2.92.0\n"
    assert resolve_version(run=runner(Result(stdout=stdout))) == "2.120.3"


def test_resolve_version_is_environmental_when_uv_is_missing():
    def run(command, **kwargs):
        raise FileNotFoundError("uv")

    with pytest.raises(DoclingUnavailableError):
        resolve_version(run=run)


def test_an_empty_success_is_retried_once(tmp_path):
    # Docling intermittently exits clean without writing the JSON; the
    # same command succeeds on the next run, so one retry rescues a
    # transient instead of tombstoning the paper forever.
    seen: list[list[str]] = []

    def run(command, **kwargs):
        seen.append(command)
        if len(seen) == 2:
            (tmp_path / "paper.json").write_text("{}")
        return Result()

    produced = convert(tmp_path / "paper.pdf", tmp_path, DEFAULT_OPTIONS, run=run)
    assert produced == tmp_path / "paper.json"
    assert len(seen) == 2


def test_a_persistently_empty_success_still_fails(tmp_path):
    seen: list[list[str]] = []
    with pytest.raises(ConversionFailedError):
        convert(tmp_path / "paper.pdf", tmp_path, DEFAULT_OPTIONS,
                run=runner(Result(), Result(), record=seen))
    assert len(seen) == 2


def test_the_timeout_is_passed_to_the_runner(tmp_path):
    seen: list = []

    def run(command, **kwargs):
        seen.append(kwargs["timeout"])
        (tmp_path / "paper.json").write_text("{}")
        return Result()

    convert(tmp_path / "paper.pdf", tmp_path, DEFAULT_OPTIONS, run=run, timeout=42)
    assert seen == [42]


def test_the_default_timeout_is_the_module_constant(tmp_path):
    seen: list = []

    def run(command, **kwargs):
        seen.append(kwargs["timeout"])
        (tmp_path / "paper.json").write_text("{}")
        return Result()

    convert(tmp_path / "paper.pdf", tmp_path, DEFAULT_OPTIONS, run=run)
    assert seen == [CONVERSION_TIMEOUT_SECONDS]


def test_an_explicit_converter_version_pins_the_command(tmp_path):
    seen: list = []
    (tmp_path / "paper.json").write_text("{}")
    convert(tmp_path / "paper.pdf", tmp_path, DEFAULT_OPTIONS,
            run=runner(Result(), record=seen), converter_version="2.100.0")
    assert "docling==2.100.0" in seen[0]
    assert DOCLING_SPEC not in seen[0]


def a_uv_whose_child_outlives_it(tmp_path, monkeypatch) -> Path:
    """A `uv` on PATH that starts a long child of its own, as it does Docling."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text('#!/bin/sh\nsleep 60 &\necho $! > "$CHILD_PID_FILE"\nwait\n')
    uv.chmod(0o755)
    pid_file = tmp_path / "child.pid"
    monkeypatch.setenv("CHILD_PID_FILE", str(pid_file))
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    return pid_file


def gone_within(pid: int, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def test_a_timeout_kills_what_uv_started(tmp_path, monkeypatch):
    # Killing uv alone left Docling running, orphaned, until it finished:
    # a 461-page book was still at 840 MB beside the next conversion.
    pid_file = a_uv_whose_child_outlives_it(tmp_path, monkeypatch)
    with pytest.raises(ConversionFailedError, match="timed out"):
        convert(tmp_path / "paper.pdf", tmp_path / "out", DEFAULT_OPTIONS, timeout=1)
    child = int(pid_file.read_text())
    try:
        assert gone_within(child, 3)
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(child, signal.SIGKILL)


def test_an_interrupt_kills_what_uv_started(tmp_path, monkeypatch):
    # Docling runs in a session of its own so a timeout can kill all of
    # it, which also keeps a terminal's Ctrl+C from reaching it.
    pid_file = a_uv_whose_child_outlives_it(tmp_path, monkeypatch)

    def interrupted(self, *args, **kwargs):
        while not pid_file.exists() or not pid_file.read_text().strip():
            time.sleep(0.05)
        raise KeyboardInterrupt

    monkeypatch.setattr(subprocess.Popen, "communicate", interrupted)
    with pytest.raises(KeyboardInterrupt):
        convert(tmp_path / "paper.pdf", tmp_path / "out", DEFAULT_OPTIONS, timeout=60)
    child = int(pid_file.read_text())
    try:
        assert gone_within(child, 3)
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(child, signal.SIGKILL)
