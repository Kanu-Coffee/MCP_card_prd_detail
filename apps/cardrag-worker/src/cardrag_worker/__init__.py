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

# Application SemVer is intentionally independent from the immutable native OCR
# processor contract. Patch releases may retain that older contract so already
# verified remote OCR artifacts remain reusable without resuming a failed run.
__version__ = "1.0.15"
