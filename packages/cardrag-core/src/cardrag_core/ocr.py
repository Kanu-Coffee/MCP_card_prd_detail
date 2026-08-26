"""Native OCR processing identity and byte-level verification."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Self, cast

from pydantic import (
    Field,
    SerializerFunctionWrapHandler,
    StringConstraints,
    model_serializer,
    model_validator,
)

from .canonical import canonical_sha256, sha256_bytes
from .domain import (
    NonEmptyText,
    NonNegativeInt,
    PositiveInt,
    Sha256Hex,
    StrictFrozenModel,
)

OCR_CONTRACT_SCHEMA: Literal["cardrag.ocr-contract.v2"] = "cardrag.ocr-contract.v2"
LEGACY_OCR_CONTRACT_SCHEMA: Literal["cardrag.ocr-contract.v1"] = "cardrag.ocr-contract.v1"
OCR_INPUT_SCHEMA: Literal["cardrag.ocr-input.v1"] = "cardrag.ocr-input.v1"
OCR_OUTPUT_PROFILE: Literal["cardrag.ocr-markdown.v1"] = "cardrag.ocr-markdown.v1"

_PAGE_MARKER = re.compile(r"^## Page ([1-9][0-9]*)$", re.MULTILINE)
_REASONING = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
ReasoningEffort = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,31}$")]


class NativeOCRContract(StrictFrozenModel):
    """Every semantic input that may change native OCR output."""

    schema_version: Literal["cardrag.ocr-contract.v1", "cardrag.ocr-contract.v2"] = OCR_CONTRACT_SCHEMA
    processor_version: NonEmptyText
    output_profile: Literal["cardrag.ocr-markdown.v1"] = OCR_OUTPUT_PROFILE
    cache_epoch: NonNegativeInt = 0
    prompt_version: NonEmptyText
    prompt_sha256: Sha256Hex
    renderer_id: NonEmptyText
    render_scale_milli: int = Field(strict=True, ge=1000, le=8000)
    provider: NonEmptyText
    model: NonEmptyText
    reasoning_effort: ReasoningEffort | None = None
    # ``chunk_pages`` is retained only to parse and hash v1 manifests exactly.
    # New producers use the explicit continuity-aware segmentation fields.
    chunk_pages: int | None = Field(default=None, strict=True, ge=1, le=100)
    segmentation_strategy_id: NonEmptyText | None = None
    whole_document_max_pages: int | None = Field(default=None, strict=True, ge=1, le=100)
    target_pages_per_call: int | None = Field(default=None, strict=True, ge=1, le=100)
    context_pages_before: int | None = Field(default=None, strict=True, ge=0, le=20)
    context_pages_after: int | None = Field(default=None, strict=True, ge=0, le=20)
    output_policy: Literal["target-pages-only"] | None = None

    @model_validator(mode="after")
    def segmentation_contract_matches_schema(self) -> Self:
        continuity_fields = (
            self.segmentation_strategy_id,
            self.whole_document_max_pages,
            self.target_pages_per_call,
            self.context_pages_before,
            self.context_pages_after,
            self.output_policy,
        )
        if self.schema_version == LEGACY_OCR_CONTRACT_SCHEMA:
            if self.chunk_pages is None:
                raise ValueError("v1 OCR contract requires chunk_pages")
            if any(value is not None for value in continuity_fields):
                raise ValueError("v1 OCR contract cannot contain v2 segmentation fields")
            return self
        if self.chunk_pages is not None:
            raise ValueError("v2 OCR contract must use target_pages_per_call, not chunk_pages")
        if any(value is None for value in continuity_fields):
            raise ValueError("v2 OCR contract requires the complete segmentation contract")
        return self

    @model_serializer(mode="wrap")
    def serialize_by_schema(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        """Keep historical v1 JSON byte-compatible while emitting all v2 inputs."""

        payload = cast(dict[str, Any], handler(self))
        if self.schema_version == LEGACY_OCR_CONTRACT_SCHEMA:
            for field_name in (
                "segmentation_strategy_id",
                "whole_document_max_pages",
                "target_pages_per_call",
                "context_pages_before",
                "context_pages_after",
                "output_policy",
            ):
                payload.pop(field_name, None)
        else:
            payload.pop("chunk_pages", None)
        return payload

    @property
    def contract_sha256(self) -> str:
        return canonical_sha256(self)


class OCRInput(StrictFrozenModel):
    """Content identity known before invoking an OCR provider."""

    schema_version: Literal["cardrag.ocr-input.v1"] = OCR_INPUT_SCHEMA
    pdf_sha256: Sha256Hex
    pdf_size_bytes: PositiveInt
    page_count: PositiveInt


def native_ocr_reuse_key(contract: NativeOCRContract, source: OCRInput) -> str:
    """Return the lookup key for one PDF under one exact native OCR contract."""

    return canonical_sha256(
        {
            "contract_sha256": contract.contract_sha256,
            "input": source,
            "schema_version": "cardrag.ocr-reuse-key.v1",
        }
    )


@dataclass(frozen=True, slots=True)
class VerifiedOCR:
    """Canonical OCR bytes after every structural/hash check has passed."""

    text: str
    sha256: str
    size_bytes: int
    char_count: int
    pages: tuple[str, ...]
    page_sha256: tuple[str, ...]


class OCRVerificationError(ValueError):
    """OCR bytes do not satisfy the immutable artifact contract."""


def _split_pages(text: str, *, expected_page_count: int, minimum_chars_per_page: int) -> tuple[str, ...]:
    markers = list(_PAGE_MARKER.finditer(text))
    marker_numbers = [int(match.group(1)) for match in markers]
    expected = list(range(1, expected_page_count + 1))
    if marker_numbers != expected:
        raise OCRVerificationError(f"OCR page markers {marker_numbers} do not match {expected}")
    pages = tuple(
        text[match.start() : markers[index + 1].start() if index + 1 < len(markers) else len(text)].strip()
        for index, match in enumerate(markers)
    )
    if any(len(page) < minimum_chars_per_page for page in pages):
        raise OCRVerificationError("OCR contains an implausibly short page")
    canonical_text = "\n\n".join(pages) + "\n"
    if text != canonical_text:
        raise OCRVerificationError("OCR bytes are not in canonical page-join form")
    return pages


def verify_ocr_bytes(
    payload: bytes,
    *,
    expected_page_count: int,
    expected_sha256: str | None = None,
    expected_size_bytes: int | None = None,
    expected_char_count: int | None = None,
    expected_page_sha256: tuple[str, ...] | None = None,
    minimum_chars_per_page: int = 20,
) -> VerifiedOCR:
    """Strictly verify UTF-8, canonical pages, coverage, and all supplied hashes."""

    if expected_page_count < 1:
        raise ValueError("expected_page_count must be positive")
    if minimum_chars_per_page < 1:
        raise ValueError("minimum_chars_per_page must be positive")
    if b"\x00" in payload or b"\r" in payload:
        raise OCRVerificationError("OCR bytes contain forbidden NUL or CR characters")
    if expected_size_bytes is not None and len(payload) != expected_size_bytes:
        raise OCRVerificationError("OCR byte size does not match its manifest")
    digest = sha256_bytes(payload)
    if expected_sha256 is not None and digest != expected_sha256:
        raise OCRVerificationError("OCR SHA-256 does not match its manifest")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise OCRVerificationError("OCR object is not valid UTF-8") from exc
    pages = _split_pages(
        text,
        expected_page_count=expected_page_count,
        minimum_chars_per_page=minimum_chars_per_page,
    )
    if expected_char_count is not None and len(text) != expected_char_count:
        raise OCRVerificationError("OCR character count does not match its manifest")
    page_hashes = tuple(sha256_bytes(page.encode("utf-8")) for page in pages)
    if expected_page_sha256 is not None and page_hashes != expected_page_sha256:
        raise OCRVerificationError("OCR page hashes do not match its manifest")
    return VerifiedOCR(
        text=text,
        sha256=digest,
        size_bytes=len(payload),
        char_count=len(text),
        pages=pages,
        page_sha256=page_hashes,
    )


class OCRPageHashes(StrictFrozenModel):
    """Reusable strict wrapper when loading ordered page digests from JSON."""

    page_sha256: tuple[Sha256Hex, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def hashes_are_unique_by_position_not_value(self) -> Self:
        # Duplicate page content is valid. The validator intentionally only
        # ensures a non-empty, type-safe ordered tuple through the field above.
        return self
