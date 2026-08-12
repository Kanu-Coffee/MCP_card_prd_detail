"""Transport-neutral contracts for the read-only CardRAG service.

The repository implementation is deliberately kept behind these models.  This
lets the MCP/HTTP edge enforce one stable public contract without coupling it
to PostgreSQL row shapes or generation-directory layouts.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

Issuer = Literal["woori", "kb", "shinhan"]
Identifier = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=512),
]
Cursor = Annotated[str, StringConstraints(max_length=2_048)]
PageLimit = Annotated[int, Field(ge=1, le=50)]
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
SearchWarning = Literal["no_evidence", "low_confidence", "conflicting_versions", "vector_degraded"]


class StrictModel(BaseModel):
    """Base model that rejects accidental contract expansion."""

    model_config = ConfigDict(extra="forbid")


class SourceSpan(StrictModel):
    page_start: int = Field(ge=1)
    page_end: int = Field(ge=1)
    char_start: int | None = Field(default=None, ge=0)
    char_end: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def ordered(self) -> SourceSpan:
        if self.page_end < self.page_start:
            raise ValueError("page_end must not precede page_start")
        if self.char_start is not None and self.char_end is not None and self.char_end < self.char_start:
            raise ValueError("char_end must not precede char_start")
        return self


class ExactSourceSpan(StrictModel):
    """Exact page-local quote coordinates; ``SourceSpan`` is only an envelope."""

    page: int = Field(ge=1)
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    quote_sha256: Sha256Hex

    @model_validator(mode="after")
    def ordered(self) -> ExactSourceSpan:
        if self.end <= self.start:
            raise ValueError("exact source span end must be greater than start")
        return self


class Evidence(StrictModel):
    evidence_id: Identifier
    issuer: Issuer
    product_code: Identifier
    document_id: Identifier
    document_version: Identifier
    effective_date: date | None = None
    generation_id: Identifier
    section_type: str = Field(min_length=1, max_length=120)
    title: str | None = Field(default=None, max_length=500)
    text: str = Field(min_length=1)
    source_span: SourceSpan
    source_spans: tuple[ExactSourceSpan, ...] = Field(min_length=1)
    text_sha256: Sha256Hex
    pdf_sha256: Sha256Hex
    confidence: float = Field(ge=0, le=1)
    score: float | None = None

    @model_validator(mode="after")
    def exact_text_hash(self) -> Evidence:
        if hashlib.sha256(self.text.encode()).hexdigest() != self.text_sha256:
            raise ValueError("evidence text hash does not match returned text")
        coordinates = tuple(
            (item.page, item.start, item.end, item.quote_sha256) for item in self.source_spans
        )
        ordered = tuple(sorted(coordinates))
        if coordinates != ordered or len(set(coordinates)) != len(coordinates):
            raise ValueError("exact source spans must be unique and ordered")
        if (
            self.source_span.page_start != min(item.page for item in self.source_spans)
            or self.source_span.page_end != max(item.page for item in self.source_spans)
            or self.source_span.char_start != min(item.start for item in self.source_spans)
            or self.source_span.char_end != max(item.end for item in self.source_spans)
        ):
            raise ValueError("source span envelope does not match exact source spans")
        return self


class SearchRequest(StrictModel):
    query: str = Field(min_length=1, max_length=2_000)
    issuer: Issuer | None = None
    product_code: Identifier | None = None
    section_type: str | None = Field(default=None, min_length=1, max_length=120)
    version: Identifier | None = None
    as_of: date | None = None
    limit: int = Field(default=10, ge=1, le=50)
    cursor: str | None = Field(default=None, max_length=2_048)
    allow_degraded: bool = False

    @field_validator("query")
    @classmethod
    def non_blank_query(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("query must not be blank")
        return value

    @model_validator(mode="after")
    def unambiguous_version_selection(self) -> SearchRequest:
        if self.version is not None and self.as_of is not None:
            raise ValueError("version and as_of cannot be combined")
        return self


class SearchPage(StrictModel):
    generation_id: Identifier
    items: list[Evidence]
    next_cursor: str | None = None
    retrieval_mode: Literal["hybrid", "lexical_only"] = "hybrid"
    degraded: bool = False
    failed_branch: Literal["vector"] | None = None
    corpus_as_of: datetime | None = None
    no_evidence: bool = False
    low_confidence: bool = False
    conflicting_versions: bool = False
    warnings: tuple[SearchWarning, ...] = ()

    @model_validator(mode="after")
    def consistent_degraded_state(self) -> SearchPage:
        if self.degraded != (self.retrieval_mode == "lexical_only"):
            raise ValueError("degraded must match lexical_only retrieval mode")
        if self.degraded != (self.failed_branch is not None):
            raise ValueError("failed_branch is required only for degraded results")
        if self.no_evidence != (not self.items):
            raise ValueError("no_evidence must match an empty result page")
        expected_warnings = {
            name
            for name, present in (
                ("no_evidence", self.no_evidence),
                ("low_confidence", self.low_confidence),
                ("conflicting_versions", self.conflicting_versions),
                ("vector_degraded", self.degraded),
            )
            if present
        }
        if set(self.warnings) != expected_warnings or len(self.warnings) != len(expected_warnings):
            raise ValueError("warnings must exactly describe the explicit search states")
        return self


class EvidencePage(StrictModel):
    generation_id: Identifier
    evidence_id: Identifier
    document_id: Identifier
    items: list[Evidence]
    next_cursor: str | None = None


class EvidenceLookupRequest(StrictModel):
    """Bounded input contract for adjacent-evidence pagination."""

    evidence_id: Identifier
    cursor: Cursor | None = None
    limit: PageLimit = 20


class ProductVersion(StrictModel):
    issuer: Issuer
    product_code: Identifier
    document_id: Identifier
    version: Identifier
    effective_date: date | None = None
    discovered_at: datetime
    source_sha256: Sha256Hex
    is_latest: bool


class ProductVersions(StrictModel):
    generation_id: Identifier
    issuer: Issuer
    product_code: Identifier
    items: list[ProductVersion]


class SourcePdf(StrictModel):
    """Catalog record for one immutable source PDF.

    ``path`` is an internal capability and is never serialized by a public
    response model.
    """

    document_id: Identifier
    issuer: Issuer
    product_code: Identifier
    version: Identifier
    path: Path
    sha256: Sha256Hex
    size_bytes: int = Field(ge=1)
    mime_type: Literal["application/pdf"] = "application/pdf"


class SourcePdfDescriptor(StrictModel):
    document_id: Identifier
    issuer: Issuer
    product_code: Identifier
    version: Identifier
    url: str
    sha256: Sha256Hex
    size_bytes: int = Field(ge=1)
    mime_type: Literal["application/pdf"] = "application/pdf"
    range_supported: bool = True


class SourcePage(StrictModel):
    document_id: Identifier
    issuer: Issuer
    product_code: Identifier
    version: Identifier
    page: int = Field(ge=1)
    page_count: int = Field(ge=1)
    ocr_text: str
    ocr_sha256: Sha256Hex
    pdf_sha256: Sha256Hex

    @model_validator(mode="after")
    def page_in_document(self) -> SourcePage:
        if self.page > self.page_count:
            raise ValueError("page exceeds page_count")
        return self


class SourcePageDescriptor(StrictModel):
    document_id: Identifier
    issuer: Issuer
    product_code: Identifier
    version: Identifier
    page: int = Field(ge=1)
    page_count: int = Field(ge=1)
    ocr_text: str
    ocr_sha256: Sha256Hex
    pdf_sha256: Sha256Hex
    png_url: str | None = None
    png_cache_ttl_seconds: int | None = Field(default=None, ge=60)


class SourceOcrDescriptor(StrictModel):
    document_id: Identifier
    issuer: Issuer
    product_code: Identifier
    version: Identifier
    page_count: int = Field(ge=1)
    pdf_sha256: Sha256Hex
    page_resource_template: str


class DocumentDescriptor(StrictModel):
    document_id: Identifier
    issuer: Issuer
    product_code: Identifier
    version: Identifier
    sha256: Sha256Hex
    size_bytes: int = Field(ge=1)
    mime_type: Literal["application/pdf"] = "application/pdf"
    pdf_tool: Literal["get_source_pdf"] = "get_source_pdf"
    ocr_resource: str


class ReadinessStatus(StrictModel):
    ready: bool
    generation_id: str | None = None
    checks: dict[str, bool] = Field(default_factory=dict)


AuditAction = Literal[
    "search_evidence",
    "get_evidence",
    "get_product_versions",
    "get_source_pdf",
    "get_source_page",
    "issuer_catalog",
    "index_status",
    "product_resource",
    "document_resource",
    "evidence_resource",
    "source_ocr_resource",
    "source_ocr_page_resource",
    "mcp_transport_auth",
    "source_pdf",
    "source_page_png",
]
AuditOutcome = Literal[
    "success",
    "no_result",
    "allowed",
    "denied",
    "not_found",
    "invalid_source",
    "degraded",
    "timeout",
    "error",
]


class AuditEvent(StrictModel):
    request_id: Identifier
    occurred_at: datetime
    action: AuditAction
    subject_hash: Sha256Hex
    client_id: str | None = Field(default=None, max_length=512)
    granted_scopes: tuple[str, ...] = ()
    document_id: Identifier | None = None
    page: int | None = Field(default=None, ge=1)
    source_sha256: Sha256Hex | None = None
    requested_range: str | None = Field(default=None, max_length=200)
    outcome: AuditOutcome

    @model_validator(mode="after")
    def allowed_event_has_verified_source(self) -> AuditEvent:
        if self.outcome == "allowed" and self.source_sha256 is None:
            raise ValueError("allowed source access requires a verified source hash")
        return self
