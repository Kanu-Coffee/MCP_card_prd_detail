"""Strict public and repository contracts for the active serving generation."""

from __future__ import annotations

import math
from datetime import date
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

Identifier = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=512),
]
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
MAX_CONTRACT_SEARCH_LIMIT = 100
MAX_SEARCH_RESPONSE_NODES: Literal[2048] = 2_048
MAX_SEARCH_RESPONSE_CHARACTERS: Literal[1000000] = 1_000_000
DocumentAggregationPolicy = Literal["max_child", "top3_mean", "contract_plus_child"]


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
    protected_magic: Literal["SCDSA002", "SCDSA004", "FASOO_DRMONE"]
    protected_source_sha256: Sha256Hex
    protected_source_size_bytes: int = Field(ge=1)


class OCRFailedProduct(StrictModel):
    issuer: Identifier
    product_code: Identifier
    name: str = Field(min_length=1, max_length=1_000)
    availability: Literal["ocr_failed"] = "ocr_failed"
    document: Document
    reason_code: Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]{1,64}$")]
    reason: str = Field(min_length=1, max_length=256)
    attempts: int = Field(ge=1)

    @field_validator("reason")
    @classmethod
    def reason_must_be_one_line(cls, value: str) -> str:
        if "\n" in value or "\r" in value:
            raise ValueError("OCR failure reason must be one line")
        return value


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
    retrieval_mode: Literal["hybrid", "lexical_only", "exact"]
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
    schema_id: Literal[
        "cardrag.serving-db.v2",
        "cardrag.serving-db.v3",
        "cardrag.serving-db.v4",
        "cardrag.serving-db.v5",
    ]
    generation_id: Identifier
    corpus_sha256: Sha256Hex
    contract_sha256: Sha256Hex
    embedding_provider: str = Field(min_length=1, max_length=256)
    embedding_model: str = Field(min_length=1, max_length=512)
    embedding_input_policy_version: Literal[
        "cardrag.embedding-input.v1",
        "cardrag.structure-views.v1",
    ]
    embedding_dimension: Literal[1536, 4096]
    embedding_count: int = Field(ge=0)
    unsupported_document_count: int = Field(ge=0)
    unsupported_documents_sha256: Sha256Hex
    ocr_failed_document_count: int = Field(ge=0)
    ocr_failed_documents_sha256: Sha256Hex
    primary_embedding_profile_id: Identifier | None = None
    vector_sidecar_sha256: Sha256Hex | None = None
    vector_sidecar_size_bytes: int | None = Field(default=None, ge=0)
    exact_row_corpus_sha256: Sha256Hex | None = None
    document_aggregation_status: Literal["candidate_default", "sealed"] = "candidate_default"
    document_aggregation_policy: DocumentAggregationPolicy = "max_child"
    sealed_profile_sha256: Sha256Hex | None = None

    @model_validator(mode="after")
    def document_aggregation_identity_is_coherent(self) -> ServingMetadata:
        if self.document_aggregation_status == "sealed":
            if self.sealed_profile_sha256 is None or self.exact_row_corpus_sha256 is None:
                raise ValueError("sealed document aggregation metadata is incomplete")
        elif (
            self.document_aggregation_policy != "max_child"
            or self.sealed_profile_sha256 is not None
        ):
            raise ValueError("candidate-default aggregation must be unsealed max_child")
        return self


TemporalStatus = Literal["current", "superseded", "ambiguous"]
NodeType = Literal[
    "ROOT",
    "MAJOR_SECTION",
    "ITEM",
    "PARAGRAPH",
    "LIST_ITEM",
    "TABLE",
    "TABLE_ROW",
    "FOOTNOTE",
    "BOILERPLATE",
    "UNCLASSIFIED",
]
MajorClass = Literal["BENEFIT", "NOTICE", "MIXED", "UNKNOWN"]
ViewType = Literal[
    "TITLE",
    "RAW_ITEM",
    "CONTEXTUAL_ITEM",
    "DETAIL",
    "MAJOR_SECTION",
    "CONTRACT",
]


class ContractRevisionSummary(StrictModel):
    product_lineage_id: Identifier
    contract_revision_id: Identifier
    document_id: Identifier
    issuer: Identifier
    product_code: Identifier
    product_name: str = Field(min_length=1, max_length=1_000)
    document_type: Identifier
    source_id: Identifier
    source_version: str = Field(min_length=1, max_length=512)
    source_url: str = Field(min_length=1, max_length=4_096)
    effective_date: date | None = None
    temporal_status: TemporalStatus
    supersedes_revision_id: Identifier | None = None
    pdf_sha256: Sha256Hex
    page_count: int = Field(ge=1)


class SourceSpan(StrictModel):
    node_id: Identifier
    page: int = Field(ge=1)
    source_start: int = Field(ge=0)
    source_end: int = Field(gt=0)
    text_sha256: Sha256Hex
    span_ordinal: int = Field(ge=0)
    text: str

    @model_validator(mode="after")
    def source_range_is_nonempty(self) -> SourceSpan:
        if self.source_end <= self.source_start:
            raise ValueError("source span end must follow start")
        return self


class StructureNodeLink(StrictModel):
    from_node_id: Identifier
    to_node_id: Identifier
    link_type: Literal["CONTINUATION_OF", "FOOTNOTE_OF", "APPLIES_TO", "PREVIOUS", "NEXT"]


class StructureNode(StrictModel):
    node_id: Identifier
    contract_revision_id: Identifier
    parent_id: Identifier | None = None
    node_type: NodeType
    major_class: MajorClass
    raw_heading: str | None = Field(default=None, max_length=2_000)
    ordinal: int = Field(ge=0)
    display_text: str
    spans: tuple[SourceSpan, ...] = ()
    links: tuple[StructureNodeLink, ...] = ()
    table_headers: tuple[str, ...] = ()
    table_cells: tuple[str, ...] = ()
    table_role: Literal["HEADER", "SEPARATOR", "BODY"] | None = None

    @model_validator(mode="after")
    def table_metadata_matches_node_type(self) -> StructureNode:
        if self.node_type == "TABLE_ROW":
            if self.table_role is None or not self.table_cells:
                raise ValueError("table row requires original cells and a role")
        elif self.node_type == "TABLE":
            if self.table_cells or self.table_role is not None:
                raise ValueError("table container cannot contain row-only metadata")
        elif self.table_headers or self.table_cells or self.table_role is not None:
            raise ValueError("non-table node cannot contain table metadata")
        return self


class ScoredEmbeddingView(StrictModel):
    row_index: int = Field(ge=0)
    view_type: ViewType
    score: float
    display_text: str = Field(min_length=1)
    spans: tuple[SourceSpan, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def score_and_source_are_valid(self) -> ScoredEmbeddingView:
        if not math.isfinite(self.score):
            raise ValueError("embedding view score must be finite")
        if "".join(span.text for span in self.spans) != self.display_text:
            raise ValueError("embedding view display text must equal its source spans")
        return self


class ScoredStructureNode(StrictModel):
    node: StructureNode
    score: float
    matched_view_types: tuple[ViewType, ...] = Field(min_length=1)
    matched_views: tuple[ScoredEmbeddingView, ...] = Field(min_length=1)
    lexical_only: bool = False

    @model_validator(mode="after")
    def view_scores_bind_the_node_score(self) -> ScoredStructureNode:
        if len({view.row_index for view in self.matched_views}) != len(self.matched_views):
            raise ValueError("matched embedding view rows must be unique")
        best = max(view.score for view in self.matched_views)
        if not math.isclose(self.score, best, abs_tol=1e-7):
            raise ValueError("structure node score must equal its best matched view")
        best_types = tuple(
            view.view_type
            for view in self.matched_views
            if math.isclose(view.score, best, abs_tol=1e-7)
        )
        if self.matched_view_types != best_types:
            raise ValueError("matched view types must identify every best-scoring lane")
        return self


class ContractEvidenceBundle(StrictModel):
    contract: ContractRevisionSummary
    matches: tuple[ScoredStructureNode, ...]
    nodes: tuple[StructureNode, ...]
    linked_notice_count: int = Field(ge=0)
    parent_expansion_count: int = Field(ge=0)

    @property
    def context_character_count(self) -> int:
        """Count the actual serialized bundle characters, including repeated context."""

        return len(self.model_dump_json())


class SearchCoverage(StrictModel):
    generation_id: Identifier
    search_mode: Literal["exact", "exhaustive"]
    temporal_scope: Literal["current", "as_of", "history"]
    expected_active_contracts: int = Field(ge=0)
    scored_contracts: int = Field(ge=0)
    expected_embedding_rows: int = Field(ge=0)
    scored_embedding_rows: int = Field(ge=0)
    document_aggregation_status: Literal["candidate_default", "sealed"] = "candidate_default"
    document_aggregation_policy: DocumentAggregationPolicy = "max_child"
    sealed_profile_sha256: Sha256Hex | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    exact_row_corpus_sha256: Sha256Hex | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    approximate: Literal[False] = False
    lexical_influenced_ranking: Literal[False] = False
    reranker_influenced_ranking: Literal[False] = False
    reranker_shadow_status: Literal["succeeded", "failed"] | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    reranker_shadow_candidate_count: int | None = Field(
        default=None,
        ge=0,
        le=256,
        exclude_if=lambda value: value is None,
    )
    reranker_shadow_rank_change_count: int | None = Field(
        default=None,
        ge=0,
        le=256,
        exclude_if=lambda value: value is None,
    )
    reranker_shadow_artifact_sha256: Sha256Hex | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    reranker_shadow_failure_reason: (
        Literal[
            "provider_request_failed",
            "provider_contract_invalid",
            "candidate_input_invalid",
            "artifact_store_failed",
            "shadow_internal_error",
        ]
        | None
    ) = Field(default=None, exclude_if=lambda value: value is None)
    exact_search_milliseconds: float = Field(ge=0)
    exact_blocks: int = Field(ge=0)
    lexical_additional_evidence_count: int = Field(ge=0)
    lexical_enabled: bool
    lexical_status: Literal["succeeded", "failed", "deferred"]
    lexical_error: Literal["fts_unavailable", "query_invalid", "internal_error"] | None = None
    lexical_global_matched_evidence_count: int = Field(ge=0)
    lexical_global_additional_evidence_count: int = Field(ge=0)
    catalog_resolution_status: Literal["explicit", "resolved", "unresolved", "ambiguous"]
    catalog_candidate_count: int = Field(ge=0)
    catalog_resolved_product_lineage_id: Identifier | None = None
    catalog_resolved_product_name: str | None = None
    response_node_count: int = Field(ge=0, le=MAX_SEARCH_RESPONSE_NODES)
    response_character_count: int = Field(ge=0, le=MAX_SEARCH_RESPONSE_CHARACTERS)
    response_node_limit: Literal[2048] = MAX_SEARCH_RESPONSE_NODES
    response_character_limit: Literal[1000000] = MAX_SEARCH_RESPONSE_CHARACTERS
    response_truncated: bool = False
    full_contract_fallback_count: int = Field(default=0, ge=0)
    exhaustive_status: Literal["running", "complete"] | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    exhaustive_job_id: Identifier | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    exhaustive_profile_id: Identifier | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    exhaustive_completed_contracts: int | None = Field(
        default=None,
        ge=0,
        exclude_if=lambda value: value is None,
    )
    exhaustive_total_contracts: int | None = Field(
        default=None,
        ge=0,
        exclude_if=lambda value: value is None,
    )
    exhaustive_resumed: bool | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    exhaustive_artifact_sha256: Sha256Hex | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def document_aggregation_diagnostics_are_coherent(self) -> SearchCoverage:
        if self.document_aggregation_status == "sealed":
            if self.sealed_profile_sha256 is None or self.exact_row_corpus_sha256 is None:
                raise ValueError("sealed aggregation coverage identity is incomplete")
        elif (
            self.document_aggregation_policy != "max_child"
            or self.sealed_profile_sha256 is not None
        ):
            raise ValueError("candidate-default aggregation coverage must be max_child")
        return self

    @model_validator(mode="after")
    def reranker_shadow_diagnostics_are_coherent(self) -> SearchCoverage:
        diagnostics = (
            self.reranker_shadow_candidate_count,
            self.reranker_shadow_rank_change_count,
            self.reranker_shadow_artifact_sha256,
            self.reranker_shadow_failure_reason,
        )
        if self.reranker_shadow_status is None:
            if any(value is not None for value in diagnostics):
                raise ValueError("reranker shadow diagnostics require a status")
            return self
        if self.reranker_shadow_candidate_count is None:
            raise ValueError("reranker shadow status requires a candidate count")
        if self.reranker_shadow_status == "succeeded":
            if (
                self.reranker_shadow_rank_change_count is None
                or self.reranker_shadow_artifact_sha256 is None
                or self.reranker_shadow_failure_reason is not None
            ):
                raise ValueError("successful reranker shadow diagnostics are incomplete")
            if self.reranker_shadow_rank_change_count > self.reranker_shadow_candidate_count:
                raise ValueError("reranker shadow rank changes exceed its candidates")
            return self
        if (
            self.reranker_shadow_rank_change_count is not None
            or self.reranker_shadow_failure_reason is None
        ):
            raise ValueError("failed reranker shadow diagnostics are incomplete")
        if (
            self.reranker_shadow_failure_reason
            in {
                "provider_request_failed",
                "provider_contract_invalid",
            }
            and self.reranker_shadow_artifact_sha256 is None
        ):
            raise ValueError("provider failure requires a reranker shadow artifact")
        return self

    @model_validator(mode="after")
    def lexical_catalog_and_response_diagnostics_are_coherent(self) -> SearchCoverage:
        if self.lexical_status == "failed":
            if self.lexical_error is None:
                raise ValueError("failed lexical audit requires a bounded error reason")
        elif self.lexical_error is not None:
            raise ValueError("successful or deferred lexical audit cannot declare an error")
        if not self.lexical_enabled and self.lexical_status != "failed":
            raise ValueError("disabled lexical audit must fail closed with a reason")
        if (
            self.lexical_global_additional_evidence_count
            > self.lexical_global_matched_evidence_count
            or self.lexical_additional_evidence_count
            > self.lexical_global_additional_evidence_count
        ):
            raise ValueError("lexical additional evidence exceeds its global matched audit")
        if self.lexical_status != "succeeded" and any(
            (
                self.lexical_additional_evidence_count,
                self.lexical_global_matched_evidence_count,
                self.lexical_global_additional_evidence_count,
            )
        ):
            raise ValueError("failed or deferred lexical audits cannot expose evidence counts")
        if self.catalog_resolution_status in {"explicit", "resolved"}:
            if (
                self.catalog_candidate_count != 1
                or self.catalog_resolved_product_lineage_id is None
            ):
                raise ValueError("resolved catalog diagnostics require exactly one lineage")
        elif self.catalog_resolved_product_lineage_id is not None:
            raise ValueError("unresolved catalog diagnostics cannot declare a lineage")
        if self.catalog_resolution_status == "resolved":
            if not self.catalog_resolved_product_name:
                raise ValueError("catalog resolution requires the matched product name")
        elif self.catalog_resolution_status != "explicit" and self.catalog_resolved_product_name:
            raise ValueError("only resolved catalog diagnostics can declare a product name")
        if self.catalog_resolution_status == "unresolved" and self.catalog_candidate_count != 0:
            raise ValueError("unresolved catalog diagnostics cannot declare candidates")
        if self.catalog_resolution_status == "ambiguous" and self.catalog_candidate_count < 2:
            raise ValueError("ambiguous catalog diagnostics require multiple candidates")
        return self

    @model_validator(mode="after")
    def exhaustive_diagnostics_are_complete_or_absent(self) -> SearchCoverage:
        diagnostics = (
            self.exhaustive_status,
            self.exhaustive_job_id,
            self.exhaustive_profile_id,
            self.exhaustive_completed_contracts,
            self.exhaustive_total_contracts,
            self.exhaustive_resumed,
            self.exhaustive_artifact_sha256,
        )
        if self.search_mode == "exact":
            if any(value is not None for value in diagnostics):
                raise ValueError("exact search cannot declare an exhaustive audit job")
            return self
        if any(value is None for value in diagnostics[:-1]):
            raise ValueError("exhaustive search requires complete audit diagnostics")
        if self.exhaustive_status == "running":
            if (
                self.exhaustive_completed_contracts is None
                or self.exhaustive_total_contracts is None
                or self.exhaustive_completed_contracts >= self.exhaustive_total_contracts
                or self.exhaustive_artifact_sha256 is not None
            ):
                raise ValueError("running exhaustive audit has invalid progress diagnostics")
        elif (
            self.exhaustive_completed_contracts != self.exhaustive_total_contracts
            or self.exhaustive_artifact_sha256 is None
        ):
            raise ValueError("complete exhaustive audit is missing its artifact or contracts")
        return self

    @model_validator(mode="after")
    def score_coverage_is_complete_or_a_bound_progress_prefix(self) -> SearchCoverage:
        if (
            self.scored_contracts > self.expected_active_contracts
            or self.scored_embedding_rows > self.expected_embedding_rows
        ):
            raise ValueError("scored coverage cannot exceed its expected corpus")
        completed = self.exhaustive_completed_contracts
        total = self.exhaustive_total_contracts
        if self.search_mode == "exhaustive" and (
            completed is None
            or total is None
            or total != self.expected_active_contracts
            or completed != self.scored_contracts
        ):
            raise ValueError("exhaustive progress counters differ from scored coverage")
        if self.search_mode == "exact" or self.exhaustive_status == "complete":
            if (
                self.scored_contracts != self.expected_active_contracts
                or self.scored_embedding_rows != self.expected_embedding_rows
            ):
                raise ValueError("complete search must score every expected contract and row")
            return self
        if completed is None or total is None:
            raise ValueError("running exhaustive coverage requires progress counters")
        if (
            self.scored_contracts >= self.expected_active_contracts
            or self.scored_embedding_rows >= self.expected_embedding_rows
        ):
            raise ValueError("running exhaustive coverage is not a strict scored prefix")
        return self


class ContractSearchPage(StrictModel):
    generation_id: Identifier
    bundles: tuple[ContractEvidenceBundle, ...]
    coverage: SearchCoverage

    @model_validator(mode="after")
    def bundles_respect_the_sealed_response_budget(self) -> ContractSearchPage:
        node_count = sum(len(bundle.nodes) for bundle in self.bundles)
        character_count = sum(bundle.context_character_count for bundle in self.bundles)
        if (
            node_count != self.coverage.response_node_count
            or character_count != self.coverage.response_character_count
        ):
            raise ValueError("contract bundles differ from response budget diagnostics")
        if self.coverage.exhaustive_status == "running" and self.bundles:
            raise ValueError("running exhaustive audit cannot expose partial bundles")
        return self


class ContractSearchRequest(StrictModel):
    query: str = Field(min_length=1, max_length=2_000)
    issuer: Identifier | None = None
    product_lineage_id: Identifier | None = None
    as_of: date | None = None
    include_history: bool = False
    mode: Literal["exact", "exhaustive"] = "exact"
    limit: int = Field(default=10, ge=1, le=MAX_CONTRACT_SEARCH_LIMIT)

    @field_validator("query")
    @classmethod
    def contract_query_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("query must not be blank")
        return value

    @model_validator(mode="after")
    def temporal_scope_is_unambiguous(self) -> ContractSearchRequest:
        if self.include_history and self.as_of is not None:
            raise ValueError("include_history and as_of are mutually exclusive")
        return self


class ContractBundle(StrictModel):
    generation_id: Identifier
    contract: ContractRevisionSummary
    scope: Literal["full", "benefits", "notices"]
    nodes: tuple[StructureNode, ...]


class ProductRevisionList(StrictModel):
    generation_id: Identifier
    issuer: Identifier
    product_lineage_id: Identifier
    revisions: tuple[ContractRevisionSummary, ...]


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


class ProductCatalogEntry(StrictModel):
    """Card product catalog entry with launch and revision dates."""

    issuer: Identifier
    product_code: Identifier
    product_lineage_id: Identifier
    product_name: str = Field(min_length=1, max_length=1_000)
    document_type: Identifier
    effective_date: date | None = None
    launch_date: date | None = None
    temporal_status: TemporalStatus


class ProductCatalogPage(StrictModel):
    """Card product catalog page response."""

    generation_id: Identifier
    items: tuple[ProductCatalogEntry, ...]
    total_count: int = Field(ge=0)


class MerchantSearchHit(StrictModel):
    """Matched card product with benefit text excerpts."""

    issuer: Identifier
    product_code: Identifier
    product_name: str = Field(min_length=1, max_length=1_000)
    matched_texts: tuple[str, ...] = Field(min_length=1)


class MerchantSearchPage(StrictModel):
    """Merchant benefit search response."""

    generation_id: Identifier
    merchant_query: str
    items: tuple[MerchantSearchHit, ...]
    total_count: int = Field(ge=0)


class ProductSummary(StrictModel):
    """Compact summary of a card product."""

    generation_id: Identifier
    issuer: Identifier
    product_code: Identifier
    product_name: str = Field(min_length=1, max_length=1_000)
    effective_date: date | None = None
    launch_date: date | None = None
    annual_fee_text: str | None = None
    benefit_headings: tuple[str, ...] = ()
    benefit_summary_texts: tuple[str, ...] = ()
