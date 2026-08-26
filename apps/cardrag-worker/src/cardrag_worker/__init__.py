"""CardRAG's finite offline worker."""

from .contracts import (
    DownloadRequest,
    EvidenceRecord,
    IssuerSpec,
    PageRecord,
    SourceRecord,
    SourceSnapshot,
)

__all__ = [
    "DownloadRequest",
    "EvidenceRecord",
    "IssuerSpec",
    "PageRecord",
    "SourceRecord",
    "SourceSnapshot",
]

__version__ = "1.0.4"
