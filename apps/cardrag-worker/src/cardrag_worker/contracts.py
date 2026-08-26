"""Small immutable contracts at the worker's privileged boundaries."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, Literal, Protocol

import httpx
from cardrag_core import issuer_code

SERVING_SCHEMA_ID = "cardrag.serving-db.v2"
GENERATION_SCHEMA_ID = "cardrag.generation.v2"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_ID = re.compile(r"^source_[0-9a-f]{64}$")


def utc_now() -> datetime:
    return datetime.now(UTC)


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


@dataclass(frozen=True, slots=True)
class ProtectedSourceAllowance:
    source_id: str
    product_code: str
    source_version: str
    source_url: str
    sha256: str
    size_bytes: int
    magic: Literal["SCDSA002", "SCDSA004"]

    def __post_init__(self) -> None:
        if (
            not _SOURCE_ID.fullmatch(self.source_id)
            or not self.product_code
            or self.product_code != self.product_code.strip()
            or not self.source_version
            or not self.source_url.startswith("https://")
            or not _SHA256.fullmatch(self.sha256)
            or self.size_bytes < 1
        ):
            raise ValueError("protected source allowance requires an exact source and byte identity")

    @property
    def contract_payload(self) -> dict[str, Any]:
        return {
            "magic": self.magic,
            "product_code": self.product_code,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "source_id": self.source_id,
            "source_url": self.source_url,
            "source_version": self.source_version,
        }


@dataclass(frozen=True, slots=True)
class IssuerSpec:
    code: str
    display_name: str
    sort_order: int
    allowed_hosts: frozenset[str]
    categories: tuple[str, ...]
    minimum_records: int = 1
    minimum_interval_seconds: float = 0.25
    retry_base_seconds: float = 1.0
    maximum_retries: int = 4
    minimum_retention_ratio: float = 0.5
    protected_source_allowances: tuple[ProtectedSourceAllowance, ...] = ()

    def __post_init__(self) -> None:
        issuer_code(self.code)
        if (
            not self.allowed_hosts
            or self.minimum_records < 1
            or self.maximum_retries < 1
            or not 0 < self.minimum_retention_ratio <= 1
            or not math.isfinite(self.minimum_interval_seconds)
            or self.minimum_interval_seconds < 0
            or not math.isfinite(self.retry_base_seconds)
            or self.retry_base_seconds <= 0
            or len({item.source_id for item in self.protected_source_allowances})
            != len(self.protected_source_allowances)
        ):
            raise ValueError("issuer spec requires a code, hosts, and a positive minimum")


@dataclass(frozen=True, slots=True)
class SourceRecord:
    issuer: str
    product_code: str
    product_name: str
    effective_date: date
    source_version: str
    source_url: str
    source_post_id: str
    file_name: str
    category: str
    discovered_at: datetime
    metadata: Mapping[str, Any] = field(default_factory=dict)
    document_type: str = "product_description"

    def __post_init__(self) -> None:
        if not all((self.issuer, self.product_code, self.product_name, self.source_version, self.source_url)):
            raise ValueError("source record contains an empty identity field")
        if not self.file_name.casefold().endswith(".pdf"):
            raise ValueError("source record file_name must end in .pdf")
        if self.discovered_at.tzinfo is None:
            raise ValueError("discovered_at must be timezone-aware")

    @property
    def discovery_payload(self) -> dict[str, Any]:
        """Stable payload: observation time is deliberately excluded."""

        return {
            "category": self.category,
            "document_type": self.document_type,
            "effective_date": self.effective_date.isoformat(),
            "file_name": self.file_name,
            "issuer": self.issuer,
            "metadata": dict(self.metadata),
            "product_code": self.product_code,
            "product_name": self.product_name,
            "source_post_id": self.source_post_id,
            "source_url": self.source_url,
            "source_version": self.source_version,
        }

    @property
    def source_id(self) -> str:
        return "source_" + canonical_sha256(self.discovery_payload)

    def document_id(self, pdf_sha256: str) -> str:
        if not _SHA256.fullmatch(pdf_sha256):
            raise ValueError("invalid PDF sha256")
        return "doc_" + canonical_sha256(
            {
                "document_type": self.document_type,
                "effective_date": self.effective_date.isoformat(),
                "issuer": self.issuer,
                "pdf_sha256": pdf_sha256,
                "product_code": self.product_code,
                "version": self.source_version,
            }
        )


@dataclass(frozen=True, slots=True)
class UnsupportedProductRecord:
    source: SourceRecord
    protected_sha256: str
    protected_size_bytes: int
    protected_magic: Literal["SCDSA002", "SCDSA004"]
    disposition: Literal["unsupported_drm"] = "unsupported_drm"

    def __post_init__(self) -> None:
        if not _SHA256.fullmatch(self.protected_sha256) or self.protected_size_bytes < 1:
            raise ValueError("unsupported product requires an exact protected byte identity")

    @property
    def payload(self) -> dict[str, Any]:
        return {
            "disposition": self.disposition,
            "protected_magic": self.protected_magic,
            "protected_sha256": self.protected_sha256,
            "protected_size_bytes": self.protected_size_bytes,
            "source": self.source.discovery_payload,
            "source_id": self.source.source_id,
        }

    @property
    def source_payload_json(self) -> str:
        return canonical_json_bytes(self.source.discovery_payload).decode("utf-8")


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    issuer: str
    source_url: str
    parser_version: str
    records: tuple[SourceRecord, ...]
    started_at: datetime
    finished_at: datetime
    warnings: tuple[str, ...] = ()

    @property
    def payload(self) -> dict[str, Any]:
        ordered = sorted((row.discovery_payload for row in self.records), key=canonical_json_bytes)
        return {
            "contract_version": "cardrag.source-snapshot.v1",
            "issuer": self.issuer,
            "parser_version": self.parser_version,
            "records": ordered,
            "source_url": self.source_url,
        }

    @property
    def snapshot_id(self) -> str:
        return canonical_sha256(self.payload)


@dataclass(frozen=True, slots=True)
class DownloadRequest:
    url: str
    method: Literal["GET", "POST"] = "GET"
    form: Mapping[str, str] | None = None
    headers: Mapping[str, str] = field(default_factory=dict)


class IssuerAdapter(Protocol):
    spec: IssuerSpec
    parser_version: str

    async def discover_current(self, client: httpx.AsyncClient) -> SourceSnapshot: ...

    async def prepare_download(self, client: httpx.AsyncClient, source: SourceRecord) -> DownloadRequest: ...


@dataclass(frozen=True, slots=True)
class PageRecord:
    document_id: str
    page: int
    text: str

    @property
    def text_sha256(self) -> str:
        return sha256_bytes(self.text.encode("utf-8"))


@dataclass(frozen=True, slots=True)
class DocumentRecord:
    document_id: str
    issuer: str
    product_code: str
    product_name: str
    title: str
    pdf_sha256: str
    pdf_size_bytes: int
    page_count: int
    pages: tuple[PageRecord, ...]


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    evidence_id: str
    document_id: str
    page_start: int
    page_end: int
    section_type: str
    text: str
    source_start: int
    source_end: int
    embedding: Sequence[float]


def snapshot_from_records(
    *,
    issuer: str,
    source_url: str,
    parser_version: str,
    records: Sequence[SourceRecord],
    started_at: datetime,
    warnings: Sequence[str] = (),
) -> SourceSnapshot:
    unique: dict[tuple[str, str, date, str, str], SourceRecord] = {}
    for row in records:
        key = (
            row.product_code,
            row.document_type,
            row.effective_date,
            row.source_version,
            row.source_post_id,
        )
        unique[key] = row
    ordered = tuple(
        sorted(
            unique.values(),
            key=lambda row: (row.product_code, row.effective_date, row.source_version, row.source_post_id),
        )
    )
    return SourceSnapshot(
        issuer=issuer,
        source_url=source_url,
        parser_version=parser_version,
        records=ordered,
        started_at=started_at,
        finished_at=utc_now(),
        warnings=tuple(warnings),
    )
