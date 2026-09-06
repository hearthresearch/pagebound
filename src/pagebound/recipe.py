"""The conversion recipe: what produced an artifact, and its cache key.

The recipe keys the object directory because it answers the expensive
question, "must Docling run again?". `format_version` deliberately does
not take part: a change to our own block schema is a second of local
re-normalisation from the cached Docling output, and must never discard a
thirty-second conversion.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

# The options that reach the Docling command line. `referenced` keeps
# figures out of the markdown as files rather than base64 payloads;
# measured on a real paper, embedding them produced 1.8M characters of
# which 93% were base64.
DEFAULT_OPTIONS: dict[str, Any] = {
    "ocr": True,
    "image_export_mode": "referenced",
}


@dataclass(frozen=True)
class Recipe:
    converter: str
    converter_version: str
    options: dict[str, Any]

    @property
    def key(self) -> str:
        """A stable short hash of everything that affects Docling's output."""
        payload = json.dumps(
            {
                "converter": self.converter,
                "converter_version": self.converter_version,
                "options": self.options,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:8]
