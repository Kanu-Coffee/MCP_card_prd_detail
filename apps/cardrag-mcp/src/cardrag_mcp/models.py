"""Strict public and repository contracts for the active serving generation."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

Identifier = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=512),
]
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Issuer(StrictModel):
    code: Identifier
    display_name: str = Field(min_length=1, max_length=256)
    sort_order: int


class Document(StrictModel):
    document_id: Identifier
    issuer: Identifier
    product_code: Identifier
    title: str = Field(min_length=1, max_length=1_000)
    pdf_sha256: Sha256Hex
    pdf_size_bytes: int = Field(ge=1, le=100 * 1024 * 1024)
    page_count: int = Field(ge=1)


class Product(StrictModel):
    issuer: Identifier
    product_code: Identifier
    name: str = Field(min_length=1, max_length=1_000)
    availability: Literal["available"] = "available"
    document: Document


class UnsupportedProduct(StrictModel):
    issuer: Identifier
    product_code: Identifier
    name: str = Field(min_length=1, max_length=1_000)
    availability: Literal["unsupported_drm"] = "unsupported_drm"
    source_id: Identifier
    source_version: str = Field(min_length=1, max_length=512)
    source_url: str = Field(min_length=1, max_length=4_096)
    protected_magic: Literal["SCDSA002", "SCDSA004"]
    protected_source_sha256: Sha256Hex
    protected_source_size_bytes: int = Field(ge=1)


class SourcePage(StrictModel):
    document_id: Identifier
    page: int = Field(ge=1)
    page_count: int = Field(ge=1)
    text: str
    text_sha256: Sha256Hex
    pdf_sha256: Sha256Hex


class Evidence(StrictModel):
    evidence_id: Identifier
    document_id: Identifier
    issuer: Identifier
    product_code: Identifier
    product_name: str
    document_title: str
    page_start: int = Field(ge=1)
    page_end: int = Field(ge=1)
    section_type: str = Field(min_length=1, max_length=256)
    text: str = Field(min_length=1)
    source_start: int = Field(ge=0)
    source_end: int = Field(ge=0)
    pdf_sha256: Sha256Hex
    score: float | None = None
    lexical_rank: int | None = Field(default=None, ge=1)
    vector_rank: int | None = Field(default=None, ge=1)


class SearchFilters(StrictModel):
    issuer: Identifier | None = None
    product_code: Identifier | None = None
    document_id: Identifier | None = None
    section_type: str | None = Field(default=None, min_length=1, max_length=256)


class SearchPage(StrictModel):
    generation_id: Identifier
    items: tuple[Evidence, ...]
    next_cursor: str | None = Field(default=None, max_length=2_048)
    retrieval_mode: Literal["hybrid", "lexical_only"]
    degraded: bool = False


class EvidencePage(StrictModel):
    generation_id: Identifier
    evidence_id: Identifier
    document_id: Identifier
    items: tuple[Evidence, ...]
    next_cursor: str | None = Field(default=None, max_length=2_048)


class SourcePdfDescriptor(StrictModel):
    document_id: Identifier
    url: str
    sha256: Sha256Hex
    size_bytes: int = Field(ge=1, le=100 * 1024 * 1024)
    mime_type: Literal["application/pdf"] = "application/pdf"
    range_supported: Literal[True] = True


class ServingMetadata(StrictModel):
    schema_id: Literal["cardrag.serving-db.v2"]
    generation_id: Identifier
    corpus_sha256: Sha256Hex
    embedding_provider: str = Field(min_length=1, max_length=256)
    embedding_model: str = Field(min_length=1, max_length=512)
    embedding_input_policy_version: Literal["cardrag.embedding-input.v1"]
    embedding_dimension: Literal[1536]
    embedding_count: int = Field(ge=0)
    unsupported_document_count: int = Field(ge=0)
    unsupported_documents_sha256: Sha256Hex


class SearchRequest(StrictModel):
    query: str = Field(min_length=1, max_length=2_000)
    filters: SearchFilters = Field(default_factory=SearchFilters)
    limit: int = Field(default=10, ge=1, le=50)
    cursor: str | None = Field(default=None, max_length=2_048)
    allow_degraded: bool = False

    @field_validator("query")
    @classmethod
    def query_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("query must not be blank")
        return value
