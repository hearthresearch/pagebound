"""Exception hierarchy shared across pagebound.

Split by what a caller can do about it: an unavailable converter is an
environment problem and should stop the run, while a failed conversion is
a per-document problem the caller degrades around.
"""

from __future__ import annotations

from datetime import datetime


class PageboundError(Exception):
    """Base for every error this package raises."""


class DoclingUnavailableError(PageboundError):
    """Docling could not be run at all (uv missing, or tool not installed).

    Environmental: the run should stop rather than tombstone every
    document in turn.
    """


class ConversionFailedError(PageboundError):
    """Docling ran and could not convert this file."""


class NoTextLayerError(PageboundError):
    """The PDF carries no extractable text, so conversion would yield nothing."""


class TombstonedError(ConversionFailedError):
    """An earlier run already failed on this file under this recipe.

    Replayed from the tombstone instead of retrying, so `reason` and
    `tombstoned_at` describe that earlier failure. A caller can tell
    "failed now" from "gave up earlier" by type, and `force=True`
    retries.
    """

    def __init__(self, message: str, *, reason: str,
                 tombstoned_at: datetime | None) -> None:
        super().__init__(message)
        self.reason = reason
        self.tombstoned_at = tombstoned_at


class TombstonedNoTextLayerError(NoTextLayerError, TombstonedError):
    """A scan whose no-text-layer verdict was recorded by an earlier run.

    Both a NoTextLayerError, so a caller that degrades around scans keeps
    working on the replay, and a TombstonedError, so it can see the
    verdict is old.
    """


class SourceUnreadableError(PageboundError):
    """The PDF itself could not be read: missing, unreadable, or gone."""


class UnsupportedFormatError(PageboundError):
    """A cached artifact was written by a newer, unknown format version."""
