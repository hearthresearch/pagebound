"""pagebound: a shared, content-addressed cache of converted PDFs."""

from pagebound.api import (
    PAGEBOUND_VERSION,
    CacheStatus,
    RenormalizeReport,
    cache_status,
    current_recipe,
    get_cached_document,
    get_document,
    get_document_for_doi,
    get_document_for_item,
    renormalize,
)
from pagebound.errors import (
    ConversionFailedError,
    DoclingUnavailableError,
    NoTextLayerError,
    PageboundError,
    SourceUnreadableError,
    TombstonedError,
    TombstonedNoTextLayerError,
    UnsupportedFormatError,
)
from pagebound.model import FORMAT_VERSION, Block, Document
from pagebound.store import Artifact, Store, Tombstone

__version__ = PAGEBOUND_VERSION
__all__ = [
    "Artifact", "Block", "CacheStatus", "Document", "FORMAT_VERSION",
    "RenormalizeReport", "Store", "Tombstone",
    "cache_status", "current_recipe", "get_cached_document", "get_document",
    "get_document_for_doi", "get_document_for_item", "renormalize",
    "ConversionFailedError", "DoclingUnavailableError", "NoTextLayerError",
    "PageboundError", "SourceUnreadableError", "TombstonedError", "TombstonedNoTextLayerError",
    "UnsupportedFormatError",
    "__version__",
]
