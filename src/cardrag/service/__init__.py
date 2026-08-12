"""Read-only service contracts used by HTTP and MCP transports."""

from cardrag.service.models import (
    DocumentDescriptor,
    Evidence,
    EvidencePage,
    ExactSourceSpan,
    ProductVersion,
    ProductVersions,
    ReadinessStatus,
    SearchPage,
    SearchRequest,
    SourceOcrDescriptor,
    SourcePage,
    SourcePageDescriptor,
    SourcePdf,
    SourcePdfDescriptor,
)
from cardrag.service.query import NotFoundError, QueryService, ServiceUnavailableError
from cardrag.service.repository import CardRAGRepository

__all__ = [
    "CardRAGRepository",
    "Evidence",
    "EvidencePage",
    "ExactSourceSpan",
    "DocumentDescriptor",
    "NotFoundError",
    "ProductVersion",
    "ProductVersions",
    "QueryService",
    "ReadinessStatus",
    "SearchPage",
    "SearchRequest",
    "ServiceUnavailableError",
    "SourcePage",
    "SourcePageDescriptor",
    "SourceOcrDescriptor",
    "SourcePdf",
    "SourcePdfDescriptor",
]
