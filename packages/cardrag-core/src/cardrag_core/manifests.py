"""Strict generation and OCR publication manifests."""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Annotated, Literal, Self, overload

from pydantic import AwareDatetime, Field, StringConstraints, field_validator, model_validator

from .canonical import canonical_json_bytes, canonical_sha256
from .domain import (
    ArtifactRef,
    IssuerCode,
    NonEmptyText,
    NonNegativeInt,
    PositiveInt,
    Sha256Hex,
    StrictFrozenModel,
)
from .embedding import (
    QWEN3_DOCUMENT_INSTRUCTION,
    QWEN3_DOCUMENT_POLICY,
    QWEN3_EMBEDDING_DIMENSION,
    QWEN3_EMBEDDING_DTYPE,
    QWEN3_EMBEDDING_MODEL,
    QWEN3_EMBEDDING_NORMALIZATION,
    QWEN3_EMBEDDING_PROVIDER,
    QWEN3_EMBEDDING_PROVIDER_FALLBACK_POLICY,
    QWEN3_QUERY_POLICY,
    QWEN3_TRUNCATION_POLICY,
    Qwen3EmbeddingProviderId,
    qwen3_embedding_cache_namespace,
    qwen3_embedding_profile_id,
)
from .ocr import NativeOCRContract, OCRInput, native_ocr_reuse_key
from .paths import (
    generation_database_path,
    generation_manifest_path,
    generation_ready_path,
    generation_vectors_path,
    object_path,
    validate_identifier,
)

_DOCUMENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")
DocumentId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")]
OCRCacheKind = Literal["native", "adopted"]
GenerationAvailability = Literal["available", "ocr_failed"]
OCRFailureReasonCode = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z0-9_]{1,64}$"),
]
OCRFailureReasonText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=256),
]

LEGACY_ADOPTION_POLICY_V1: Literal["cardrag.legacy-ocr-adoption.v1"] = "cardrag.legacy-ocr-adoption.v1"
LEGACY_ADOPTION_POLICY_V2: Literal["cardrag.legacy-ocr-adoption.v2"] = "cardrag.legacy-ocr-adoption.v2"
LEGACY_OCR_NORMALIZATION_EXACT: Literal["exact"] = "exact"
LEGACY_OCR_NORMALIZATION_STRIP_PREFIX_V1: Literal["strip-exact-generated-prefix-v1"] = (
    "strip-exact-generated-prefix-v1"
)
LEGACY_OCR_APPROVED_PREFIX = "# OCR 처리 완료본\n\n".encode()
LEGACY_OCR_APPROVED_PREFIX_SHA256 = "dd547e4a08542b54f8cc2f1f90b4c71d97bec67a5c286cfcb2d59587bb4adc48"
LegacyOCRNormalizationProfile = Literal[
    "exact",
    "strip-exact-generated-prefix-v1",
]


class EmbeddingContract(StrictFrozenModel):
    provider: NonEmptyText
    model: NonEmptyText
    dimension: Literal[1536, 4096]
    count: NonNegativeInt


EmbeddingProfileId = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"),
]
EmbeddingCacheNamespace = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"),
]
DocumentAggregationPolicy = Literal[
    "max_child",
    "top3_mean",
    "contract_plus_child",
]
EmbeddingViewType = Literal[
    "TITLE",
    "RAW_ITEM",
    "CONTEXTUAL_ITEM",
    "DETAIL",
    "MAJOR_SECTION",
    "CONTRACT",
]
EMBEDDING_VIEW_TYPES: tuple[EmbeddingViewType, ...] = (
    "TITLE",
    "RAW_ITEM",
    "CONTEXTUAL_ITEM",
    "DETAIL",
    "MAJOR_SECTION",
    "CONTRACT",
)


class EmbeddingProfile(StrictFrozenModel):
    """One fully sealed, provider-routed Qwen v5 embedding profile."""

    schema_version: Literal["cardrag.embedding-profile.v1"]
    profile_id: EmbeddingProfileId
    cache_namespace: EmbeddingCacheNamespace
    provider: Literal["openrouter"]
    provider_id: Qwen3EmbeddingProviderId
    provider_fallback: Literal["forbidden"]
    model: Literal["qwen/qwen3-embedding-8b"]
    dimension: Literal[4096]
    dtype: Literal["float32"]
    normalization: Literal["l2"]
    document_instruction: None
    query_policy: Literal["cardrag.qwen3-query.v1"]
    document_policy: Literal["cardrag.structure-views.v1"]
    truncation: Literal["error"]
    maximum_tokens: PositiveInt

    @classmethod
    def qwen3(
        cls,
        *,
        provider_id: Qwen3EmbeddingProviderId,
        maximum_tokens: int,
    ) -> Self:
        """Construct the only supported v5 profile without caller-owned IDs."""

        return cls(
            schema_version="cardrag.embedding-profile.v1",
            profile_id=qwen3_embedding_profile_id(provider_id, maximum_tokens=maximum_tokens),
            cache_namespace=qwen3_embedding_cache_namespace(provider_id, maximum_tokens=maximum_tokens),
            provider=QWEN3_EMBEDDING_PROVIDER,
            provider_id=provider_id,
            provider_fallback=QWEN3_EMBEDDING_PROVIDER_FALLBACK_POLICY,
            model=QWEN3_EMBEDDING_MODEL,
            dimension=QWEN3_EMBEDDING_DIMENSION,
            dtype=QWEN3_EMBEDDING_DTYPE,
            normalization=QWEN3_EMBEDDING_NORMALIZATION,
            document_instruction=QWEN3_DOCUMENT_INSTRUCTION,
            query_policy=QWEN3_QUERY_POLICY,
            document_policy=QWEN3_DOCUMENT_POLICY,
            truncation=QWEN3_TRUNCATION_POLICY,
            maximum_tokens=maximum_tokens,
        )

    @model_validator(mode="after")
    def identity_is_bound_to_every_input_contract_field(self) -> Self:
        expected_profile_id = qwen3_embedding_profile_id(
            self.provider_id,
            maximum_tokens=self.maximum_tokens,
        )
        expected_namespace = qwen3_embedding_cache_namespace(
            self.provider_id,
            maximum_tokens=self.maximum_tokens,
        )
        if self.profile_id != expected_profile_id:
            raise ValueError("embedding profile ID does not match its sealed Qwen contract")
        if self.cache_namespace != expected_namespace:
            raise ValueError("embedding cache namespace does not match its sealed Qwen contract")
        return self


class EmbeddingViewCount(StrictFrozenModel):
    view_type: EmbeddingViewType
    count: NonNegativeInt


class EmbeddingVectorSidecar(StrictFrozenModel):
    """Identity and binary layout of the exact-search matrix."""

    artifact: ArtifactRef
    profile_id: EmbeddingProfileId
    row_count: NonNegativeInt
    dimension: Literal[4096]
    dtype: Literal["float32"]
    byte_order: Literal["little-endian"]
    layout: Literal["row-major"]
    normalization: Literal["l2"]

    @model_validator(mode="after")
    def byte_size_matches_full_fp32_matrix(self) -> Self:
        if self.artifact.media_type != "application/octet-stream":
            raise ValueError("vector sidecar media type must be application/octet-stream")
        expected_size = self.row_count * self.dimension * 4
        if self.artifact.size_bytes != expected_size:
            raise ValueError("vector sidecar size must equal row_count times dimension times four")
        return self


class MaxChildAggregationDefinition(StrictFrozenModel):
    child_view_types: tuple[
        Literal[
            "CONTEXTUAL_ITEM",
            "DETAIL",
            "MAJOR_SECTION",
            "RAW_ITEM",
            "TITLE",
        ],
        ...,
    ]
    formula: Literal["max(non-CONTRACT row score)"]

    @model_validator(mode="after")
    def child_lanes_are_complete_and_canonical(self) -> Self:
        expected = (
            "CONTEXTUAL_ITEM",
            "DETAIL",
            "MAJOR_SECTION",
            "RAW_ITEM",
            "TITLE",
        )
        if self.child_view_types != expected:
            raise ValueError("max-child definition must list every non-CONTRACT lane canonically")
        return self


class Top3MeanAggregationDefinition(StrictFrozenModel):
    child_count: Literal[3]
    formula: Literal["mean(highest min(3, available) non-CONTRACT row scores)"]


class ContractPlusChildAggregationDefinition(StrictFrozenModel):
    child_policy: Literal["max_child"]
    child_weight: float = Field(strict=True, ge=0.5, le=0.5)
    contract_view_policy: Literal["single CONTRACT row score"]
    contract_weight: float = Field(strict=True, ge=0.5, le=0.5)
    formula: Literal["0.5*CONTRACT + 0.5*max_child"]


DocumentAggregationDefinition = (
    MaxChildAggregationDefinition | Top3MeanAggregationDefinition | ContractPlusChildAggregationDefinition
)


class DocumentAggregationBootstrap(StrictFrozenModel):
    ci: float = Field(strict=True, ge=0.95, le=0.95)
    method: Literal["paired-query-percentile-pcg64"]
    samples: int = Field(strict=True, ge=2_000, le=10_000)
    seed: int = Field(strict=True, ge=0)


class DocumentAggregationProfile(StrictFrozenModel):
    """One gold-selected policy, bound to the generation used for evaluation.

    ``generation_manifest_sha256`` intentionally identifies the immutable
    evaluation generation.  A later serving generation can embed this profile
    without creating an impossible mutual hash dependency.
    """

    schema_version: Literal["cardrag.document-aggregation-profile.v1"]
    profile_id: Annotated[
        str,
        StringConstraints(pattern=r"^cardrag[.]document-aggregation[.][a-z0-9-]+[.]v1$"),
    ]
    aggregation_policy: DocumentAggregationPolicy
    aggregation_definition: DocumentAggregationDefinition
    bootstrap: DocumentAggregationBootstrap
    embedding_profile_id: EmbeddingProfileId
    exact_row_corpus_sha256: Sha256Hex
    generation_id: str
    generation_manifest_sha256: Sha256Hex
    gold_sha256: Sha256Hex
    score_artifact_sha256: Sha256Hex
    selection_objective: Literal["ndcg_at_10"]

    @field_validator("generation_id")
    @classmethod
    def generation_id_is_safe(cls, value: str) -> str:
        return validate_identifier(value, label="aggregation evaluation generation_id")

    @model_validator(mode="after")
    def policy_definition_and_id_are_consistent(self) -> Self:
        expected_type: type[StrictFrozenModel]
        if self.aggregation_policy == "max_child":
            expected_type = MaxChildAggregationDefinition
        elif self.aggregation_policy == "top3_mean":
            expected_type = Top3MeanAggregationDefinition
        else:
            expected_type = ContractPlusChildAggregationDefinition
        if not isinstance(self.aggregation_definition, expected_type):
            raise ValueError("document aggregation policy and definition differ")
        expected_id = "cardrag.document-aggregation." + self.aggregation_policy.replace("_", "-") + ".v1"
        if self.profile_id != expected_id:
            raise ValueError("document aggregation profile ID differs from its selected policy")
        return self

    @property
    def profile_sha256(self) -> str:
        return canonical_sha256(self)


def sealed_v5_retrieval_policy(
    profile: DocumentAggregationProfile,
    profile_sha256: str,
) -> dict[str, object]:
    """Return the canonical v5 retrieval contract for a sealed winner."""

    if not re.fullmatch(r"[0-9a-f]{64}", profile_sha256):
        raise ValueError("document aggregation profile SHA-256 is invalid")
    if profile.profile_sha256 != profile_sha256:
        raise ValueError("document aggregation profile SHA-256 does not bind the profile")
    return {
        "aggregation": {
            "policy": profile.aggregation_policy,
            "profile_id": profile.profile_id,
            "sealed_profile_sha256": profile_sha256,
        },
        "candidate_prefilter": "none",
        "dense_scan": "exact-all-active-rows.v1",
        "lexical_fusion": "forbidden",
        "schema_version": "cardrag.retrieval-policy.v2",
        "temporal_scope": "current",
    }


def v5_exact_row_corpus_sha256(
    *,
    embedding_profile_id: str,
    vector_sidecar_sha256: str,
    rows: Sequence[tuple[int, str, str, str, str, str]],
    revisions: Sequence[tuple[str, str, str | None, str]],
) -> str:
    """Bind the exact matrix rows and temporal revision scope without a manifest cycle."""

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", embedding_profile_id):
        raise ValueError("exact-row corpus embedding profile ID is invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", vector_sidecar_sha256):
        raise ValueError("exact-row corpus vector SHA-256 is invalid")
    expected_indices = list(range(len(rows)))
    if [row[0] for row in rows] != expected_indices:
        raise ValueError("exact-row corpus row indices must be contiguous and ordered")
    if any(row[5] != embedding_profile_id for row in rows):
        raise ValueError("exact-row corpus row uses another embedding profile")
    if len({(row[1], row[2], row[3]) for row in rows}) != len(rows):
        raise ValueError("exact-row corpus contains duplicate revision/node/view identities")
    if tuple(revisions) != tuple(sorted(set(revisions))):
        raise ValueError("exact-row corpus revisions must be sorted and unique")
    revision_ids = {row[0] for row in revisions}
    if {row[1] for row in rows} != revision_ids:
        raise ValueError("exact-row corpus rows and revisions differ")
    return canonical_sha256(
        {
            "embedding_profile_id": embedding_profile_id,
            "revisions": [
                {
                    "contract_revision_id": contract_revision_id,
                    "effective_date": effective_date,
                    "product_lineage_id": product_lineage_id,
                    "temporal_status": temporal_status,
                }
                for (
                    contract_revision_id,
                    product_lineage_id,
                    effective_date,
                    temporal_status,
                ) in revisions
            ],
            "rows": [
                {
                    "contract_revision_id": contract_revision_id,
                    "embedding_profile_id": profile_id,
                    "input_sha256": input_sha256,
                    "node_id": node_id,
                    "row_index": row_index,
                    "view_type": view_type,
                }
                for (
                    row_index,
                    contract_revision_id,
                    node_id,
                    view_type,
                    input_sha256,
                    profile_id,
                ) in rows
            ],
            "schema_version": "cardrag.exact-row-corpus.v1",
            "vector_sidecar_sha256": vector_sidecar_sha256,
        }
    )


class IssuerParserProfile(StrictFrozenModel):
    issuer: IssuerCode
    profile_id: EmbeddingProfileId
    profile_sha256: Sha256Hex


class StructureNodeCounts(StrictFrozenModel):
    total: NonNegativeInt
    root: NonNegativeInt
    major_section: NonNegativeInt
    item: NonNegativeInt
    paragraph: NonNegativeInt
    list_item: NonNegativeInt
    table: NonNegativeInt
    table_row: NonNegativeInt
    footnote: NonNegativeInt
    boilerplate: NonNegativeInt
    unclassified: NonNegativeInt

    @model_validator(mode="after")
    def total_matches_node_types(self) -> Self:
        typed_total = (
            self.root
            + self.major_section
            + self.item
            + self.paragraph
            + self.list_item
            + self.table
            + self.table_row
            + self.footnote
            + self.boilerplate
            + self.unclassified
        )
        if self.total != typed_total:
            raise ValueError("structure node total does not match node type counts")
        return self


class StructureMajorClassCounts(StrictFrozenModel):
    total: NonNegativeInt
    benefit: NonNegativeInt
    notice: NonNegativeInt
    mixed: NonNegativeInt
    unknown: NonNegativeInt

    @model_validator(mode="after")
    def total_matches_major_classes(self) -> Self:
        if self.total != self.benefit + self.notice + self.mixed + self.unknown:
            raise ValueError("major class total does not match class counts")
        return self


class StructureSourceCoverage(StrictFrozenModel):
    source_non_whitespace_characters: NonNegativeInt
    covered_non_whitespace_characters: NonNegativeInt
    source_non_whitespace_sha256: Sha256Hex
    covered_non_whitespace_sha256: Sha256Hex

    @model_validator(mode="after")
    def coverage_is_exactly_complete(self) -> Self:
        if self.covered_non_whitespace_characters != self.source_non_whitespace_characters:
            raise ValueError("structure source coverage must be 100 percent")
        if self.covered_non_whitespace_sha256 != self.source_non_whitespace_sha256:
            raise ValueError("covered structure source hash must equal source hash")
        return self


class StructureRevisionCounts(StrictFrozenModel):
    total: NonNegativeInt
    current: NonNegativeInt
    superseded: NonNegativeInt
    ambiguous: NonNegativeInt

    @model_validator(mode="after")
    def total_matches_temporal_statuses(self) -> Self:
        if self.total != self.current + self.superseded + self.ambiguous:
            raise ValueError("contract revision total does not match temporal status counts")
        return self


class StructureContract(StrictFrozenModel):
    schema_version: Literal["cardrag.structure.v2"]
    parser_profiles: tuple[IssuerParserProfile, ...] = Field(min_length=1)
    node_counts: StructureNodeCounts
    major_class_counts: StructureMajorClassCounts
    source_coverage: StructureSourceCoverage
    revision_counts: StructureRevisionCounts
    cross_contract_parent_count: Literal[0]
    cross_contract_link_count: Literal[0]
    lineages_with_multiple_current_revisions: Literal[0]

    @field_validator("parser_profiles")
    @classmethod
    def parser_profiles_are_sorted_unique(
        cls,
        value: tuple[IssuerParserProfile, ...],
    ) -> tuple[IssuerParserProfile, ...]:
        issuers = [profile.issuer for profile in value]
        if issuers != sorted(issuers) or len(issuers) != len(set(issuers)):
            raise ValueError("issuer parser profiles must be sorted and unique")
        return value


class GenerationCounts(StrictFrozenModel):
    documents: NonNegativeInt
    pdf_objects: NonNegativeInt
    ocr_objects: NonNegativeInt
    chunks: NonNegativeInt


class GenerationOCRFailure(StrictFrozenModel):
    """Bounded, provider-secret-free reason for one isolated OCR failure."""

    reason_code: OCRFailureReasonCode
    reason: OCRFailureReasonText
    attempts: PositiveInt

    @field_validator("reason")
    @classmethod
    def reason_is_one_line(cls, value: str) -> str:
        if "\n" in value or "\r" in value:
            raise ValueError("OCR failure reason must be one line")
        return value


class IssuerOCRCounts(StrictFrozenModel):
    issuer: IssuerCode
    acquired: PositiveInt
    succeeded: PositiveInt
    failed: NonNegativeInt

    @model_validator(mode="after")
    def counts_are_consistent(self) -> Self:
        if self.acquired != self.succeeded + self.failed:
            raise ValueError("issuer OCR acquired count must equal succeeded plus failed")
        return self


class GenerationDocument(StrictFrozenModel):
    """One served document and, when usable, its exact remote OCR cache identity.

    ``ocr_cache_kind`` and ``ocr_reuse_key`` stay absent for generation-only OCR
    whose cache control files were not successfully verified and committed.
    """

    document_id: DocumentId
    issuer: IssuerCode
    pdf: ArtifactRef
    ocr: ArtifactRef | None = None
    ocr_cache_kind: OCRCacheKind | None = None
    ocr_reuse_key: Sha256Hex | None = None
    page_count: PositiveInt
    # These fields are absent from v1-v3 canonical manifests. ``exclude_if``
    # preserves their exact historical bytes while allowing an explicit v4
    # disposition for every acquired PDF.
    availability: GenerationAvailability | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    ocr_failure: GenerationOCRFailure | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def artifact_contracts_are_cas_bound(self) -> Self:
        if self.pdf.media_type != "application/pdf":
            raise ValueError("generation PDF media type must be application/pdf")
        if PurePosixPath(self.pdf.path) != object_path(self.pdf.sha256):
            raise ValueError("generation PDF must reference its CAS object path")
        if self.ocr is not None:
            if self.ocr.media_type != "text/markdown; charset=utf-8":
                raise ValueError("generation OCR media type is invalid")
            if PurePosixPath(self.ocr.path) != object_path(self.ocr.sha256):
                raise ValueError("generation OCR must reference its CAS object path")
        cache_fields_present = (self.ocr_cache_kind is not None, self.ocr_reuse_key is not None)
        if cache_fields_present[0] != cache_fields_present[1]:
            raise ValueError("OCR cache kind and reuse key must be provided together")
        if self.ocr is None and cache_fields_present[0]:
            raise ValueError("OCR cache identity requires a generation OCR artifact")
        if self.availability == "available":
            if self.ocr is None or self.ocr_failure is not None:
                raise ValueError("available generation document requires OCR and no failure")
        elif self.availability == "ocr_failed":
            if self.ocr is not None or self.ocr_failure is None:
                raise ValueError("ocr_failed generation document requires only a failure summary")
        elif self.ocr_failure is not None:
            raise ValueError("legacy generation document cannot contain an OCR failure summary")
        return self


class GenerationManifest(StrictFrozenModel):
    schema_version: Literal[
        "cardrag.generation.v1",
        "cardrag.generation.v2",
        "cardrag.generation.v3",
        "cardrag.generation.v4",
        "cardrag.generation.v5",
    ] = "cardrag.generation.v3"
    generation_id: str
    created_at: AwareDatetime
    serving_schema: Literal[
        "cardrag.serving-db.v1",
        "cardrag.serving-db.v2",
        "cardrag.serving-db.v3",
        "cardrag.serving-db.v4",
        "cardrag.serving-db.v5",
    ] = "cardrag.serving-db.v3"
    serving_database: ArtifactRef
    corpus_sha256: Sha256Hex
    contract_sha256: Sha256Hex
    embedding_contract: EmbeddingContract
    issuer_codes: tuple[IssuerCode, ...] = Field(min_length=1)
    counts: GenerationCounts
    documents: tuple[GenerationDocument, ...] = ()
    issuer_ocr_counts: tuple[IssuerOCRCounts, ...] = Field(
        default=(),
        exclude_if=lambda value: not value,
    )
    # Every v5-only field is excluded while absent so parsing and re-encoding a
    # historical v1-v4 manifest produces exactly its original canonical bytes.
    structure_contract: StructureContract | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    embedding_profiles: tuple[EmbeddingProfile, ...] = Field(
        default=(),
        exclude_if=lambda value: not value,
    )
    primary_embedding_profile_id: EmbeddingProfileId | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    embedding_view_counts: tuple[EmbeddingViewCount, ...] = Field(
        default=(),
        exclude_if=lambda value: not value,
    )
    vector_sidecar: EmbeddingVectorSidecar | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    parser_policy_sha256: Sha256Hex | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    embedding_policy_sha256: Sha256Hex | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    retrieval_policy_sha256: Sha256Hex | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    document_aggregation_profile: DocumentAggregationProfile | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    document_aggregation_policy: DocumentAggregationPolicy | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    sealed_profile_sha256: Sha256Hex | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    exact_row_corpus_sha256: Sha256Hex | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    previous_generation_id: str | None = None

    @field_validator("generation_id")
    @classmethod
    def generation_id_is_safe(cls, value: str) -> str:
        return validate_identifier(value, label="generation_id")

    @field_validator("previous_generation_id")
    @classmethod
    def previous_generation_id_is_safe(cls, value: str | None) -> str | None:
        return None if value is None else validate_identifier(value, label="previous_generation_id")

    @field_validator("created_at")
    @classmethod
    def normalize_created_at(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @field_validator("issuer_codes")
    @classmethod
    def issuer_codes_are_sorted_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("issuer_codes must be sorted and unique")
        return value

    @field_validator("documents")
    @classmethod
    def documents_are_sorted_unique(
        cls,
        value: tuple[GenerationDocument, ...],
    ) -> tuple[GenerationDocument, ...]:
        keys = [document.document_id for document in value]
        if keys != sorted(keys) or len(keys) != len(set(keys)):
            raise ValueError("generation documents must be sorted and unique by document_id")
        return value

    @field_validator("issuer_ocr_counts")
    @classmethod
    def issuer_ocr_counts_are_sorted_unique(
        cls,
        value: tuple[IssuerOCRCounts, ...],
    ) -> tuple[IssuerOCRCounts, ...]:
        keys = [row.issuer for row in value]
        if keys != sorted(keys) or len(keys) != len(set(keys)):
            raise ValueError("issuer OCR counts must be sorted and unique")
        return value

    @field_validator("embedding_profiles")
    @classmethod
    def embedding_profiles_are_sorted_unique(
        cls,
        value: tuple[EmbeddingProfile, ...],
    ) -> tuple[EmbeddingProfile, ...]:
        profile_ids = [profile.profile_id for profile in value]
        if profile_ids != sorted(profile_ids) or len(profile_ids) != len(set(profile_ids)):
            raise ValueError("embedding profiles must be sorted and unique by profile_id")
        cache_namespaces = [profile.cache_namespace for profile in value]
        if len(cache_namespaces) != len(set(cache_namespaces)):
            raise ValueError("embedding profiles must use distinct cache namespaces")
        return value

    @field_validator("embedding_view_counts")
    @classmethod
    def embedding_view_counts_are_canonical_when_present(
        cls,
        value: tuple[EmbeddingViewCount, ...],
    ) -> tuple[EmbeddingViewCount, ...]:
        view_types = [row.view_type for row in value]
        if view_types and tuple(view_types) != EMBEDDING_VIEW_TYPES:
            raise ValueError("embedding view counts must use the canonical complete view order")
        return value

    @model_validator(mode="after")
    def bundle_is_self_consistent(self) -> Self:
        expected_serving_schema = {
            "cardrag.generation.v1": "cardrag.serving-db.v1",
            "cardrag.generation.v2": "cardrag.serving-db.v2",
            "cardrag.generation.v3": "cardrag.serving-db.v3",
            "cardrag.generation.v4": "cardrag.serving-db.v4",
            "cardrag.generation.v5": "cardrag.serving-db.v5",
        }[self.schema_version]
        if self.serving_schema != expected_serving_schema:
            raise ValueError("generation and serving database schema versions must match")
        if PurePosixPath(self.serving_database.path) != generation_database_path(self.generation_id):
            raise ValueError("serving database path does not match generation_id")
        if self.serving_database.media_type != "application/vnd.sqlite3":
            raise ValueError("serving database media type must be application/vnd.sqlite3")
        if self.previous_generation_id == self.generation_id:
            raise ValueError("generation cannot name itself as its predecessor")
        if self.counts.documents != len(self.documents):
            raise ValueError("generation document count does not match documents")
        if self.counts.pdf_objects != len({document.pdf.sha256 for document in self.documents}):
            raise ValueError("generation PDF object count does not match documents")
        if self.counts.ocr_objects != len(
            {document.ocr.sha256 for document in self.documents if document.ocr is not None}
        ):
            raise ValueError("generation OCR object count does not match documents")
        if self.embedding_contract.count != self.counts.chunks:
            raise ValueError("embedding count must equal generation chunk count")
        if self.schema_version == "cardrag.generation.v5":
            if self.embedding_contract.dimension != QWEN3_EMBEDDING_DIMENSION:
                raise ValueError("v5 embedding dimension must be 4096")
        elif self.embedding_contract.dimension != 1536:
            raise ValueError("v1-v4 embedding dimension must be 1536")
        document_issuers = {document.issuer for document in self.documents}
        if not document_issuers.issubset(self.issuer_codes):
            raise ValueError("generation documents reference an undeclared issuer")
        if self.schema_version in {"cardrag.generation.v4", "cardrag.generation.v5"}:
            if any(document.availability is None for document in self.documents):
                raise ValueError("v4-v5 generation documents require explicit availability")
            count_issuers = tuple(row.issuer for row in self.issuer_ocr_counts)
            if count_issuers != self.issuer_codes:
                raise ValueError("v4-v5 issuer OCR counts must cover every declared issuer")
            if sum(row.acquired for row in self.issuer_ocr_counts) != self.counts.documents:
                raise ValueError("v4-v5 issuer OCR counts differ from generation document count")
            availability_counts = {
                issuer: (
                    sum(
                        document.availability == "available"
                        for document in self.documents
                        if document.issuer == issuer
                    ),
                    sum(
                        document.availability == "ocr_failed"
                        for document in self.documents
                        if document.issuer == issuer
                    ),
                )
                for issuer in self.issuer_codes
            }
            for row in self.issuer_ocr_counts:
                succeeded, failed = availability_counts[row.issuer]
                if (row.succeeded, row.failed) != (succeeded, failed):
                    raise ValueError("v4-v5 issuer OCR counts differ from document availability")
                if row.succeeded * 100 < row.acquired * 95:
                    raise ValueError("v4-v5 issuer OCR success rate is below 95 percent")
        elif self.issuer_ocr_counts or any(document.availability is not None for document in self.documents):
            raise ValueError("OCR publication dispositions require generation v4 or v5")
        if self.schema_version == "cardrag.generation.v5":
            self._validate_v5_contract()
        elif any(
            (
                self.structure_contract is not None,
                bool(self.embedding_profiles),
                self.primary_embedding_profile_id is not None,
                bool(self.embedding_view_counts),
                self.vector_sidecar is not None,
                self.parser_policy_sha256 is not None,
                self.embedding_policy_sha256 is not None,
                self.retrieval_policy_sha256 is not None,
                self.document_aggregation_profile is not None,
                self.document_aggregation_policy is not None,
                self.sealed_profile_sha256 is not None,
                self.exact_row_corpus_sha256 is not None,
            )
        ):
            raise ValueError("v5 structure and vector fields require generation v5")
        return self

    def _validate_v5_contract(self) -> None:
        if self.structure_contract is None:
            raise ValueError("v5 generation requires a structure contract")
        if not self.embedding_profiles:
            raise ValueError("v5 generation requires embedding profiles")
        if self.primary_embedding_profile_id is None:
            raise ValueError("v5 generation requires a primary embedding profile")
        if self.vector_sidecar is None:
            raise ValueError("v5 generation requires a vector sidecar")
        if any(
            policy_hash is None
            for policy_hash in (
                self.parser_policy_sha256,
                self.embedding_policy_sha256,
                self.retrieval_policy_sha256,
            )
        ):
            raise ValueError("v5 generation requires parser, embedding, and retrieval policy hashes")

        parser_issuers = tuple(profile.issuer for profile in self.structure_contract.parser_profiles)
        if parser_issuers != self.issuer_codes:
            raise ValueError("v5 parser profiles must cover every declared issuer")
        if (
            self.structure_contract.major_class_counts.total
            != self.structure_contract.node_counts.major_section
        ):
            raise ValueError("v5 major class counts must cover every major section")

        profiles_by_id = {profile.profile_id: profile for profile in self.embedding_profiles}
        primary = profiles_by_id.get(self.primary_embedding_profile_id)
        if primary is None:
            raise ValueError("primary embedding profile is not present in embedding profiles")
        if (
            self.embedding_contract.provider != primary.provider
            or self.embedding_contract.model != primary.model
            or self.embedding_contract.dimension != primary.dimension
        ):
            raise ValueError("primary embedding profile does not match embedding contract")
        if self.vector_sidecar.profile_id != primary.profile_id:
            raise ValueError("vector sidecar must be bound to the primary embedding profile")
        if self.vector_sidecar.dimension != primary.dimension:
            raise ValueError("vector sidecar dimension does not match primary profile")
        if self.vector_sidecar.dtype != primary.dtype:
            raise ValueError("vector sidecar dtype does not match primary profile")
        if self.vector_sidecar.normalization != primary.normalization:
            raise ValueError("vector sidecar normalization does not match primary profile")
        if PurePosixPath(self.vector_sidecar.artifact.path) != generation_vectors_path(self.generation_id):
            raise ValueError("vector sidecar path does not match generation_id")
        if self.vector_sidecar.row_count != self.counts.chunks:
            raise ValueError("vector sidecar row count must equal generation chunk count")

        view_types = tuple(row.view_type for row in self.embedding_view_counts)
        if view_types != EMBEDDING_VIEW_TYPES:
            raise ValueError("v5 embedding view counts must explicitly cover every view type")
        if sum(row.count for row in self.embedding_view_counts) != self.vector_sidecar.row_count:
            raise ValueError("embedding view counts must equal vector sidecar row count")

        aggregation_presence = (
            self.document_aggregation_profile is not None,
            self.document_aggregation_policy is not None,
            self.sealed_profile_sha256 is not None,
            self.exact_row_corpus_sha256 is not None,
        )
        if len(set(aggregation_presence)) != 1:
            raise ValueError("v5 document aggregation profile identity is all-or-nothing")
        if self.document_aggregation_profile is not None:
            profile_sha256 = self.sealed_profile_sha256
            if profile_sha256 is None:  # guarded above
                raise ValueError("v5 document aggregation profile SHA-256 is absent")
            if self.document_aggregation_profile.profile_sha256 != profile_sha256:
                raise ValueError("v5 document aggregation profile SHA-256 is inconsistent")
            if self.document_aggregation_profile.aggregation_policy != self.document_aggregation_policy:
                raise ValueError("v5 document aggregation policy differs from its profile")
            if self.document_aggregation_profile.embedding_profile_id != primary.profile_id:
                raise ValueError("document aggregation profile uses another embedding profile")
            if self.document_aggregation_profile.exact_row_corpus_sha256 != self.exact_row_corpus_sha256:
                raise ValueError("document aggregation profile uses another exact-row corpus")
            expected_retrieval_sha256 = canonical_sha256(
                sealed_v5_retrieval_policy(
                    self.document_aggregation_profile,
                    profile_sha256,
                )
            )
            if self.retrieval_policy_sha256 != expected_retrieval_sha256:
                raise ValueError("retrieval policy SHA-256 does not bind document aggregation")

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self)

    @property
    def manifest_sha256(self) -> str:
        return canonical_sha256(self)


class GenerationReady(StrictFrozenModel):
    schema_version: Literal["cardrag.generation-ready.v1"] = "cardrag.generation-ready.v1"
    generation_id: str
    manifest_sha256: Sha256Hex
    serving_database_sha256: Sha256Hex
    serving_database_size_bytes: NonNegativeInt
    vector_sidecar_sha256: Sha256Hex | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    vector_sidecar_size_bytes: NonNegativeInt | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )

    @field_validator("generation_id")
    @classmethod
    def generation_id_is_safe(cls, value: str) -> str:
        return validate_identifier(value, label="generation_id")

    @model_validator(mode="after")
    def vector_sidecar_identity_is_all_or_nothing(self) -> Self:
        if (self.vector_sidecar_sha256 is None) != (self.vector_sidecar_size_bytes is None):
            raise ValueError("vector sidecar SHA-256 and size must be provided together")
        return self

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self)


class GenerationPointer(StrictFrozenModel):
    schema_version: Literal["cardrag.generation-pointer.v1"] = "cardrag.generation-pointer.v1"
    generation_id: str
    manifest_sha256: Sha256Hex
    ready_sha256: Sha256Hex

    @field_validator("generation_id")
    @classmethod
    def generation_id_is_safe(cls, value: str) -> str:
        return validate_identifier(value, label="generation_id")

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self)


class OCRArtifactManifest(StrictFrozenModel):
    schema_version: Literal["cardrag.ocr-artifact.v1"] = "cardrag.ocr-artifact.v1"
    origin: Literal["native"] = "native"
    status: Literal["succeeded"] = "succeeded"
    validation_profile: Literal["cardrag.ocr-markdown.v1"] = "cardrag.ocr-markdown.v1"
    reuse_key: Sha256Hex
    source: OCRInput
    contract: NativeOCRContract
    output: ArtifactRef
    ocr_chars: PositiveInt
    page_output_sha256: tuple[Sha256Hex, ...] = Field(min_length=1)
    created_at: AwareDatetime

    @field_validator("created_at")
    @classmethod
    def normalize_created_at(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def native_contract_is_bound(self) -> Self:
        if self.reuse_key != native_ocr_reuse_key(self.contract, self.source):
            raise ValueError("OCR reuse key does not match source and contract")
        if self.output.media_type != "text/markdown; charset=utf-8":
            raise ValueError("OCR output media type is invalid")
        if PurePosixPath(self.output.path) != object_path(self.output.sha256):
            raise ValueError("OCR output must use its CAS object path")
        if len(self.page_output_sha256) != self.source.page_count:
            raise ValueError("OCR manifest requires one page hash per source page")
        return self

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self)

    @property
    def manifest_sha256(self) -> str:
        return canonical_sha256(self)


class OCRReady(StrictFrozenModel):
    schema_version: Literal["cardrag.ocr-ready.v1"] = "cardrag.ocr-ready.v1"
    reuse_key: Sha256Hex
    manifest_sha256: Sha256Hex
    ocr_sha256: Sha256Hex

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self)


def adopted_ocr_reuse_key(
    *,
    adoption_policy_version: str,
    source_document_id: str,
    pdf_sha256: str,
) -> str:
    """Return the pre-output lookup key for one explicitly adopted legacy OCR."""

    if not adoption_policy_version.strip():
        raise ValueError("adoption_policy_version must not be empty")
    if not _DOCUMENT_ID.fullmatch(source_document_id):
        raise ValueError("source_document_id is invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", pdf_sha256):
        raise ValueError("pdf_sha256 is invalid")
    return canonical_sha256(
        {
            "adoption_policy_version": adoption_policy_version,
            "pdf_sha256": pdf_sha256,
            "schema_version": "cardrag.adopted-ocr-reuse-key.v1",
            "source_document_id": source_document_id,
        }
    )


class LegacyAdoptionValidation(StrictFrozenModel):
    hash_verified: Literal[True]
    page_coverage_verified: Literal[True]
    utf8_verified: Literal[True]
    ledger_bound: Literal[True]


class LegacyAdoptionReceipt(StrictFrozenModel):
    """Self-contained receipt minted only after a legacy DB ledger check."""

    schema_version: Literal["cardrag.legacy-adoption-receipt.v1"] = "cardrag.legacy-adoption-receipt.v1"
    adoption_policy_version: NonEmptyText
    source_bundle_id: NonEmptyText
    source_bundle_sha256: Sha256Hex
    source_database_id: NonEmptyText
    source_document_id: DocumentId
    pdf_sha256: Sha256Hex
    ocr_sha256: Sha256Hex
    validation: LegacyAdoptionValidation


class LegacyAdoptionValidationV2(StrictFrozenModel):
    """Proofs made while both original and normalized legacy bytes were available."""

    source_hash_verified: Literal[True]
    normalized_hash_verified: Literal[True]
    transformation_verified: Literal[True]
    page_coverage_verified: Literal[True]
    utf8_verified: Literal[True]
    ledger_bound: Literal[True]


class LegacyAdoptionReceiptV2(StrictFrozenModel):
    """Dual-lineage receipt for a narrowly approved legacy normalization."""

    schema_version: Literal["cardrag.legacy-adoption-receipt.v2"] = "cardrag.legacy-adoption-receipt.v2"
    adoption_policy_version: Literal["cardrag.legacy-ocr-adoption.v2"] = LEGACY_ADOPTION_POLICY_V2
    source_bundle_id: NonEmptyText
    source_bundle_sha256: Sha256Hex
    source_database_id: NonEmptyText
    source_document_id: DocumentId
    pdf_sha256: Sha256Hex
    source_ocr_sha256: Sha256Hex
    source_ocr_size_bytes: PositiveInt
    normalized_ocr_sha256: Sha256Hex
    normalized_ocr_size_bytes: PositiveInt
    normalization_profile: LegacyOCRNormalizationProfile
    prefix_sha256: Sha256Hex | None = None
    removed_bytes: NonNegativeInt
    validation: LegacyAdoptionValidationV2

    @model_validator(mode="after")
    def normalization_is_narrow_and_fully_bound(self) -> Self:
        if self.normalization_profile == LEGACY_OCR_NORMALIZATION_EXACT:
            if self.source_ocr_sha256 != self.normalized_ocr_sha256:
                raise ValueError("exact adoption requires identical source and normalized OCR hashes")
            if self.source_ocr_size_bytes != self.normalized_ocr_size_bytes:
                raise ValueError("exact adoption requires identical source and normalized OCR sizes")
            if self.prefix_sha256 is not None or self.removed_bytes != 0:
                raise ValueError("exact adoption cannot remove a prefix")
            return self
        if self.normalization_profile != LEGACY_OCR_NORMALIZATION_STRIP_PREFIX_V1:
            raise ValueError("unsupported legacy OCR normalization profile")
        if self.prefix_sha256 != LEGACY_OCR_APPROVED_PREFIX_SHA256:
            raise ValueError("prefix-strip adoption requires the approved prefix hash")
        if self.removed_bytes != len(LEGACY_OCR_APPROVED_PREFIX):
            raise ValueError("prefix-strip adoption requires exactly 24 removed bytes")
        if self.source_ocr_size_bytes != self.normalized_ocr_size_bytes + self.removed_bytes:
            raise ValueError("prefix-strip adoption OCR sizes do not match removed bytes")
        if self.source_ocr_sha256 == self.normalized_ocr_sha256:
            raise ValueError("prefix-strip adoption requires distinct source and normalized OCR hashes")
        return self


class AdoptedOCRArtifactManifest(StrictFrozenModel):
    """Strict legacy OCR artifact without fabricating native model provenance."""

    schema_version: Literal["cardrag.ocr-artifact.v1", "cardrag.ocr-artifact.v2"] = "cardrag.ocr-artifact.v1"
    origin: Literal["legacy_adoption"] = "legacy_adoption"
    status: Literal["succeeded"] = "succeeded"
    validation_profile: Literal[
        "cardrag.legacy-ocr-adoption.v1",
        "cardrag.legacy-ocr-adoption.v2",
    ] = "cardrag.legacy-ocr-adoption.v1"
    reuse_key: Sha256Hex
    source: OCRInput
    receipt: LegacyAdoptionReceipt | LegacyAdoptionReceiptV2
    output: ArtifactRef
    ocr_chars: PositiveInt
    page_output_sha256: tuple[Sha256Hex, ...] = Field(min_length=1)
    created_at: AwareDatetime

    @field_validator("created_at")
    @classmethod
    def normalize_created_at(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def adoption_is_fully_bound(self) -> Self:
        if isinstance(self.receipt, LegacyAdoptionReceiptV2):
            if (
                self.schema_version != "cardrag.ocr-artifact.v2"
                or self.validation_profile != LEGACY_ADOPTION_POLICY_V2
            ):
                raise ValueError("v2 adoption receipt requires the v2 artifact/profile contract")
            receipt_ocr_sha256 = self.receipt.normalized_ocr_sha256
            receipt_ocr_size_bytes = self.receipt.normalized_ocr_size_bytes
        else:
            if (
                self.schema_version != "cardrag.ocr-artifact.v1"
                or self.validation_profile != LEGACY_ADOPTION_POLICY_V1
                or self.receipt.adoption_policy_version != LEGACY_ADOPTION_POLICY_V1
            ):
                raise ValueError("v1 adoption receipt requires the v1 artifact/profile contract")
            receipt_ocr_sha256 = self.receipt.ocr_sha256
            receipt_ocr_size_bytes = self.output.size_bytes
        expected_key = adopted_ocr_reuse_key(
            adoption_policy_version=self.receipt.adoption_policy_version,
            source_document_id=self.receipt.source_document_id,
            pdf_sha256=self.source.pdf_sha256,
        )
        if self.reuse_key != expected_key:
            raise ValueError("adopted OCR reuse key does not match receipt and PDF")
        if self.receipt.pdf_sha256 != self.source.pdf_sha256:
            raise ValueError("adoption receipt PDF hash does not match OCR input")
        if receipt_ocr_sha256 != self.output.sha256:
            raise ValueError("adoption receipt OCR hash does not match output")
        if receipt_ocr_size_bytes != self.output.size_bytes:
            raise ValueError("adoption receipt OCR size does not match output")
        if self.output.media_type != "text/markdown; charset=utf-8":
            raise ValueError("adopted OCR output media type is invalid")
        if PurePosixPath(self.output.path) != object_path(self.output.sha256):
            raise ValueError("adopted OCR output must use its CAS object path")
        if len(self.page_output_sha256) != self.source.page_count:
            raise ValueError("adopted OCR requires one page hash per source page")
        return self

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self)

    @property
    def manifest_sha256(self) -> str:
        return canonical_sha256(self)


@overload
def expected_generation_files(
    generation_id: str,
    *,
    include_vectors: Literal[False] = False,
) -> tuple[str, str, str]: ...


@overload
def expected_generation_files(
    generation_id: str,
    *,
    include_vectors: Literal[True],
) -> tuple[str, str, str, str]: ...


def expected_generation_files(
    generation_id: str,
    *,
    include_vectors: bool = False,
) -> tuple[str, ...]:
    """Expose legacy or opt-in v5 generation layouts without changing old calls."""

    legacy_files = (
        generation_database_path(generation_id).as_posix(),
        generation_manifest_path(generation_id).as_posix(),
        generation_ready_path(generation_id).as_posix(),
    )
    if not include_vectors:
        return legacy_files
    return (*legacy_files, generation_vectors_path(generation_id).as_posix())
