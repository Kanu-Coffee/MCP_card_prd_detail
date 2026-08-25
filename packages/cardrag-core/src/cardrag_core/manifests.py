"""Strict generation and OCR publication manifests."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Annotated, Literal, Self

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
from .ocr import NativeOCRContract, OCRInput, native_ocr_reuse_key
from .paths import (
    generation_database_path,
    generation_manifest_path,
    generation_ready_path,
    object_path,
    validate_identifier,
)

_DOCUMENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")
DocumentId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")]
OCRCacheKind = Literal["native", "adopted"]


class EmbeddingContract(StrictFrozenModel):
    provider: NonEmptyText
    model: NonEmptyText
    dimension: Literal[1536]
    count: NonNegativeInt


class GenerationCounts(StrictFrozenModel):
    documents: NonNegativeInt
    pdf_objects: NonNegativeInt
    ocr_objects: NonNegativeInt
    chunks: NonNegativeInt


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
        return self


class GenerationManifest(StrictFrozenModel):
    schema_version: Literal["cardrag.generation.v1"] = "cardrag.generation.v1"
    generation_id: str
    created_at: AwareDatetime
    serving_schema: Literal["cardrag.serving-db.v1"] = "cardrag.serving-db.v1"
    serving_database: ArtifactRef
    corpus_sha256: Sha256Hex
    contract_sha256: Sha256Hex
    embedding_contract: EmbeddingContract
    issuer_codes: tuple[IssuerCode, ...] = Field(min_length=1)
    counts: GenerationCounts
    documents: tuple[GenerationDocument, ...] = ()
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

    @model_validator(mode="after")
    def bundle_is_self_consistent(self) -> Self:
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
        document_issuers = tuple(sorted({document.issuer for document in self.documents}))
        if self.documents and self.issuer_codes != document_issuers:
            raise ValueError("issuer_codes do not match generation documents")
        return self

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

    @field_validator("generation_id")
    @classmethod
    def generation_id_is_safe(cls, value: str) -> str:
        return validate_identifier(value, label="generation_id")

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


class AdoptedOCRArtifactManifest(StrictFrozenModel):
    """Strict legacy OCR artifact without fabricating native model provenance."""

    schema_version: Literal["cardrag.ocr-artifact.v1"] = "cardrag.ocr-artifact.v1"
    origin: Literal["legacy_adoption"] = "legacy_adoption"
    status: Literal["succeeded"] = "succeeded"
    validation_profile: Literal["cardrag.legacy-ocr-adoption.v1"] = "cardrag.legacy-ocr-adoption.v1"
    reuse_key: Sha256Hex
    source: OCRInput
    receipt: LegacyAdoptionReceipt
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
        expected_key = adopted_ocr_reuse_key(
            adoption_policy_version=self.receipt.adoption_policy_version,
            source_document_id=self.receipt.source_document_id,
            pdf_sha256=self.source.pdf_sha256,
        )
        if self.reuse_key != expected_key:
            raise ValueError("adopted OCR reuse key does not match receipt and PDF")
        if self.receipt.pdf_sha256 != self.source.pdf_sha256:
            raise ValueError("adoption receipt PDF hash does not match OCR input")
        if self.receipt.ocr_sha256 != self.output.sha256:
            raise ValueError("adoption receipt OCR hash does not match output")
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


def expected_generation_files(generation_id: str) -> tuple[str, str, str]:
    """Expose the complete fixed generation bundle layout for publishers."""

    return (
        generation_database_path(generation_id).as_posix(),
        generation_manifest_path(generation_id).as_posix(),
        generation_ready_path(generation_id).as_posix(),
    )
