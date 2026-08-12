"""Strict, immutable domain contracts shared by offline and online services."""

from __future__ import annotations

import math
import re
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, Self

from pydantic import (
    AnyHttpUrl,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from .canonical import canonical_json_bytes, canonical_sha256, sha256_bytes

NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
NonNegativeInt = Annotated[int, Field(strict=True, ge=0)]
PositiveInt = Annotated[int, Field(strict=True, gt=0)]
NaturalVersionKey = tuple[tuple[int, int | str], ...]

_CONTROL_CHARACTER = re.compile(r"[\x00-\x1f\x7f]")
_DOCUMENT_TYPE = re.compile(r"^[a-z][a-z0-9_]*$")
_VERSION_TOKEN = re.compile(r"\d+|[^\d]+")


class StrictFrozenModel(BaseModel):
    """Base contract: no coercion, no extra fields, and no mutation."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        str_strip_whitespace=True,
        validate_default=True,
    )


class Issuer(StrEnum):
    """Canonical v1 card issuer codes."""

    WOORI = "woori"
    KB = "kb"
    SHINHAN = "shinhan"


def _validate_plain_text(value: str, field_name: str) -> str:
    if _CONTROL_CHARACTER.search(value):
        raise ValueError(f"{field_name} must not contain control characters")
    return value


def natural_version_key(version: str) -> NaturalVersionKey:
    """Return a total, natural-sort key (`v9 < v10`, `1.2 < 1.10`)."""

    normalized = version.strip().casefold()
    if len(normalized) > 1 and normalized[0] == "v" and normalized[1].isdigit():
        normalized = normalized[1:]
    return tuple(
        (0, int(token)) if token.isdigit() else (1, token) for token in _VERSION_TOKEN.findall(normalized)
    )


class DocumentIdentity(StrictFrozenModel):
    """Issuer-scoped identity of one immutable disclosure document version."""

    issuer: Issuer
    product_code: NonEmptyText
    document_type: NonEmptyText
    effective_date: NonEmptyText
    version: NonEmptyText
    source_sha256: Sha256Hex | None = None

    @field_validator("product_code", "version")
    @classmethod
    def validate_fragments(cls, value: str, info: object) -> str:
        field_name = getattr(info, "field_name", "identity")
        return _validate_plain_text(value, field_name)

    @field_validator("document_type")
    @classmethod
    def validate_document_type(cls, value: str) -> str:
        if not _DOCUMENT_TYPE.fullmatch(value):
            raise ValueError("document_type must be a lowercase snake-case identifier")
        return value

    @field_validator("effective_date")
    @classmethod
    def validate_effective_date(cls, value: str) -> str:
        try:
            parsed = datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError as exc:
            raise ValueError("effective_date must use a valid YYYY-MM-DD date") from exc
        if parsed.isoformat() != value:
            raise ValueError("effective_date must use zero-padded YYYY-MM-DD format")
        return value

    @property
    def canonical_payload(self) -> dict[str, str]:
        payload = {
            "document_type": self.document_type,
            "effective_date": self.effective_date,
            "issuer": self.issuer.value,
            "product_code": self.product_code,
            "version": self.version,
        }
        if self.source_sha256 is not None:
            payload["source_sha256"] = self.source_sha256
        return payload

    @property
    def stable_id(self) -> str:
        return f"doc_{canonical_sha256(self.canonical_payload)}"

    @property
    def doc_version_id(self) -> str:
        """Compatibility name for catalog and adapter boundaries."""

        return self.stable_id

    @property
    def version_sort_key(self) -> NaturalVersionKey:
        return natural_version_key(self.version)

    @property
    def chronological_sort_key(self) -> tuple[str, NaturalVersionKey]:
        return self.effective_date, self.version_sort_key


class EvidenceSourceSpan(StrictFrozenModel):
    """One exact page-local Unicode codepoint fragment of evidence."""

    page: PositiveInt
    start: NonNegativeInt
    end: PositiveInt
    quote_sha256: Sha256Hex

    @model_validator(mode="after")
    def validate_span(self) -> Self:
        if self.end <= self.start:
            raise ValueError("source fragment end must be greater than start")
        return self


class EvidenceIdentity(StrictFrozenModel):
    """Stable ordered multi-span evidence identity independent of rank/generation."""

    document: DocumentIdentity
    source_spans: tuple[EvidenceSourceSpan, ...] = Field(min_length=1)
    text_sha256: Sha256Hex

    @field_validator("source_spans")
    @classmethod
    def validate_source_spans(cls, value: tuple[EvidenceSourceSpan, ...]) -> tuple[EvidenceSourceSpan, ...]:
        ordered = tuple(sorted(value, key=lambda span: (span.page, span.start, span.end)))
        if value != ordered or len(set(value)) != len(value):
            raise ValueError("evidence source spans must be unique and in document order")
        for previous, current in zip(value, value[1:], strict=False):
            if previous.page == current.page and current.start < previous.end:
                raise ValueError("evidence source spans must not overlap")
        return value

    @classmethod
    def from_text(
        cls,
        *,
        document: DocumentIdentity,
        page: int,
        start: int,
        end: int,
        text: str,
    ) -> Self:
        return cls(
            document=document,
            source_spans=(
                EvidenceSourceSpan(
                    page=page,
                    start=start,
                    end=end,
                    quote_sha256=sha256_bytes(text.encode("utf-8")),
                ),
            ),
            text_sha256=sha256_bytes(text.encode("utf-8")),
        )

    @property
    def canonical_payload(self) -> dict[str, object]:
        return {
            "document_id": self.document.stable_id,
            "source_spans": [span.model_dump(mode="json") for span in self.source_spans],
            "text_sha256": self.text_sha256,
        }

    @property
    def stable_id(self) -> str:
        return f"evidence_{canonical_sha256(self.canonical_payload)}"

    @property
    def evidence_id(self) -> str:
        return self.stable_id


class SourceRecord(BaseModel):
    """The single versioned normalized discovery contract for every adapter."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    issuer: Issuer
    product_code: str = Field(min_length=1, max_length=128)
    product_name: str = Field(min_length=1, max_length=500)
    document_type: str = Field(default="product_description", min_length=1, max_length=80)
    effective_date: date
    source_version: str = Field(min_length=1, max_length=128)
    source_url: AnyHttpUrl
    source_post_id: str = Field(min_length=1, max_length=500)
    file_name: str = Field(min_length=1, max_length=500)
    category: str = Field(min_length=1, max_length=100)
    is_current: bool
    discovered_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("discovered_at")
    @classmethod
    def timestamp_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("discovered_at must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def reject_non_pdf_or_credentials(self) -> Self:
        if not self.file_name.lower().endswith(".pdf"):
            raise ValueError("issuer source must name a PDF")
        if self.source_url.username or self.source_url.password:
            raise ValueError("credentials are forbidden in source URLs")
        return self

    def document_identity_for(self, source_sha256: str | None = None) -> DocumentIdentity:
        return DocumentIdentity(
            issuer=self.issuer,
            product_code=self.product_code,
            document_type=self.document_type,
            effective_date=self.effective_date.isoformat(),
            version=self.source_version,
            source_sha256=source_sha256,
        )

    @property
    def document_identity(self) -> DocumentIdentity:
        return self.document_identity_for()


class Lineage(StrictFrozenModel):
    """Reproducible processing lineage for one derived artifact."""

    processor: NonEmptyText
    processor_version: NonEmptyText
    config_sha256: Sha256Hex
    input_sha256: tuple[Sha256Hex, ...] = ()
    input_artifact_ids: tuple[NonEmptyText, ...] = ()
    source_snapshot_id: NonEmptyText | None = None
    prompt_version: NonEmptyText | None = None
    provider: NonEmptyText | None = None
    model: NonEmptyText | None = None
    attempt: PositiveInt = 1

    @field_validator("input_sha256", "input_artifact_ids")
    @classmethod
    def normalize_sets(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("lineage input references must be unique")
        return tuple(sorted(value))

    @property
    def canonical_sha256(self) -> str:
        return canonical_sha256(self)


class ArtifactType(StrEnum):
    SOURCE_PDF = "source_pdf"
    OCR_MARKDOWN = "ocr_markdown"
    OCR_PAGE_MAP = "ocr_page_map"
    STRUCTURED = "structured"
    EMBEDDING = "embedding"
    LEXICAL_INDEX = "lexical_index"
    VECTOR_INDEX = "vector_index"
    GENERATION_MANIFEST = "generation_manifest"
    QUALITY_REPORT = "quality_report"


class ManifestAttribute(StrictFrozenModel):
    """Small canonical metadata entry; names are unique inside a manifest."""

    name: NonEmptyText
    value: str | bool | int | float

    @field_validator("value")
    @classmethod
    def validate_value(cls, value: str | bool | int | float) -> str | bool | int | float:
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("manifest attributes must be finite")
        return value


class ArtifactManifest(StrictFrozenModel):
    """Canonical manifest for a byte artifact and its complete lineage."""

    schema_version: Literal["cardrag.artifact-manifest.v1"] = "cardrag.artifact-manifest.v1"
    artifact_type: ArtifactType
    content_sha256: Sha256Hex
    size_bytes: NonNegativeInt
    media_type: NonEmptyText
    created_at: AwareDatetime
    lineage: Lineage
    document: DocumentIdentity | None = None
    page_count: PositiveInt | None = None
    item_count: NonNegativeInt | None = None
    attributes: tuple[ManifestAttribute, ...] = ()

    @field_validator("created_at")
    @classmethod
    def normalize_created_at(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @field_validator("attributes")
    @classmethod
    def normalize_attributes(cls, value: tuple[ManifestAttribute, ...]) -> tuple[ManifestAttribute, ...]:
        names = [attribute.name for attribute in value]
        if len(names) != len(set(names)):
            raise ValueError("manifest attribute names must be unique")
        return tuple(sorted(value, key=lambda attribute: attribute.name))

    @classmethod
    def for_bytes(
        cls,
        *,
        artifact_type: ArtifactType,
        payload: bytes,
        media_type: str,
        created_at: datetime,
        lineage: Lineage,
        document: DocumentIdentity | None = None,
        page_count: int | None = None,
        item_count: int | None = None,
        attributes: tuple[ManifestAttribute, ...] = (),
    ) -> Self:
        return cls(
            artifact_type=artifact_type,
            content_sha256=sha256_bytes(payload),
            size_bytes=len(payload),
            media_type=media_type,
            created_at=created_at,
            lineage=lineage,
            document=document,
            page_count=page_count,
            item_count=item_count,
            attributes=attributes,
        )

    @property
    def artifact_id(self) -> str:
        identity = {"sha256": self.content_sha256, "type": self.artifact_type.value}
        return f"artifact_{canonical_sha256(identity)}"

    @property
    def manifest_id(self) -> str:
        return f"manifest_{canonical_sha256(self)}"

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self)

    @property
    def canonical_sha256(self) -> str:
        return canonical_sha256(self)
