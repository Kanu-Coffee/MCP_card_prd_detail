"""Repository protocol consumed by the online read-only service."""

from __future__ import annotations

from datetime import date
from typing import Protocol

from cardrag.service.models import (
    AuditEvent,
    EvidencePage,
    Issuer,
    ProductVersions,
    ReadinessStatus,
    SearchPage,
    SearchRequest,
    SourcePage,
    SourcePdf,
)


class CardRAGRepository(Protocol):
    """Minimum online data boundary.

    Implementations must pin each read to one active generation and must apply
    issuer/product/version/as-of/section filters before vector/lexical candidate
    selection.  No method in this protocol permits collection, OCR, mutation, or
    generation activation.
    """

    async def search_evidence(self, request: SearchRequest) -> SearchPage: ...

    async def get_evidence(
        self,
        evidence_id: str,
        *,
        cursor: str | None,
        limit: int,
    ) -> EvidencePage | None: ...

    async def get_product_versions(
        self,
        issuer: Issuer,
        product_code: str,
        *,
        as_of: date | None,
    ) -> ProductVersions: ...

    async def get_source_pdf(self, document_id: str) -> SourcePdf | None: ...

    async def get_source_page(self, document_id: str, page: int) -> SourcePage | None: ...

    async def readiness(self) -> ReadinessStatus: ...


class AuditRepository(Protocol):
    async def record_audit(self, event: AuditEvent) -> None: ...


class MCPMetricRepository(Protocol):
    """Optional anonymous operation/outcome rollup sink used by the MCP edge."""

    async def record_mcp_metric(
        self,
        *,
        operation: str,
        outcome: str,
        duration: float,
    ) -> None: ...
