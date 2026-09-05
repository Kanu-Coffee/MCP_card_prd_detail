"""One finite latest-only pipeline from discovery to an immutable serving bundle."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import re
import shutil
import sqlite3
import stat
import struct
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, TypeVar, cast

import httpx
from cardrag_core import (
    DOCUMENT_EMBEDDING_PREFIX,
    EMBEDDING_DIMENSION,
    EMBEDDING_POLICY_VERSION,
    EMBEDDING_VIEW_TYPES,
    QUERY_EMBEDDING_PREFIX,
    QWEN3_DOCUMENT_POLICY,
    ArtifactRef,
    EmbeddingContract,
    EmbeddingProfile,
    EmbeddingVectorSidecar,
    EmbeddingViewCount,
    GenerationCounts,
    GenerationDocument,
    GenerationManifest,
    GenerationOCRFailure,
    IssuerOCRCounts,
    OCRCacheKind,
    StructureContract,
    StructureMajorClassCounts,
    StructureNodeCounts,
    StructureRevisionCounts,
    StructureSourceCoverage,
    WebDAVError,
    WebDAVHTTPError,
    WebDAVIntegrityError,
    canonical_json_bytes,
    canonical_sha256,
    generation_database_path,
    generation_manifest_path,
    generation_vectors_path,
    object_path,
    sealed_v5_retrieval_policy,
    sha256_bytes,
    sha256_file,
    validate_identifier,
)
from cardrag_core import (
    IssuerParserProfile as ManifestIssuerParserProfile,
)

from .aggregation_profile_v5 import VerifiedAggregationProfileV5
from .async_utils import to_thread_fenced
from .cache_seed_v109 import load_v109_seed_pins
from .capacity_v5 import (
    V5CapacityError,
    V5CapacityPolicy,
    build_v5_database_ledger,
    predict_serving_database_bytes,
    predict_v5_local_artifacts,
    preflight_v5_capacity,
    preflight_v5_remaining_free_capacity,
)
from .contracts import (
    GENERATION_SCHEMA_ID,
    SERVING_SCHEMA_ID,
    DocumentRecord,
    EvidenceRecord,
    IssuerAdapter,
    OCRFailedProductRecord,
    PageRecord,
    ProtectedSourceAllowance,
    SourceRecord,
    SourceSnapshot,
    UnsupportedProductRecord,
)
from .downloader import (
    DownloadedPDF,
    DownloadPolicy,
    PDFNotModified,
    ProtectedDocumentError,
    SecurePDFDownloader,
)
from .embedding_v5 import (
    EmbeddingV5Error,
    EmbeddingV5PermanentRequestError,
    EmbeddingV5RequestError,
    EmbeddingV5TransientError,
    OpenRouterQwenEmbeddingProviderV5,
    QwenEmbeddingProfileV5,
    embedding_cache_key,
    format_embedding_input,
)
from .exporter import ServingDatabaseExporter, encode_embedding
from .exporter_v5 import (
    ContractRevisionInput,
    DocumentPageInput,
    EmbeddingProfileInput,
    EmbeddingViewInput,
    IssuerInput,
    LazyEmbeddingVector,
    NodeLinkInput,
    NodeSpanInput,
    OCRFailedProductInput,
    ProductLineageInput,
    ServingDatabaseExporterV5,
    StructureNodeInput,
    UnsupportedProductInput,
    ViewSourceSpanInput,
)
from .ocr import (
    OCRCachePublicationError,
    OCRResolver,
    OCRResult,
    OCRValidationError,
    PriorLocalNativeSource,
    page_records,
)
from .pdf_cache import PDFCache, PDFCachePruneError, PDFSourceIdentity
from .providers import (
    EmbeddingProvider,
    ProviderDocumentError,
    ProviderError,
    ProviderSystemicError,
)
from .rate_limit import IssuerRateLimiter, RateLimitedClient
from .revision_history_v5 import (
    REVISION_HISTORY_POLICY_VERSION,
    UNRESOLVED_REVISION_LEDGER_SCHEMA,
    TemporalStatusV5,
    UnresolvedRevisionIdentityV5,
    UnresolvedRevisionLedgerEntryV5,
    UnresolvedRevisionReasonV5,
    canonical_unresolved_revision_ledger_v5,
    plan_revision_history_v5,
    unresolved_revision_ledger_sha256_v5,
)
from .state import WorkerState, WorkerStateWALCapacityError, retry_delay, worker_lock
from .structure import (
    DerivedView,
    StructureArtifact,
    build_derived_views,
    build_unclassified_fallback_artifact,
    contextual_item_policy_payload,
    issuer_parser_profile,
    parse_structure_artifact,
    unclassified_fallback_policy_payload,
    validate_structure_artifact,
)
from .tokenizer_v5 import QWEN_TOKENIZER_REVISION, QWEN_TOKENIZER_SHA256
from .webdav import PublishedBundle, WebDAVBundlePublisher, WebDAVClient

T = TypeVar("T")
CHUNK_CONTRACT = "cardrag.page-window.v1"
GENERATION_SCHEMA_ID_V5 = "cardrag.generation.v5"
SERVING_SCHEMA_ID_V5 = "cardrag.serving-db.v5"
V5_VIEW_MAXIMUM_CHARACTERS = 131_072
MAX_GENERATION_MANIFEST_BYTES = 32 * 1024 * 1024
MAX_WORKER_SEAL_BYTES = 64 * 1024 * 1024
STRUCTURE_FALLBACK_LEDGER_SCHEMA = "cardrag.structure-fallback-ledger.v1"
STRUCTURE_FAILED_LEDGER_SCHEMA = "cardrag.structure-failed-ledger.v1"
V5_RETRIEVAL_POLICY = {
    "aggregation": "max-child.v1",
    "candidate_prefilter": "none",
    "dense_scan": "exact-all-active-rows.v1",
    "lexical_fusion": "forbidden",
    "schema_version": "cardrag.retrieval-policy.v1",
    "temporal_scope": "current",
}
LOGGER = logging.getLogger(__name__)
LOCAL_RUN_CLEANUP_ERROR = (
    "local_run_cleanup_failed: Local diagnostic run cleanup failed after bounded retention."
)
PDF_CACHE_PRUNE_ERROR = "pdf_cache_prune_failed: Local PDF cache pruning failed after durable run completion."
REMOTE_GC_ERROR = "remote_gc_failed: Remote garbage collection failed after durable run completion."
REMOTE_GC_PARTIAL_ERROR = (
    "remote_gc_partial_failure: Remote garbage collection stopped after partial deletion."
)


class CorpusConflictError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class OCRFailureReason:
    reason_code: str
    reason: str

    @property
    def stored_error(self) -> str:
        return f"{self.reason_code}: {self.reason}"


@dataclass(frozen=True, slots=True)
class OCRFailureRecord:
    issuer: str
    product_code: str
    product_name: str
    file_name: str
    document_id: str
    pdf_sha256: str
    page_count: int
    attempts: int
    reason_code: str
    reason: str

    def __post_init__(self) -> None:
        limits = {
            "issuer": 64,
            "product_code": 256,
            "product_name": 512,
            "file_name": 512,
            "reason": 256,
        }
        for field_name, maximum in limits.items():
            value = str(getattr(self, field_name))
            if not value or len(value) > maximum:
                raise ValueError(f"OCR failure {field_name} is empty or too long")
        if not re.fullmatch(r"doc_[0-9a-f]{64}", self.document_id):
            raise ValueError("OCR failure document_id is invalid")
        if not re.fullmatch(r"[0-9a-f]{64}", self.pdf_sha256):
            raise ValueError("OCR failure PDF sha256 is invalid")
        if not re.fullmatch(r"[a-z0-9_]{1,64}", self.reason_code):
            raise ValueError("OCR failure reason_code is invalid")
        if "\n" in self.reason or "\r" in self.reason:
            raise ValueError("OCR failure reason must be one line")
        if self.page_count < 1 or self.attempts < 1:
            raise ValueError("OCR failure counts must be positive")

    @property
    def payload(self) -> dict[str, Any]:
        return {
            "attempts": self.attempts,
            "document_id": self.document_id,
            "file_name": self.file_name,
            "issuer": self.issuer,
            "page_count": self.page_count,
            "pdf_sha256": self.pdf_sha256,
            "product_code": self.product_code,
            "product_name": self.product_name,
            "reason": self.reason,
            "reason_code": self.reason_code,
        }


class OCRDocumentFailuresError(RuntimeError):
    """A safe aggregate raised only after every acquired PDF was attempted."""

    def __init__(
        self,
        *,
        run_id: str,
        report_path: Path,
        failures: Sequence[OCRFailureRecord],
    ) -> None:
        self.run_id = run_id
        self.report_path = report_path
        self.report = f"runs/{run_id}/reports/ocr-failures.json"
        self.failures = tuple(failures)
        if not self.failures:
            raise ValueError("OCR document failure aggregate cannot be empty")
        super().__init__(f"{len(self.failures)} OCR document(s) failed; report={self.report}")


StructureFailureStage = Literal["parser", "derived_views"]
StructureFallbackReasonCode = Literal["parser_failed", "derived_view_failed"]


@dataclass(frozen=True, slots=True)
class StructureFailureRecord:
    """One canonical, secret-free disposition after even lossless fallback failed."""

    issuer: str
    product_code: str
    document_id: str
    source_id: str
    pdf_sha256: str
    ocr_sha256: str
    source_pages_sha256: str
    page_count: int
    failure_stage: StructureFailureStage

    def __post_init__(self) -> None:
        for field_name, maximum in (("issuer", 64), ("product_code", 256)):
            value = getattr(self, field_name)
            if (
                not value
                or value != value.strip()
                or len(value) > maximum
                or any(ord(character) < 32 or ord(character) == 127 for character in value)
            ):
                raise ValueError(f"structure failure {field_name} is invalid")
        if not re.fullmatch(r"doc_[0-9a-f]{64}", self.document_id):
            raise ValueError("structure failure document_id is invalid")
        if not re.fullmatch(r"source_[0-9a-f]{64}", self.source_id):
            raise ValueError("structure failure source_id is invalid")
        for field_name in ("pdf_sha256", "ocr_sha256", "source_pages_sha256"):
            if not re.fullmatch(r"[0-9a-f]{64}", getattr(self, field_name)):
                raise ValueError(f"structure failure {field_name} is invalid")
        if self.page_count < 1:
            raise ValueError("structure failure page_count must be positive")
        if self.failure_stage not in {"parser", "derived_views"}:
            raise ValueError("structure failure stage is invalid")

    @property
    def payload(self) -> dict[str, Any]:
        return {
            "disposition": "structure_failed",
            "document_id": self.document_id,
            "failure_code": "structure_fallback_failed",
            "failure_stage": self.failure_stage,
            "issuer": self.issuer,
            "ocr_sha256": self.ocr_sha256,
            "page_count": self.page_count,
            "pdf_sha256": self.pdf_sha256,
            "product_code": self.product_code,
            "source_id": self.source_id,
            "source_pages_sha256": self.source_pages_sha256,
        }


def _canonical_structure_failure_ledger(
    failures: Sequence[StructureFailureRecord],
) -> dict[str, Any]:
    ordered = sorted(
        failures,
        key=lambda item: (item.issuer, item.product_code, item.document_id),
    )
    return {
        "documents": [failure.payload for failure in ordered],
        "failure_code": "structure_failed",
        "schema_version": STRUCTURE_FAILED_LEDGER_SCHEMA,
    }


def _structure_failure_ledger_sha256(failures: Sequence[StructureFailureRecord]) -> str:
    return canonical_sha256(_canonical_structure_failure_ledger(failures))


def _canonical_structure_fallback_ledger(
    documents: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    ordered = sorted(
        (dict(document) for document in documents),
        key=lambda item: str(item.get("document_id", "")),
    )
    return {
        "documents": ordered,
        "policy_version": str(unclassified_fallback_policy_payload()["schema_version"]),
        "schema_version": STRUCTURE_FALLBACK_LEDGER_SCHEMA,
    }


def _structure_fallback_ledger_sha256(documents: Sequence[Mapping[str, Any]]) -> str:
    return canonical_sha256(_canonical_structure_fallback_ledger(documents))


class StructureDocumentFailuresError(RuntimeError):
    """A final publication gate for documents with no searchable fallback."""

    def __init__(
        self,
        *,
        run_id: str,
        report_path: Path,
        failures: Sequence[StructureFailureRecord],
    ) -> None:
        self.run_id = run_id
        self.report_path = report_path
        self.report = f"runs/{run_id}/reports/structure-failures.json"
        self.failures = tuple(failures)
        if not self.failures:
            raise ValueError("structure document failure aggregate cannot be empty")
        self.ledger_sha256 = _structure_failure_ledger_sha256(self.failures)
        self.stored_error = (
            "structure_failed: "
            f"count={len(self.failures)}; ledger_sha256={self.ledger_sha256}; "
            f"report={self.report}"
        )
        super().__init__(self.stored_error)


class _StructureFallbackFailed(RuntimeError):
    """Internal bounded signal; the original parser/provider error is discarded."""

    def __init__(self, failure_stage: StructureFailureStage) -> None:
        self.failure_stage = failure_stage
        super().__init__("structure_fallback_failed")


@dataclass(frozen=True, slots=True)
class OCRSystemicFailureRecord:
    """One bounded, secret-safe incident at the OCR system boundary."""

    run_id: str
    document_id: str
    source_id: str
    issuer: str
    product_code: str
    pdf_sha256: str
    attempt: int
    occurred_at: datetime
    reason_code: str
    reason: str
    error_class_category: str
    phase: str | None = None
    status_code: int | None = None
    error_kind: str | None = None
    retryable: bool | None = None
    publication_attempts: int | None = None
    exit_code: int | None = None
    stderr_size_bytes: int | None = None
    stderr_sha256: str | None = None

    def __post_init__(self) -> None:
        bounded_identity = {
            "run_id": (self.run_id, 128),
            "issuer": (self.issuer, 64),
            "product_code": (self.product_code, 256),
        }
        for field_name, (value, maximum) in bounded_identity.items():
            if (
                not value
                or len(value) > maximum
                or any(ord(character) < 32 or ord(character) == 127 for character in value)
            ):
                raise ValueError(f"OCR systemic failure {field_name} is invalid")
        if not re.fullmatch(r"doc_[0-9a-f]{64}", self.document_id):
            raise ValueError("OCR systemic failure document_id is invalid")
        if not re.fullmatch(r"source_[0-9a-f]{64}", self.source_id):
            raise ValueError("OCR systemic failure source_id is invalid")
        if not re.fullmatch(r"[0-9a-f]{64}", self.pdf_sha256):
            raise ValueError("OCR systemic failure PDF sha256 is invalid")
        if self.attempt < 1:
            raise ValueError("OCR systemic failure attempt must be positive")
        if self.occurred_at.tzinfo is None:
            raise ValueError("OCR systemic failure timestamp must be timezone-aware")
        if not re.fullmatch(r"[a-z0-9_]{1,64}", self.reason_code):
            raise ValueError("OCR systemic failure reason_code is invalid")
        if (
            not self.reason
            or len(self.reason) > 256
            or any(ord(character) < 32 or ord(character) == 127 for character in self.reason)
        ):
            raise ValueError("OCR systemic failure reason is invalid")
        if not re.fullmatch(r"[a-z0-9_]{1,64}", self.error_class_category):
            raise ValueError("OCR systemic failure error class category is invalid")
        if self.phase is not None and not re.fullmatch(r"[a-z0-9_]{1,32}", self.phase):
            raise ValueError("OCR systemic failure phase is invalid")
        if self.error_kind is not None and not re.fullmatch(r"[a-z0-9_]{1,32}", self.error_kind):
            raise ValueError("OCR systemic failure error_kind is invalid")
        if self.status_code is not None and not 100 <= self.status_code <= 599:
            raise ValueError("OCR systemic failure status_code is invalid")
        if self.retryable is not None and not isinstance(self.retryable, bool):
            raise ValueError("OCR systemic failure retryable is invalid")
        if self.publication_attempts is not None and not 1 <= self.publication_attempts <= 64:
            raise ValueError("OCR systemic failure publication_attempts is invalid")
        if self.exit_code is not None and (
            isinstance(self.exit_code, bool) or not -255 <= self.exit_code <= 255 or self.exit_code == 0
        ):
            raise ValueError("OCR systemic failure exit_code is invalid")
        if (self.stderr_size_bytes is None) != (self.stderr_sha256 is None):
            raise ValueError("OCR systemic failure stderr diagnostics must be complete")
        if self.stderr_size_bytes is not None and (
            isinstance(self.stderr_size_bytes, bool)
            or not isinstance(self.stderr_size_bytes, int)
            or not 0 <= self.stderr_size_bytes <= 2**63 - 1
            or self.stderr_sha256 is None
            or re.fullmatch(r"[0-9a-f]{64}", self.stderr_sha256) is None
        ):
            raise ValueError("OCR systemic failure stderr diagnostics are invalid")

    @property
    def payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "attempt": self.attempt,
            "document_id": self.document_id,
            "error_class_category": self.error_class_category,
            "issuer": self.issuer,
            "occurred_at": self.occurred_at.astimezone(UTC).isoformat(),
            "pdf_sha256": self.pdf_sha256,
            "product_code": self.product_code,
            "reason": self.reason,
            "reason_code": self.reason_code,
            "run_id": self.run_id,
            "source_id": self.source_id,
        }
        if self.phase is not None:
            payload["phase"] = self.phase
        if self.status_code is not None:
            payload["status_code"] = self.status_code
        if self.error_kind is not None:
            payload["error_kind"] = self.error_kind
        if self.retryable is not None:
            payload["retryable"] = self.retryable
        if self.publication_attempts is not None:
            payload["publication_attempts"] = self.publication_attempts
        if self.exit_code is not None:
            payload["exit_code"] = self.exit_code
        if self.stderr_size_bytes is not None:
            payload["stderr_size_bytes"] = self.stderr_size_bytes
            payload["stderr_sha256"] = self.stderr_sha256
        return payload


class OCRSystemicFailureError(RuntimeError):
    """A safe terminal error backed by a durable operator incident report."""

    def __init__(
        self,
        *,
        run_id: str,
        report_path: Path,
        failure: OCRSystemicFailureRecord,
    ) -> None:
        self.run_id = run_id
        self.report_path = report_path
        self.report = f"runs/{run_id}/reports/ocr-systemic-failure.json"
        self.failure = failure
        self.stored_error = f"{failure.reason_code}: {failure.reason}; report={self.report}"
        super().__init__(self.stored_error)


class OCRFailureBookkeepingError(RuntimeError):
    """A safe terminal error for failure-isolation state corruption."""

    def __init__(self) -> None:
        super().__init__("OCR failure isolation bookkeeping failed")


class OCRCacheHealingIdentityError(RuntimeError):
    """A remote/provider OCR result differs from the retained generation OCR."""

    def __init__(self) -> None:
        super().__init__("OCR cache healing result differs from its retained generation seal")


@dataclass(frozen=True, slots=True)
class WorkerUnexpectedFailureRecord:
    """One bounded diagnostic for a failure outside a typed pipeline boundary."""

    run_id: str
    occurred_at: datetime
    error_class_category: str
    phase: str | None = None
    status_code: int | None = None
    errno: int | None = None
    reason_code: str = "worker_unexpected_failure"
    reason: str = "Worker pipeline failed unexpectedly."

    def __post_init__(self) -> None:
        if (
            not self.run_id
            or len(self.run_id) > 128
            or any(ord(character) < 32 or ord(character) == 127 for character in self.run_id)
        ):
            raise ValueError("Worker failure run_id is invalid")
        if self.occurred_at.tzinfo is None:
            raise ValueError("Worker failure timestamp must be timezone-aware")
        if not re.fullmatch(r"[a-z0-9_]{1,64}", self.error_class_category):
            raise ValueError("Worker failure error class category is invalid")
        if self.phase is not None and not re.fullmatch(r"[a-z0-9_]{1,64}", self.phase):
            raise ValueError("Worker failure phase is invalid")
        if self.status_code is not None and not 100 <= self.status_code <= 599:
            raise ValueError("Worker failure status_code is invalid")
        if self.errno is not None and (isinstance(self.errno, bool) or not 1 <= self.errno <= 4095):
            raise ValueError("Worker failure errno is invalid")
        if self.reason_code != "worker_unexpected_failure":
            raise ValueError("Worker failure reason_code is invalid")
        if self.reason != "Worker pipeline failed unexpectedly.":
            raise ValueError("Worker failure reason is invalid")

    @property
    def payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "error_class_category": self.error_class_category,
            "occurred_at": self.occurred_at.astimezone(UTC).isoformat(),
            "reason": self.reason,
            "reason_code": self.reason_code,
            "run_id": self.run_id,
        }
        if self.status_code is not None:
            payload["status_code"] = self.status_code
        if self.errno is not None:
            payload["errno"] = self.errno
        if self.phase is not None:
            payload["phase"] = self.phase
        return payload


class WorkerUnexpectedFailureError(RuntimeError):
    """A secret-safe terminal error for an otherwise untyped pipeline failure."""

    def __init__(
        self,
        *,
        run_id: str,
        report_path: Path,
        failure: WorkerUnexpectedFailureRecord,
    ) -> None:
        self.run_id = run_id
        self.report_path = report_path
        self.report = f"runs/{run_id}/reports/worker-failure.json"
        self.failure = failure
        self.stored_error = f"{failure.reason_code}: {failure.reason}; report={self.report}"
        super().__init__(self.stored_error)


@dataclass(frozen=True, slots=True)
class PipelineResult:
    run_id: str
    status: str
    corpus_sha256: str
    contract_sha256: str
    generation_id: str | None
    document_count: int
    evidence_count: int
    unsupported_document_count: int = 0
    gc_status: str | None = None
    gc_deleted: int = 0
    gc_error: str | None = None
    pdf_cache_hits: int = 0
    pdf_cache_misses: int = 0
    pdf_downloads: int = 0
    pdf_revisions: int = 0
    pdf_cache_revalidations: int = 0
    pdf_cache_not_modified: int = 0
    pdf_cache_prune_status: str | None = None
    pdf_cache_pruned_objects: int = 0
    pdf_cache_pruned_bytes: int = 0
    pdf_cache_prune_error: str | None = None
    ocr_cache_publication_deferred: int = 0
    v5_metrics: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class _ProcessedDocument:
    source: SourceRecord
    record: DocumentRecord
    pdf_path: Path
    ocr_path: Path
    ocr_sha256: str
    ocr_size_bytes: int
    ocr_cache_kind: OCRCacheKind | None
    ocr_reuse_key: str | None
    chunks: tuple[dict[str, Any], ...]
    temporal_status: TemporalStatusV5 = "current"
    supersedes_document_id: str | None = None
    is_historical: bool = False
    structure_artifact: StructureArtifact | None = None
    embedding_views: tuple[DerivedView, ...] = ()
    structure_fallback_reason_code: StructureFallbackReasonCode | None = None


@dataclass(frozen=True, slots=True)
class _AcquiredDocument:
    source: SourceRecord
    pdf: DownloadedPDF
    temporal_status: TemporalStatusV5 = "current"
    supersedes_document_id: str | None = None
    is_historical: bool = False


def _v5_corpus_identity_payload(
    *,
    acquired: Sequence[_AcquiredDocument],
    unsupported_documents: Sequence[Mapping[str, Any]],
    unresolved_revisions: Sequence[UnresolvedRevisionLedgerEntryV5],
    unresolved_revision_sha256: str,
) -> dict[str, Any]:
    """Bind every materialized and unresolved revision truth before no-change."""

    if unresolved_revision_sha256 != unresolved_revision_ledger_sha256_v5(unresolved_revisions):
        raise RuntimeError("v5 corpus unresolved revision ledger hash mismatch")
    return {
        "schema_version": "cardrag.current-corpus.v3",
        "documents": [
            {
                "source": item.source.discovery_payload,
                "pdf_sha256": item.pdf.sha256,
                "pdf_size_bytes": item.pdf.size_bytes,
                "page_count": item.pdf.page_count,
            }
            for item in acquired
        ],
        "unsupported_documents": list(unsupported_documents),
        "revision_history": {
            "policy_version": REVISION_HISTORY_POLICY_VERSION,
            "materialized_revisions": [
                {
                    "document_id": item.source.document_id(item.pdf.sha256),
                    "source_id": item.source.source_id,
                    "pdf_sha256": item.pdf.sha256,
                    "temporal_status": item.temporal_status,
                    "supersedes_document_id": item.supersedes_document_id,
                }
                for item in acquired
            ],
            "unresolved_ledger_schema": UNRESOLVED_REVISION_LEDGER_SCHEMA,
            "unresolved_revision_count": len(unresolved_revisions),
            "unresolved_revision_sha256": unresolved_revision_sha256,
            "unresolved_revisions": list(unresolved_revisions),
        },
    }


@dataclass(frozen=True, slots=True)
class _OCRFailedDocument:
    record: OCRFailedProductRecord
    pdf_path: Path
    is_historical: bool = False


@dataclass(frozen=True, slots=True)
class _ValidatedSeal:
    manifest: GenerationManifest
    database_path: Path
    vector_path: Path | None
    objects: tuple[tuple[Path, str, str, int], ...]
    ocr_cache_publication_deferred: int
    v5_metrics: Mapping[str, Any] | None
    seal_sha256: str


def _atomic_write(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def _validated_v5_metrics(
    raw: object,
    *,
    manifest: GenerationManifest,
) -> dict[str, Any]:
    """Validate additive Worker evidence against its immutable v5 manifest."""

    if not isinstance(raw, Mapping):
        raise RuntimeError("sealed v5 Worker metrics are not an object")
    required_keys = {
        "schema_version",
        "parser_profile_document_counts",
        "node_type_counts",
        "major_class_counts",
        "unknown_unclassified_count",
        "unknown_unclassified_denominator",
        "unknown_unclassified_ratio",
        "source_non_whitespace_count",
        "covered_non_whitespace_count",
        "source_coverage_percent",
        "cross_page_continuation_count",
        "table_node_count",
        "footnote_node_count",
        "contract_revision_count",
        "current_revision_count",
        "superseded_revision_count",
        "ambiguous_revision_count",
        "revision_history_policy_version",
        "historical_revision_unresolved_count",
        "historical_revision_unresolved_identities",
        "historical_revision_unresolved_sha256",
        "historical_pdf_cache_hits",
        "structure_fallback_policy_version",
        "structure_fallback_document_count",
        "structure_fallback_documents",
        "structure_fallback_documents_sha256",
        "structure_failed_document_count",
        "structure_failed_documents_sha256",
        "embedding_view_counts",
        "embedding_provider_call_count",
        "embedding_provider",
        "embedding_model",
        "embedding_dimension",
        "embedding_profile_id",
        "vector_sidecar_size_bytes",
        "ocr_cache_reused_count",
        "ocr_provider_called_count",
    }
    if set(raw) != required_keys or raw.get("schema_version") != "cardrag.worker-v5-metrics.v3":
        raise RuntimeError("sealed v5 Worker metrics have an unknown contract")

    def count(name: str) -> int:
        value = raw.get(name)
        if type(value) is not int or value < 0:
            raise RuntimeError(f"sealed v5 Worker metric {name} is invalid")
        return value

    def count_map(name: str) -> dict[str, int]:
        value = raw.get(name)
        if not isinstance(value, Mapping) or not value:
            raise RuntimeError(f"sealed v5 Worker metric {name} is invalid")
        parsed: dict[str, int] = {}
        for key, raw_count in value.items():
            if not isinstance(key, str) or not key or type(raw_count) is not int or raw_count < 0:
                raise RuntimeError(f"sealed v5 Worker metric {name} is invalid")
            parsed[key] = raw_count
        return parsed

    def finite_number(name: str) -> float:
        value = raw.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise RuntimeError(f"sealed v5 Worker metric {name} is invalid")
        converted = float(value)
        if not math.isfinite(converted):
            raise RuntimeError(f"sealed v5 Worker metric {name} is invalid")
        return converted

    structure = manifest.structure_contract
    vector = manifest.vector_sidecar
    if structure is None or vector is None or manifest.primary_embedding_profile_id is None:
        raise RuntimeError("sealed v5 Worker metrics lost their manifest contract")
    parser_counts = count_map("parser_profile_document_counts")
    node_counts = count_map("node_type_counts")
    major_counts = count_map("major_class_counts")
    expected_node_counts = {
        "BOILERPLATE": structure.node_counts.boilerplate,
        "FOOTNOTE": structure.node_counts.footnote,
        "ITEM": structure.node_counts.item,
        "LIST_ITEM": structure.node_counts.list_item,
        "MAJOR_SECTION": structure.node_counts.major_section,
        "PARAGRAPH": structure.node_counts.paragraph,
        "ROOT": structure.node_counts.root,
        "TABLE": structure.node_counts.table,
        "TABLE_ROW": structure.node_counts.table_row,
        "UNCLASSIFIED": structure.node_counts.unclassified,
    }
    expected_major_counts = {
        "BENEFIT": structure.major_class_counts.benefit,
        "MIXED": structure.major_class_counts.mixed,
        "NOTICE": structure.major_class_counts.notice,
        "UNKNOWN": structure.major_class_counts.unknown,
    }
    if (
        node_counts != expected_node_counts
        or major_counts != expected_major_counts
        or sum(parser_counts.values()) != structure.revision_counts.total
        or count("contract_revision_count") != structure.revision_counts.total
        or count("current_revision_count") != structure.revision_counts.current
        or count("superseded_revision_count") != structure.revision_counts.superseded
        or count("ambiguous_revision_count") != structure.revision_counts.ambiguous
        or count("source_non_whitespace_count") != structure.source_coverage.source_non_whitespace_characters
        or count("covered_non_whitespace_count")
        != structure.source_coverage.covered_non_whitespace_characters
        or count("table_node_count") != structure.node_counts.table
        or count("footnote_node_count") != structure.node_counts.footnote
    ):
        raise RuntimeError("sealed v5 Worker metrics differ from the manifest")
    coverage = finite_number("source_coverage_percent")
    ratio = finite_number("unknown_unclassified_ratio")
    if not math.isclose(coverage, 100.0, abs_tol=1e-12) or not 0.0 <= ratio <= 1.0:
        raise RuntimeError("sealed v5 Worker metric ratios are invalid")
    unknown_count = count("unknown_unclassified_count")
    unknown_denominator = count("unknown_unclassified_denominator")
    expected_unknown = node_counts["UNCLASSIFIED"] + major_counts["UNKNOWN"]
    expected_denominator = sum(node_counts.values()) + node_counts["MAJOR_SECTION"]
    expected_ratio = 0.0 if expected_denominator == 0 else expected_unknown / expected_denominator
    if (
        unknown_count != expected_unknown
        or unknown_denominator != expected_denominator
        or not math.isclose(ratio, expected_ratio, abs_tol=1e-15)
    ):
        raise RuntimeError("sealed v5 unknown/unclassified metrics are inconsistent")
    raw_view_counts = raw.get("embedding_view_counts")
    if not isinstance(raw_view_counts, Mapping) or set(raw_view_counts) != set(EMBEDDING_VIEW_TYPES):
        raise RuntimeError("sealed v5 embedding view metrics are invalid")
    for view_type, row in raw_view_counts.items():
        if not isinstance(row, Mapping) or set(row) != {"downloads", "hits", "misses"}:
            raise RuntimeError("sealed v5 embedding view metrics are invalid")
        downloads = row.get("downloads")
        hits = row.get("hits")
        misses = row.get("misses")
        if any(type(value) is not int or value < 0 for value in (downloads, hits, misses)):
            raise RuntimeError("sealed v5 embedding view metrics are invalid")
        assert isinstance(downloads, int) and isinstance(hits, int) and isinstance(misses, int)
        if downloads > misses:
            raise RuntimeError("sealed v5 embedding downloads exceed misses")
        if view_type not in EMBEDDING_VIEW_TYPES:
            raise RuntimeError("sealed v5 embedding view metric type is invalid")
    if (
        raw.get("embedding_provider") != manifest.embedding_contract.provider
        or raw.get("embedding_model") != manifest.embedding_contract.model
        or count("embedding_dimension") != manifest.embedding_contract.dimension
        or raw.get("embedding_profile_id") != manifest.primary_embedding_profile_id
        or count("vector_sidecar_size_bytes") != vector.artifact.size_bytes
    ):
        raise RuntimeError("sealed v5 embedding metrics differ from the manifest")
    if raw.get("revision_history_policy_version") != REVISION_HISTORY_POLICY_VERSION:
        raise RuntimeError("sealed v5 revision history policy is invalid")
    raw_unresolved = raw.get("historical_revision_unresolved_identities")
    if not isinstance(raw_unresolved, list):
        raise RuntimeError("sealed v5 unresolved revision ledger is invalid")
    parsed_unresolved: list[UnresolvedRevisionIdentityV5] = []
    try:
        for entry in raw_unresolved:
            if not isinstance(entry, Mapping) or set(entry) != {
                "source_id",
                "pdf_sha256",
                "reason_codes",
            }:
                raise ValueError
            reason_codes = entry.get("reason_codes")
            if not isinstance(reason_codes, list) or not reason_codes:
                raise ValueError
            for reason_code in reason_codes:
                if not isinstance(reason_code, str):
                    raise ValueError
                parsed_unresolved.append(
                    UnresolvedRevisionIdentityV5(
                        source_id=str(entry.get("source_id")),
                        pdf_sha256=str(entry.get("pdf_sha256")),
                        reason_code=cast(UnresolvedRevisionReasonV5, reason_code),
                    )
                )
    except (TypeError, ValueError):
        raise RuntimeError("sealed v5 unresolved revision ledger is invalid") from None
    canonical_unresolved = canonical_unresolved_revision_ledger_v5(parsed_unresolved)
    if (
        raw_unresolved != list(canonical_unresolved)
        or count("historical_revision_unresolved_count") != len(canonical_unresolved)
        or raw.get("historical_revision_unresolved_sha256")
        != unresolved_revision_ledger_sha256_v5(canonical_unresolved)
    ):
        raise RuntimeError("sealed v5 unresolved revision ledger is inconsistent")
    fallback_policy_version = str(unclassified_fallback_policy_payload()["schema_version"])
    if raw.get("structure_fallback_policy_version") != fallback_policy_version:
        raise RuntimeError("sealed v5 structure fallback policy is invalid")
    raw_fallback_documents = raw.get("structure_fallback_documents")
    if not isinstance(raw_fallback_documents, list):
        raise RuntimeError("sealed v5 structure fallback ledger is invalid")
    parsed_fallback_documents: list[dict[str, Any]] = []
    for raw_document in raw_fallback_documents:
        if not isinstance(raw_document, Mapping) or set(raw_document) != {
            "contract_revision_id",
            "document_id",
            "reason_code",
            "structure_artifact_sha256",
        }:
            raise RuntimeError("sealed v5 structure fallback ledger is invalid")
        document_id = raw_document.get("document_id")
        revision_id = raw_document.get("contract_revision_id")
        reason_code = raw_document.get("reason_code")
        artifact_sha256 = raw_document.get("structure_artifact_sha256")
        if (
            not isinstance(document_id, str)
            or re.fullmatch(r"doc_[0-9a-f]{64}", document_id) is None
            or not isinstance(revision_id, str)
            or re.fullmatch(r"revision_[0-9a-f]{64}", revision_id) is None
            or reason_code not in {"parser_failed", "derived_view_failed"}
            or not isinstance(artifact_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", artifact_sha256) is None
        ):
            raise RuntimeError("sealed v5 structure fallback ledger is invalid")
        parsed_fallback_documents.append(
            {
                "contract_revision_id": revision_id,
                "document_id": document_id,
                "reason_code": reason_code,
                "structure_artifact_sha256": artifact_sha256,
            }
        )
    canonical_fallback = _canonical_structure_fallback_ledger(parsed_fallback_documents)
    canonical_fallback_documents = canonical_fallback["documents"]
    fallback_document_ids = [row["document_id"] for row in parsed_fallback_documents]
    available_document_ids = {
        document.document_id for document in manifest.documents if document.availability == "available"
    }
    if (
        raw_fallback_documents != canonical_fallback_documents
        or len(fallback_document_ids) != len(set(fallback_document_ids))
        or not set(fallback_document_ids) <= available_document_ids
        or count("structure_fallback_document_count") != len(parsed_fallback_documents)
        or raw.get("structure_fallback_documents_sha256") != canonical_sha256(canonical_fallback)
    ):
        raise RuntimeError("sealed v5 structure fallback ledger is inconsistent")
    if count("structure_failed_document_count") != 0 or raw.get(
        "structure_failed_documents_sha256"
    ) != _structure_failure_ledger_sha256(()):
        raise RuntimeError("sealed v5 generation contains a structure_failed disposition")
    for name in (
        "cross_page_continuation_count",
        "historical_pdf_cache_hits",
        "embedding_provider_call_count",
        "ocr_cache_reused_count",
        "ocr_provider_called_count",
    ):
        count(name)
    # Round-trip to plain canonical JSON types so callers cannot retain a
    # mutable or custom Mapping implementation supplied to validation.
    return cast(dict[str, Any], json.loads(canonical_json_bytes(raw)))


def _canonical_ocr_body(result: OCRResult) -> bytes:
    # OCRResult has already strictly verified and retained the immutable source
    # artifact. Rebuilding it from marker-free page text would silently change
    # valid legacy spacing (for example, one newline after a page marker) and
    # break the manifest/CAS SHA binding.
    return result.ocr_bytes


_DOCUMENT_SCOPED_HTTP_STATUSES = frozenset({408, 413, 422, 425, 429})


def _bounded_report_text(value: str, *, maximum: int) -> str:
    return value if len(value) <= maximum else value[:maximum]


def is_isolatable_document_ocr_failure(exc: Exception) -> bool:
    """Return whether an exhausted OCR error may be isolated to one PDF.

    This is intentionally an allowlist. Authentication/configuration responses,
    connection failures, local I/O, state/database failures, and unexpected
    programming errors must stop the run immediately instead of being repeated
    across the remaining corpus.
    """

    if isinstance(
        exc,
        (OCRValidationError, ProviderDocumentError, httpx.TimeoutException, TimeoutError),
    ):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        status_code = exc.response.status_code
        return status_code in _DOCUMENT_SCOPED_HTTP_STATUSES or 500 <= status_code <= 599
    return False


def classify_ocr_failure(exc: Exception) -> OCRFailureReason:
    """Map an OCR exception to bounded text that is safe to persist and print."""

    if isinstance(exc, OCRValidationError):
        message = str(exc)
        if message == "OCR sparse-page wrapper is invalid":
            return OCRFailureReason(
                "sparse_page_wrapper_invalid",
                "The provider returned an invalid sparse-page wrapper.",
            )
        if message.startswith("OCR page markers ") and " do not match " in message:
            return OCRFailureReason(
                "page_marker_mismatch",
                "The provider returned page markers that did not match the requested pages.",
            )
        if message == "OCR provider output must begin with the first Page marker":
            return OCRFailureReason(
                "output_not_marker_first",
                "The provider output did not begin with the required page marker.",
            )
        if message in {"OCR contains an empty page", "OCR provider returned an empty page"}:
            return OCRFailureReason("empty_page", "The provider returned an empty OCR page.")
        if message == "OCR blank-page sentinel must be exact":
            return OCRFailureReason(
                "blank_sentinel_invalid",
                "The provider returned an invalid blank-page sentinel.",
            )
        if message in {
            "OCR provider returned an implausibly short page",
            "OCR contains an implausibly short page",
        }:
            return OCRFailureReason(
                "implausibly_short",
                "The provider returned an implausibly short OCR page.",
            )
        if "cache" in message.casefold() or message.startswith("local native OCR"):
            return OCRFailureReason(
                "cache_validation_error",
                "A cached OCR artifact failed validation.",
            )
        return OCRFailureReason(
            "generic_validation_error",
            "The OCR output failed validation.",
        )
    if isinstance(exc, ProviderDocumentError):
        return OCRFailureReason(
            "provider_document_rejected",
            "The OCR provider could not process this document.",
        )
    if isinstance(exc, ProviderError):
        return OCRFailureReason("provider_error", "The OCR provider failed.")
    if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
        return OCRFailureReason("provider_timeout", "The OCR provider timed out.")
    if isinstance(exc, httpx.HTTPStatusError):
        status_code = exc.response.status_code
        return OCRFailureReason(
            f"provider_http_{status_code}",
            f"The OCR provider returned HTTP status {status_code}.",
        )
    if isinstance(exc, httpx.RequestError):
        return OCRFailureReason(
            "provider_network_error",
            "The OCR provider could not be reached.",
        )
    if isinstance(exc, OSError):
        return OCRFailureReason("local_io_error", "A local OCR file operation failed.")
    return OCRFailureReason("unexpected_error", "OCR failed unexpectedly.")


@dataclass(frozen=True, slots=True)
class _OCRSystemicFailureReason:
    reason_code: str
    reason: str
    error_class_category: str
    phase: str | None = None
    status_code: int | None = None
    error_kind: str | None = None
    retryable: bool | None = None
    publication_attempts: int | None = None
    exit_code: int | None = None
    stderr_size_bytes: int | None = None
    stderr_sha256: str | None = None


def _classify_ocr_systemic_failure(exc: Exception) -> _OCRSystemicFailureReason:
    """Classify without copying raw exception text into durable state."""

    if isinstance(exc, OCRCacheHealingIdentityError):
        return _OCRSystemicFailureReason(
            "ocr_cache_healing_identity_mismatch",
            "OCR cache healing returned bytes outside the retained generation identity.",
            "ocr_cache_integrity",
            phase="healing",
            error_kind="integrity",
            retryable=False,
        )
    if isinstance(exc, ProviderSystemicError):
        try:
            canonical_provider_error = ProviderSystemicError(
                exc.reason_code,
                exit_code=exc.exit_code,
                stderr_size_bytes=exc.stderr_size_bytes,
                stderr_sha256=exc.stderr_sha256,
            )
        except (KeyError, TypeError, ValueError):
            canonical_provider_error = ProviderSystemicError()
        return _OCRSystemicFailureReason(
            canonical_provider_error.reason_code,
            canonical_provider_error.reason,
            "ocr_provider_systemic",
            error_kind=canonical_provider_error.error_kind,
            retryable=canonical_provider_error.retryable,
            exit_code=canonical_provider_error.exit_code,
            stderr_size_bytes=canonical_provider_error.stderr_size_bytes,
            stderr_sha256=canonical_provider_error.stderr_sha256,
        )
    if isinstance(exc, ProviderError):
        return _OCRSystemicFailureReason(
            "provider_systemic_failure",
            "The OCR provider failed outside a document boundary.",
            "ocr_provider_systemic",
            error_kind="systemic",
            retryable=False,
        )
    if isinstance(exc, OCRCachePublicationError):
        try:
            canonical_cache_error = OCRCachePublicationError(
                phase=exc.phase,
                error_kind=exc.error_kind,
                status_code=exc.status_code,
                retryable=exc.retryable,
                attempts=exc.attempts,
            )
        except (KeyError, TypeError, ValueError):
            return _OCRSystemicFailureReason(
                "ocr_cache_publication_error",
                "OCR cache publication failed.",
                "ocr_cache_publication",
            )
        reason_code = canonical_cache_error.reason_code
        reason = canonical_cache_error.reason
        phase = canonical_cache_error.phase
        error_kind = canonical_cache_error.error_kind
        status_code = canonical_cache_error.status_code
        retryable = canonical_cache_error.retryable
        publication_attempts = canonical_cache_error.attempts
        if (
            not re.fullmatch(r"[a-z0-9_]{1,64}", reason_code)
            or not reason
            or len(reason) > 256
            or any(ord(character) < 32 or ord(character) == 127 for character in reason)
            or not re.fullmatch(r"[a-z0-9_]{1,32}", phase)
            or not re.fullmatch(r"[a-z0-9_]{1,32}", error_kind)
            or (status_code is not None and not 100 <= status_code <= 599)
            or not isinstance(retryable, bool)
            or not 1 <= publication_attempts <= 64
        ):
            return _OCRSystemicFailureReason(
                "ocr_cache_publication_error",
                "OCR cache publication failed.",
                "ocr_cache_publication",
            )
        return _OCRSystemicFailureReason(
            reason_code,
            reason,
            "ocr_cache_publication",
            phase=phase,
            status_code=status_code,
            error_kind=error_kind,
            retryable=retryable,
            publication_attempts=publication_attempts,
        )
    if isinstance(exc, WebDAVHTTPError):
        status_code = exc.status_code
        return _OCRSystemicFailureReason(
            f"ocr_cache_http_{status_code}",
            f"The OCR cache returned HTTP status {status_code}.",
            "ocr_cache_webdav",
            status_code=status_code,
            error_kind="http",
        )
    if isinstance(exc, WebDAVIntegrityError):
        return _OCRSystemicFailureReason(
            "ocr_cache_integrity_error",
            "OCR cache integrity verification failed.",
            "ocr_cache_webdav",
            error_kind="integrity",
        )
    if isinstance(exc, WebDAVError):
        chain: list[BaseException] = []
        seen: set[int] = set()
        current: BaseException | None = exc
        while current is not None and id(current) not in seen and len(chain) < 8:
            seen.add(id(current))
            chain.append(current)
            current = current.__cause__ or current.__context__
        if any(isinstance(item, (httpx.TimeoutException, TimeoutError)) for item in chain):
            return _OCRSystemicFailureReason(
                "ocr_cache_timeout",
                "The OCR cache request timed out.",
                "ocr_cache_webdav",
                error_kind="timeout",
            )
        if any(isinstance(item, httpx.RequestError) for item in chain):
            return _OCRSystemicFailureReason(
                "ocr_cache_network_error",
                "The OCR cache could not be reached.",
                "ocr_cache_webdav",
                error_kind="network",
            )
        return _OCRSystemicFailureReason(
            "ocr_cache_webdav_error",
            "The OCR cache operation failed.",
            "ocr_cache_webdav",
            error_kind="unexpected",
        )
    if isinstance(exc, sqlite3.Error):
        return _OCRSystemicFailureReason(
            "ocr_database_error",
            "An OCR database operation failed.",
            "database",
        )
    if isinstance(exc, httpx.HTTPStatusError):
        status_code = exc.response.status_code
        return _OCRSystemicFailureReason(
            f"ocr_http_{status_code}",
            f"An OCR dependency returned HTTP status {status_code}.",
            "http_status",
            status_code=status_code,
        )
    if isinstance(exc, httpx.RequestError):
        return _OCRSystemicFailureReason(
            "ocr_network_error",
            "An OCR dependency could not be reached.",
            "network",
        )
    if isinstance(exc, OSError):
        return _OCRSystemicFailureReason(
            "ocr_local_io_error",
            "A local OCR file operation failed.",
            "local_io",
        )
    return _OCRSystemicFailureReason(
        "ocr_unexpected_error",
        "OCR failed unexpectedly.",
        "unexpected",
    )


def _write_ocr_systemic_failure_report(
    path: Path,
    *,
    failure: OCRSystemicFailureRecord,
) -> None:
    try:
        _atomic_write(
            path,
            canonical_json_bytes(
                {
                    **failure.payload,
                    "schema_version": "cardrag.ocr-systemic-failure-report.v1",
                }
            ),
        )
    except Exception:
        raise RuntimeError("OCR systemic failure report write failed") from None


class _WorkerPhaseFailure(RuntimeError):
    """A secret-safe snapshot of a failed worker phase."""

    def __init__(
        self,
        *,
        phase: str,
        error_class_category: str,
        status_code: int | None,
        errno: int | None,
    ) -> None:
        self.phase = phase
        self.error_class_category = error_class_category
        self.status_code = status_code
        self.errno = errno
        super().__init__("Worker pipeline phase failed.")


def _classify_worker_failure(exc: Exception) -> tuple[str, int | None, int | None]:
    """Return only allowlisted diagnostic categories and bounded integers."""

    if isinstance(exc, _WorkerPhaseFailure):
        return exc.error_class_category, exc.status_code, exc.errno
    if isinstance(exc, ProviderSystemicError):
        return "provider_systemic", None, None
    if isinstance(exc, ProviderError):
        return "provider", None, None
    if isinstance(exc, WebDAVHTTPError):
        status_code = exc.status_code
        return "remote_http", status_code if 100 <= status_code <= 599 else None, None
    if isinstance(exc, WebDAVIntegrityError):
        return "remote_integrity", None, None
    if isinstance(exc, WebDAVError):
        return "remote", None, None
    if isinstance(exc, EmbeddingV5RequestError):
        return "openrouter_request", exc.status_code, None
    if isinstance(exc, sqlite3.Error):
        return "database", None, None
    if isinstance(exc, httpx.HTTPStatusError):
        status_code = exc.response.status_code
        return "http_status", status_code, None
    if isinstance(exc, httpx.RequestError):
        return "network", None, None
    if isinstance(exc, OSError):
        error_number = exc.errno
        safe_errno = (
            error_number
            if isinstance(error_number, int)
            and not isinstance(error_number, bool)
            and 1 <= error_number <= 4095
            else None
        )
        return "local_io", None, safe_errno
    if isinstance(exc, (ValueError, TypeError, KeyError, AssertionError)):
        return "contract", None, None
    if isinstance(exc, RuntimeError):
        return "runtime", None, None
    return "unexpected", None, None


def _safe_stage_error(exc: Exception) -> str:
    category, status_code, error_number = _classify_worker_failure(exc)
    fields = [f"category={category}"]
    if status_code is not None:
        fields.append(f"status_code={status_code}")
    if error_number is not None:
        fields.append(f"errno={error_number}")
    return f"worker_stage_failure: Worker pipeline stage failed ({', '.join(fields)})."


def _safe_v5_embedding_terminal_error(exc: Exception) -> str:
    if isinstance(exc, V5CapacityError):
        return "v5_capacity_preflight_failed: Worker local capacity rejected predicted v5 artifacts"
    if isinstance(exc, EmbeddingV5PermanentRequestError):
        fields = [f"kind={exc.kind}"]
        if exc.status_code is not None:
            fields.append(f"status_code={exc.status_code}")
        return (
            "v5_embedding_request_rejected: OpenRouter permanently rejected the embedding request "
            f"({', '.join(fields)})"
        )
    return "v5_embedding_contract_failed: OpenRouter embedding response violated its contract"


def _write_worker_failure_report(
    path: Path,
    *,
    failure: WorkerUnexpectedFailureRecord,
) -> None:
    try:
        _atomic_write(
            path,
            canonical_json_bytes(
                {
                    **failure.payload,
                    "schema_version": "cardrag.worker-failure-report.v1",
                }
            ),
        )
    except Exception:
        raise RuntimeError("Worker failure report write failed") from None


def _write_ocr_failure_report(
    path: Path,
    *,
    run_id: str,
    failures: Sequence[OCRFailureRecord],
) -> None:
    ordered = sorted(
        failures,
        key=lambda item: (item.issuer, item.product_code, item.document_id),
    )
    try:
        _atomic_write(
            path,
            canonical_json_bytes(
                {
                    "failure_count": len(ordered),
                    "failures": [item.payload for item in ordered],
                    "run_id": run_id,
                    "schema_version": "cardrag.ocr-failure-report.v1",
                }
            ),
        )
    except Exception:
        raise RuntimeError("OCR failure report write failed") from None


def _structure_source_pages_sha256(pages: Sequence[PageRecord]) -> str:
    return canonical_sha256(
        {
            "pages": [{"page": page.page, "text_sha256": page.text_sha256} for page in pages],
            "schema_version": "cardrag.structure-failure-source.v1",
        }
    )


def _write_structure_failure_report(
    path: Path,
    *,
    run_id: str,
    failures: Sequence[StructureFailureRecord],
) -> None:
    ledger = _canonical_structure_failure_ledger(failures)
    try:
        _atomic_write(
            path,
            canonical_json_bytes(
                {
                    "ledger": ledger,
                    "ledger_sha256": canonical_sha256(ledger),
                    "run_id": run_id,
                    "schema_version": "cardrag.structure-failure-report.v1",
                    "structure_failed_count": len(failures),
                }
            ),
        )
    except Exception:
        raise RuntimeError("structure failure report write failed") from None


def select_current(records: Sequence[SourceRecord]) -> tuple[SourceRecord, ...]:
    """Latest-only inputs must be unique; ambiguity fails closed."""

    selected: dict[tuple[str, str], SourceRecord] = {}
    for record in records:
        key = (record.issuer, record.product_code)
        previous = selected.get(key)
        if previous is None:
            selected[key] = record
        elif previous.discovery_payload != record.discovery_payload:
            raise CorpusConflictError(
                f"issuer discovery returned multiple current documents for {record.issuer}/{record.product_code}"
            )
    return tuple(sorted(selected.values(), key=lambda row: (row.issuer, row.product_code)))


def _restore_snapshot(
    payload: Mapping[str, Any],
    *,
    observed_at: datetime,
    expected_issuer: str,
    expected_parser_version: str,
) -> SourceSnapshot:
    if (
        payload.get("contract_version") != "cardrag.source-snapshot.v1"
        or payload.get("issuer") != expected_issuer
        or payload.get("parser_version") != expected_parser_version
        or not isinstance(payload.get("records"), list)
    ):
        raise RuntimeError("stored issuer snapshot contract does not match its adapter")
    records: list[SourceRecord] = []
    for raw in payload["records"]:
        if not isinstance(raw, dict) or not isinstance(raw.get("metadata"), dict):
            raise RuntimeError("stored issuer snapshot contains an invalid record")
        records.append(
            SourceRecord(
                issuer=str(raw["issuer"]),
                product_code=str(raw["product_code"]),
                product_name=str(raw["product_name"]),
                effective_date=date.fromisoformat(str(raw["effective_date"])),
                source_version=str(raw["source_version"]),
                source_url=str(raw["source_url"]),
                source_post_id=str(raw["source_post_id"]),
                file_name=str(raw["file_name"]),
                category=str(raw["category"]),
                discovered_at=observed_at,
                metadata=raw["metadata"],
                document_type=str(raw["document_type"]),
            )
        )
    return SourceSnapshot(
        issuer=expected_issuer,
        source_url=str(payload.get("source_url") or ""),
        parser_version=expected_parser_version,
        records=tuple(records),
        started_at=observed_at,
        finished_at=observed_at,
    )


def _known_snapshot_sources(
    state: WorkerState,
    adapters: Sequence[IssuerAdapter],
    current_records: Sequence[SourceRecord],
) -> dict[str, SourceRecord]:
    """Restore only canonical source payloads retained in durable snapshots."""

    known: dict[str, SourceRecord] = {}
    for adapter in adapters:
        for payload, observed_at in state.snapshot_history(adapter.spec.code):
            parser_version = payload.get("parser_version")
            if not isinstance(parser_version, str) or not parser_version:
                raise RuntimeError("stored source history has no parser version")
            snapshot = _restore_snapshot(
                payload,
                observed_at=observed_at,
                expected_issuer=adapter.spec.code,
                expected_parser_version=parser_version,
            )
            for source in snapshot.records:
                existing = known.get(source.source_id)
                if existing is not None and existing.discovery_payload != source.discovery_payload:
                    raise RuntimeError("stored snapshots disagree on a source identity")
                known[source.source_id] = source
    for source in current_records:
        existing = known.get(source.source_id)
        if existing is not None and existing.discovery_payload != source.discovery_payload:
            raise RuntimeError("current source conflicts with durable snapshot history")
        known[source.source_id] = source
    return known


def _section_type(text: str) -> str:
    first = text.lstrip().splitlines()[0] if text.strip() else ""
    return "heading" if first.startswith("#") else "body"


def chunk_pages(
    document_id: str,
    pages: Sequence[PageRecord],
    *,
    maximum_chars: int = 1600,
    overlap_chars: int = 160,
) -> tuple[dict[str, Any], ...]:
    if maximum_chars < 200 or not 0 <= overlap_chars < maximum_chars // 2:
        raise ValueError("invalid chunk window")
    chunks: list[dict[str, Any]] = []
    for page in pages:
        length = len(page.text)
        start = 0
        while start < length:
            limit = min(length, start + maximum_chars)
            end = limit
            if limit < length:
                boundary = max(
                    page.text.rfind("\n", start + 1, limit), page.text.rfind(" ", start + 1, limit)
                )
                if boundary > start + maximum_chars // 2:
                    end = boundary
            while start < end and page.text[start].isspace():
                start += 1
            while end > start and page.text[end - 1].isspace():
                end -= 1
            if end <= start:
                break
            text = page.text[start:end]
            evidence_id = "evidence_" + canonical_sha256(
                {
                    "document_id": document_id,
                    "page": page.page,
                    "source_end": end,
                    "source_start": start,
                    "text_sha256": sha256_bytes(text.encode()),
                }
            )
            chunks.append(
                {
                    "evidence_id": evidence_id,
                    "document_id": document_id,
                    "page_start": page.page,
                    "page_end": page.page,
                    "section_type": _section_type(text),
                    "text": text,
                    "source_start": start,
                    "source_end": end,
                }
            )
            if end >= length:
                break
            next_start = max(start + 1, end - overlap_chars)
            start = next_start
    return tuple(chunks)


def _embedding_miss_batches(
    misses: Sequence[int],
    formatted_token_counts: Sequence[int],
    *,
    maximum_tokens: int,
    maximum_batch_size: int = 64,
) -> tuple[tuple[int, ...], ...]:
    """Greedily bind deterministic provider batches to count and token caps."""

    if maximum_tokens < 1 or maximum_batch_size < 1:
        raise ValueError("embedding batch limits must be positive")
    batches: list[tuple[int, ...]] = []
    pending: list[int] = []
    pending_tokens = 0
    for index in misses:
        if index < 0 or index >= len(formatted_token_counts):
            raise ValueError("embedding miss index is outside the token ledger")
        token_count = formatted_token_counts[index]
        if isinstance(token_count, bool) or token_count < 1 or token_count > maximum_tokens:
            raise ValueError("embedding input exceeds its exact batch token cap")
        if pending and (len(pending) >= maximum_batch_size or pending_tokens + token_count > maximum_tokens):
            batches.append(tuple(pending))
            pending = []
            pending_tokens = 0
        pending.append(index)
        pending_tokens += token_count
    if pending:
        batches.append(tuple(pending))
    return tuple(batches)


async def validate_document_aggregation_head(
    webdav: WebDAVClient,
    selected: VerifiedAggregationProfileV5,
    *,
    expected_m1_contract_sha256: str | None = None,
) -> GenerationManifest:
    """GET-only proof that the channel head is the evaluated M0 or its sealed M1.

    The CLI calls this before creating Worker state or running the credentialed
    Qwen preflight.  At that point the live endpoint metadata needed to rebuild
    the complete M1 Worker contract is intentionally unavailable.  The pipeline
    therefore calls it again with ``expected_m1_contract_sha256`` after provider
    preflight, and once more immediately before fixing the publication
    predecessor.  Those later calls close the startup/publication TOCTOU windows
    and bind the complete profile-artifact contract.
    """

    current = await webdav.validated_current_generation()
    if current is None:
        raise RuntimeError("sealed document aggregation requires a valid remote M0/M1 head")
    body = await webdav.get_bytes(
        generation_manifest_path(current.generation_id),
        max_bytes=MAX_GENERATION_MANIFEST_BYTES,
    )
    if body is None:
        raise RuntimeError("sealed document aggregation remote head manifest is missing")
    try:
        manifest = GenerationManifest.model_validate_json(body)
    except Exception:
        raise RuntimeError("sealed document aggregation remote head manifest is invalid") from None
    if body != manifest.canonical_bytes():
        raise RuntimeError("sealed document aggregation remote head manifest is not canonical")
    if (
        manifest.generation_id != current.generation_id
        or manifest.corpus_sha256 != current.corpus_sha256
        or manifest.contract_sha256 != current.contract_sha256
        or manifest.schema_version != current.generation_schema
        or manifest.serving_schema != current.serving_schema
        or sum(document.availability == "ocr_failed" for document in manifest.documents)
        != current.ocr_failed_document_count
        or manifest.schema_version != GENERATION_SCHEMA_ID_V5
        or manifest.primary_embedding_profile_id != selected.profile.embedding_profile_id
    ):
        raise RuntimeError("sealed document aggregation remote head identity is inconsistent")

    if manifest.document_aggregation_profile is None:
        if (
            manifest.generation_id != selected.profile.generation_id
            or manifest.manifest_sha256 != selected.profile.generation_manifest_sha256
            or manifest.retrieval_policy_sha256 != canonical_sha256(V5_RETRIEVAL_POLICY)
        ):
            raise RuntimeError("sealed document aggregation is not based on its evaluated M0")
        return manifest

    expected_retrieval_policy = sealed_v5_retrieval_policy(
        selected.profile,
        selected.profile_sha256,
    )
    if (
        manifest.generation_id == selected.profile.generation_id
        or (
            expected_m1_contract_sha256 is not None
            and manifest.contract_sha256 != expected_m1_contract_sha256
        )
        or manifest.document_aggregation_profile != selected.profile
        or manifest.document_aggregation_policy != selected.profile.aggregation_policy
        or manifest.sealed_profile_sha256 != selected.profile_sha256
        or manifest.exact_row_corpus_sha256 != selected.profile.exact_row_corpus_sha256
        or manifest.retrieval_policy_sha256 != canonical_sha256(expected_retrieval_policy)
    ):
        raise RuntimeError("sealed document aggregation remote M1 identity is inconsistent")
    return manifest


class WorkerPipeline:
    def __init__(
        self,
        *,
        state: WorkerState,
        state_dir: Path,
        adapters: Sequence[IssuerAdapter],
        ocr: OCRResolver,
        embeddings: EmbeddingProvider | OpenRouterQwenEmbeddingProviderV5,
        webdav: WebDAVClient,
        maximum_attempts: int = 4,
        retry_cap_seconds: float = 30,
        pdf_cache_refresh_hours: float = 168,
        collect_remote_garbage: bool = False,
        stable_publication_approved: bool = False,
        ocr_cache_publication_approved: bool = False,
        remote_gc_approved: bool = False,
        retained_generations: int = 2,
        garbage_grace_days: int = 30,
        retained_incomplete_runs: int = 2,
        document_aggregation: VerifiedAggregationProfileV5 | None = None,
        capacity_policy_v5: V5CapacityPolicy | None = None,
    ) -> None:
        if not adapters:
            raise ValueError("at least one issuer adapter must be enabled")
        if embeddings.dimension == EMBEDDING_DIMENSION:
            v5_profile: QwenEmbeddingProfileV5 | None = None
        elif isinstance(embeddings, OpenRouterQwenEmbeddingProviderV5):
            v5_profile = embeddings.profile
            if (
                getattr(embeddings.token_counter, "asset_sha256", None) != QWEN_TOKENIZER_SHA256
                or getattr(embeddings.token_counter, "revision", None) != QWEN_TOKENIZER_REVISION
            ):
                raise ValueError("Qwen v5 pipeline requires the pinned exact tokenizer contract")
        else:
            raise ValueError(
                "embedding provider must be the legacy 1,536D rollback provider or a sealed Qwen v5 provider"
            )
        ocr_cache_mode = getattr(ocr, "cache_mode", "read-write")
        if v5_profile is None and ocr_cache_mode != "read-write":
            raise ValueError("legacy v4 Worker requires its original read-write OCR cache contract")
        if v5_profile is not None and webdav.channel == "stable" and not stable_publication_approved:
            raise ValueError("stable v1.0.14 publication requires explicit approval")
        if v5_profile is not None and webdav.channel == "candidate-v1.0.11" and ocr_cache_mode != "read-only":
            raise ValueError("v1.0.11 candidate requires read-only remote OCR cache access")
        if v5_profile is not None and ocr_cache_mode == "read-write" and not ocr_cache_publication_approved:
            raise ValueError("v1.0.11 remote OCR cache publication requires separate approval")
        if document_aggregation is not None:
            if v5_profile is None:
                raise ValueError("sealed document aggregation requires the Qwen v5 pipeline")
            if document_aggregation.profile.embedding_profile_id != v5_profile.profile_id:
                raise ValueError("sealed document aggregation uses another embedding profile")
        if capacity_policy_v5 is not None and not isinstance(capacity_policy_v5, V5CapacityPolicy):
            raise TypeError("capacity_policy_v5 must be a V5CapacityPolicy")
        if collect_remote_garbage and (
            webdav.channel != "stable" or not stable_publication_approved or not remote_gc_approved
        ):
            raise ValueError("remote GC requires stable channel plus publication and remote-GC approvals")
        if retained_generations < 1 or garbage_grace_days < 1 or retained_incomplete_runs < 1:
            raise ValueError("garbage retention and grace must be positive")
        if not math.isfinite(pdf_cache_refresh_hours) or pdf_cache_refresh_hours <= 0:
            raise ValueError("PDF cache refresh hours must be positive and finite")
        try:
            pdf_cache_refresh_interval = timedelta(hours=pdf_cache_refresh_hours)
        except OverflowError as exc:
            raise ValueError("PDF cache refresh hours are too large") from exc
        self.state = state
        self.state_dir = state_dir
        self.pdf_cache = PDFCache(state_dir, state)
        self.adapters = tuple(adapters)
        self.ocr = ocr
        self.embeddings = embeddings
        self.v5_profile = v5_profile
        self.document_aggregation = document_aggregation
        self.webdav = webdav
        self.maximum_attempts = maximum_attempts
        self.retry_cap_seconds = retry_cap_seconds
        self.pdf_cache_refresh_interval = pdf_cache_refresh_interval
        self.collect_remote_garbage = collect_remote_garbage
        self.stable_publication_approved = stable_publication_approved
        self.ocr_cache_publication_approved = ocr_cache_publication_approved
        self.remote_gc_approved = remote_gc_approved
        self.retained_generations = retained_generations
        self.garbage_grace_days = garbage_grace_days
        self.retained_incomplete_runs = retained_incomplete_runs
        self.exporter = ServingDatabaseExporter()
        self.exporter_v5 = ServingDatabaseExporterV5()
        self.capacity_policy_v5 = None if v5_profile is None else capacity_policy_v5 or V5CapacityPolicy()
        self.limiters = {
            adapter.spec.code: IssuerRateLimiter(adapter.spec.minimum_interval_seconds)
            for adapter in self.adapters
        }

    @property
    def contract_sha256(self) -> str:
        if self.v5_profile is not None:
            parser_profiles = [
                issuer_parser_profile(adapter.spec.code).payload
                for adapter in sorted(self.adapters, key=lambda item: item.spec.code)
            ]
            contract_payload: dict[str, Any] = {
                "schema_version": "cardrag.worker-contract.v4",
                "serving_schema": SERVING_SCHEMA_ID_V5,
                "issuer_adapters": [
                    {
                        "code": adapter.spec.code,
                        "display_name": adapter.spec.display_name,
                        "sort_order": adapter.spec.sort_order,
                        "parser_version": adapter.parser_version,
                        "allowed_hosts": sorted(adapter.spec.allowed_hosts),
                        "categories": list(adapter.spec.categories),
                        "minimum_records": adapter.spec.minimum_records,
                        "minimum_interval_seconds": adapter.spec.minimum_interval_seconds,
                        "retry_base_seconds": adapter.spec.retry_base_seconds,
                        "maximum_retries": adapter.spec.maximum_retries,
                        "minimum_retention_ratio": adapter.spec.minimum_retention_ratio,
                        "protected_source_allowances": sorted(
                            (item.contract_payload for item in adapter.spec.protected_source_allowances),
                            key=canonical_json_bytes,
                        ),
                    }
                    for adapter in self.adapters
                ],
                "download_contract": "cardrag.secure-pdf-download.v2",
                "ocr_contract": self.ocr.contract,
                "remote_ocr_cache": {
                    "mode": self.ocr.cache_mode,
                    "policy_version": "cardrag.remote-ocr-cache-access.v1",
                },
                "adoption_policy_version": self.ocr.adoption_policy_version,
                "structure": {
                    "schema_version": "cardrag.structure.v2",
                    "parser_profiles": parser_profiles,
                    "contextual_item_policy": contextual_item_policy_payload(),
                    "unclassified_fallback_policy": unclassified_fallback_policy_payload(),
                    "view_maximum_characters": V5_VIEW_MAXIMUM_CHARACTERS,
                },
                "revision_history": {
                    "policy_version": REVISION_HISTORY_POLICY_VERSION,
                    "unresolved_ledger_schema": UNRESOLVED_REVISION_LEDGER_SCHEMA,
                },
                "embedding": {
                    "profile_id": self.v5_profile.profile_id,
                    "cache_namespace": self.v5_profile.cache_namespace,
                    "provider": self.v5_profile.provider,
                    "provider_id": self.v5_profile.provider_id,
                    "model": self.v5_profile.model,
                    "dimension": self.v5_profile.dimension,
                    "dtype": self.v5_profile.dtype,
                    "normalization": self.v5_profile.normalization,
                    "document_policy": self.v5_profile.document_policy,
                    "query_policy": self.v5_profile.query_policy,
                    "maximum_tokens": self.v5_profile.maximum_tokens,
                    "endpoint_name": self.v5_profile.endpoint_name,
                    "endpoint_metadata_sha256": self.v5_profile.endpoint_metadata_sha256,
                    "tokenizer_revision": QWEN_TOKENIZER_REVISION,
                    "tokenizer_sha256": QWEN_TOKENIZER_SHA256,
                    "truncation": self.v5_profile.truncation_policy,
                },
                "retrieval": self.v5_retrieval_policy,
            }
            if self.document_aggregation is not None:
                contract_payload["document_aggregation_profile_artifact_sha256"] = (
                    self.document_aggregation.artifact_sha256
                )
            return canonical_sha256(contract_payload)
        return canonical_sha256(
            {
                "schema_version": "cardrag.worker-contract.v2",
                "serving_schema": SERVING_SCHEMA_ID,
                "issuer_adapters": [
                    {
                        "code": adapter.spec.code,
                        "display_name": adapter.spec.display_name,
                        "sort_order": adapter.spec.sort_order,
                        "parser_version": adapter.parser_version,
                        "allowed_hosts": sorted(adapter.spec.allowed_hosts),
                        "categories": list(adapter.spec.categories),
                        "minimum_records": adapter.spec.minimum_records,
                        "minimum_interval_seconds": adapter.spec.minimum_interval_seconds,
                        "retry_base_seconds": adapter.spec.retry_base_seconds,
                        "maximum_retries": adapter.spec.maximum_retries,
                        "minimum_retention_ratio": adapter.spec.minimum_retention_ratio,
                        "protected_source_allowances": sorted(
                            (item.contract_payload for item in adapter.spec.protected_source_allowances),
                            key=canonical_json_bytes,
                        ),
                    }
                    for adapter in self.adapters
                ],
                "download_contract": "cardrag.secure-pdf-download.v2",
                "ocr_contract": self.ocr.contract,
                "adoption_policy_version": self.ocr.adoption_policy_version,
                "chunk_contract": {
                    "version": CHUNK_CONTRACT,
                    "maximum_chars": 1600,
                    "overlap_chars": 160,
                },
                "embedding": {
                    "provider": self.embeddings.provider,
                    "model": self.embeddings.model,
                    "dimension": self.embeddings.dimension,
                    "input_policy_version": EMBEDDING_POLICY_VERSION,
                    "document_prefix": DOCUMENT_EMBEDDING_PREFIX,
                    "query_prefix": QUERY_EMBEDDING_PREFIX,
                },
            }
        )

    @property
    def v5_retrieval_policy(self) -> Mapping[str, object]:
        """Preserve M0 exactly, or return the core-sealed M1 retrieval contract."""

        if self.document_aggregation is None:
            return V5_RETRIEVAL_POLICY
        return sealed_v5_retrieval_policy(
            self.document_aggregation.profile,
            self.document_aggregation.profile_sha256,
        )

    @property
    def _seal_pdf_cache_objects_root(self) -> Path:
        return self.pdf_cache.objects_root

    async def _validated_document_aggregation_head(self) -> GenerationManifest:
        """Rebind the preflighted provider contract to the current M0/M1 head."""

        selected = self.document_aggregation
        if selected is None:
            raise RuntimeError("document aggregation head validation was not configured")
        return await validate_document_aggregation_head(
            self.webdav,
            selected,
            expected_m1_contract_sha256=self.contract_sha256,
        )

    async def _finite_stage(
        self,
        *,
        run_id: str,
        document_id: str,
        name: str,
        operation: Callable[[], Awaitable[T]],
        maximum_attempts: int | None = None,
        retry_base_seconds: float = 1.0,
        non_retryable_predicate: Callable[[Exception], bool] | None = None,
        non_retryable_error_formatter: Callable[[Exception], str] | None = None,
        error_formatter: Callable[[Exception], str] | None = None,
    ) -> T:
        maximum = maximum_attempts or self.maximum_attempts
        self.state.ensure_stage(run_id, document_id, name, max_attempts=maximum)
        row = self.state.get_stage(run_id, document_id, name)
        if row is not None and row.status == "succeeded":
            return await operation()
        while True:
            attempt = self.state.stage_started(run_id, document_id, name)
            retry_after: float | None = None
            try:
                result = await operation()
            except Exception as exc:
                if non_retryable_predicate is not None and non_retryable_predicate(exc):
                    if non_retryable_error_formatter is not None:
                        try:
                            self.state.stage_terminal_failed(
                                run_id,
                                document_id,
                                name,
                                non_retryable_error_formatter(exc),
                            )
                        except Exception:
                            raise RuntimeError("stage failure bookkeeping failed") from None
                    raise
                try:
                    delay = retry_delay(
                        attempt,
                        base_seconds=retry_base_seconds,
                        cap_seconds=self.retry_cap_seconds,
                    )
                    stored_error = (
                        error_formatter(exc) if error_formatter is not None else _safe_stage_error(exc)
                    )
                    status = self.state.stage_failed(
                        run_id,
                        document_id,
                        name,
                        stored_error,
                        delay_seconds=delay,
                    )
                except Exception:
                    raise RuntimeError("stage failure bookkeeping failed") from None
                if status == "failed":
                    raise
                retry_after = delay
            else:
                self.state.stage_succeeded(run_id, document_id, name)
                return result
            if retry_after is None:
                raise RuntimeError("retrying stage lost its backoff delay")
            # Keep cancellation during backoff outside the exception handler so
            # the prior provider exception cannot become CancelledError context.
            await asyncio.sleep(retry_after)

    async def run(self, *, resume_run_id: str | None = None) -> PipelineResult:
        with worker_lock(self.state_dir / "worker.lock"):
            if self.document_aggregation is not None:
                # Rebind the complete live-provider Worker contract before a
                # run row or retention cleanup can mutate candidate state.
                await self._validated_document_aggregation_head()
            run_id = resume_run_id or self.state.start_run()
            self.state.mark_stale_running_runs_interrupted(exclude_run_id=run_id)
            if resume_run_id:
                self.state.assert_resumable(run_id)
            self._cleanup_local_runs_safely(exclude_run_id=run_id, phase="before_run")
            cancellation_requested = False
            unexpected_failure: WorkerUnexpectedFailureError | None = None
            try:
                result = await self._run_locked(
                    run_id,
                    refresh_sources=resume_run_id is not None,
                )
            except asyncio.CancelledError:
                reconciliation = asyncio.create_task(self._reconcile_cancelled_publication(run_id))
                while not reconciliation.done():
                    try:
                        await asyncio.shield(reconciliation)
                    except asyncio.CancelledError:
                        # A repeated service-stop request must not release the
                        # worker lock before publication truth is reconciled.
                        continue
                    except BaseException:
                        break
                try:
                    published = reconciliation.result()
                except BaseException:
                    # Retrieve every child outcome, keep diagnostics secret
                    # safe, and conservatively retain interrupted truth.
                    published = False
                    LOGGER.error("Cancelled publication reconciliation failed")
                if not published:
                    self.state.finish_run_if_running(
                        run_id,
                        "interrupted",
                        error="worker_cancelled: Pipeline execution was interrupted.",
                    )
                cancellation_requested = True
            except (
                OCRDocumentFailuresError,
                OCRFailureBookkeepingError,
                OCRSystemicFailureError,
                ProtectedDocumentError,
                StructureDocumentFailuresError,
                V5CapacityError,
            ) as exc:
                self.state.finish_run_if_running(run_id, "failed", error=str(exc))
                raise
            except WorkerUnexpectedFailureError as exc:
                self.state.finish_run_if_running(run_id, "failed", error=exc.stored_error)
                raise
            except Exception as exc:
                category, status_code, error_number = _classify_worker_failure(exc)
                failure = WorkerUnexpectedFailureRecord(
                    run_id=run_id,
                    occurred_at=datetime.now(UTC),
                    error_class_category=category,
                    phase=exc.phase if isinstance(exc, _WorkerPhaseFailure) else None,
                    status_code=status_code,
                    errno=error_number,
                )
                report_path = self.state_dir / "runs" / run_id / "reports" / "worker-failure.json"
                error = WorkerUnexpectedFailureError(
                    run_id=run_id,
                    report_path=report_path,
                    failure=failure,
                )
                try:
                    _write_worker_failure_report(report_path, failure=failure)
                except Exception:
                    # The fixed DB/CLI error remains safe even if diagnostic
                    # storage itself is unavailable.
                    LOGGER.error("Worker failure report could not be written")
                self.state.finish_run_if_running(run_id, "failed", error=error.stored_error)
                unexpected_failure = error
            if cancellation_requested:
                # Normalize arbitrary provider/transport CancelledError args,
                # notes and causes after the source handler has exited.
                raise asyncio.CancelledError() from None
            if unexpected_failure is not None:
                # Do not retain the source exception object as implicit context
                # on the bounded worker error exposed to CLI callers.
                raise unexpected_failure from None
            gc_status: str | None = None
            gc_deleted = 0
            gc_error: str | None = None
            if self.collect_remote_garbage and self.webdav.channel == "stable":
                try:
                    from .gc import GCPartialFailure, collect_garbage

                    gc_result = await collect_garbage(
                        webdav=self.webdav,
                        state=self.state,
                        apply=True,
                        retain_generations=self.retained_generations,
                        grace_days=self.garbage_grace_days,
                        pointer_path=self.webdav.pointer_path,
                    )
                    gc_status = "succeeded"
                    gc_deleted = len(gc_result.deleted)
                except GCPartialFailure as exc:
                    try:
                        deleted_count: object = exc.deleted_count
                    except Exception:
                        deleted_count = None
                    if type(deleted_count) is int and deleted_count >= 1:
                        gc_deleted = deleted_count
                        gc_error = REMOTE_GC_PARTIAL_ERROR
                    else:
                        gc_error = REMOTE_GC_ERROR
                    gc_status = "failed"
                    LOGGER.error("Remote garbage collection stopped after partial deletion")
                except Exception:
                    # Publication/no-change is already durable. GC is fail-closed
                    # and reported independently rather than rewriting run truth.
                    gc_status = "failed"
                    gc_error = REMOTE_GC_ERROR
                    LOGGER.error("Remote garbage collection failed after durable run completion")
            elif self.collect_remote_garbage:
                gc_status = "skipped_candidate"
            self._cleanup_local_runs_safely(exclude_run_id=run_id, phase="after_run")
            return replace(
                result,
                unsupported_document_count=self.state.stage_status_count(run_id, "download", "skipped"),
                gc_status=gc_status,
                gc_deleted=gc_deleted,
                gc_error=gc_error,
            )

    def _cleanup_local_runs_safely(self, *, exclude_run_id: str, phase: str) -> None:
        try:
            self._cleanup_local_runs(exclude_run_id=exclude_run_id)
        except Exception:
            # Retention cleanup is diagnostic-only and runs outside durable
            # publication truth. Never leak a raw filesystem/SQLite exception
            # or turn a successful/no-change publication into a failed exit.
            LOGGER.error(
                "reason_code=local_run_cleanup_failed safe_error=%s phase=%s; continuing",
                LOCAL_RUN_CLEANUP_ERROR,
                phase,
            )

    def _cleanup_local_runs(self, *, exclude_run_id: str) -> None:
        runs_root = self.state_dir / "runs"
        if not runs_root.exists() or runs_root.is_symlink() or not runs_root.is_dir():
            return
        retained = set(self.state.retained_publication_run_ids(limit=self.retained_generations))
        removable = set(self.state.completed_run_ids())
        removable.update(self.state.prunable_incomplete_run_ids(keep=self.retained_incomplete_runs))
        for run_id in sorted(removable):
            if run_id == exclude_run_id:
                continue
            if run_id in retained:
                continue
            candidate = runs_root / run_id
            if candidate.is_symlink() or not candidate.exists():
                continue
            resolved = candidate.resolve(strict=True)
            if resolved.parent != runs_root.resolve(strict=True):
                continue
            shutil.rmtree(resolved)

    async def _reconcile_cancelled_publication(self, run_id: str) -> bool:
        """Record an exact stable commit completed immediately before cancellation."""

        try:
            seal_path = self.state_dir / "runs" / run_id / "sealed" / "publish.json"
            if seal_path.is_symlink() or not seal_path.is_file():
                return False
            sealed = json.loads(seal_path.read_bytes())
            if not isinstance(sealed, dict) or str(sealed.get("run_id") or "") != run_id:
                return False
            validated = await self._validate_local_seal(sealed)
            manifest = validated.manifest
            if await self._reconcile_remote_bundle(manifest) is None:
                return False
            self.state.record_publish(
                generation_id=manifest.generation_id,
                run_id=run_id,
                corpus_sha256=manifest.corpus_sha256,
                contract_sha256=manifest.contract_sha256,
                serving_sha256=manifest.serving_database.sha256,
                status="ready",
                details={"manifest_sha256": manifest.manifest_sha256},
            )
            self.state.finish_run_if_running(
                run_id,
                "succeeded",
                corpus_sha256=manifest.corpus_sha256,
                contract_sha256=manifest.contract_sha256,
            )
            return True
        except Exception:
            # Cancellation reconciliation is a strict positive proof.  Any
            # local/remote uncertainty falls back to interrupted without raw
            # path, transport, or response details entering logs/state.
            LOGGER.error("Cancelled publication could not be reconciled exactly")
            return False

    async def _reconcile_remote_bundle(self, manifest: GenerationManifest) -> PublishedBundle | None:
        """Return a bundle only when the stable remote truth exactly matches a local manifest."""

        try:
            current = await self.webdav.validated_current_generation()
            expected_failed_documents = sum(
                document.availability == "ocr_failed" for document in manifest.documents
            )
            if current is None or (
                current.generation_id != manifest.generation_id
                or current.corpus_sha256 != manifest.corpus_sha256
                or current.contract_sha256 != manifest.contract_sha256
                or current.ocr_failed_document_count != expected_failed_documents
            ):
                return None
            remote_manifest = await self.webdav.get_bytes(generation_manifest_path(manifest.generation_id))
            if remote_manifest != manifest.canonical_bytes():
                return None
            return PublishedBundle(
                generation_id=manifest.generation_id,
                index_sha256=manifest.serving_database.sha256,
                manifest_sha256=manifest.manifest_sha256,
            )
        except Exception:
            # Exact positive proof is required. Raw transport/path details are
            # neither logged nor retained in an exception chain.
            return None

    async def _run_locked(self, run_id: str, *, refresh_sources: bool = False) -> PipelineResult:
        run_dir = self.state_dir / "runs" / run_id
        seal_path = run_dir / "sealed" / "publish.json"
        deferred_seal: Mapping[str, Any] | None = None
        if seal_path.exists():
            if seal_path.is_symlink() or not seal_path.is_file():
                raise RuntimeError("resume publication seal is not a regular file")
            sealed = json.loads(seal_path.read_text(encoding="utf-8"))
            if not isinstance(sealed, dict):
                raise RuntimeError("resume publication seal is not a JSON object")
            deferred_seal = sealed

        snapshots = []
        async with httpx.AsyncClient(follow_redirects=False, timeout=60) as client:
            for adapter in self.adapters:
                limited = RateLimitedClient(client, self.limiters[adapter.spec.code])

                async def discover(
                    current_adapter: IssuerAdapter = adapter,
                    current_client: RateLimitedClient = limited,
                ) -> SourceSnapshot:
                    return await current_adapter.discover_current(current_client)  # type: ignore[arg-type]

                stage = self.state.get_stage(run_id, f"issuer-{adapter.spec.code}", "discovery")
                stored = self.state.run_snapshot(run_id, adapter.spec.code)
                if (
                    not refresh_sources
                    and stage is not None
                    and stage.status == "succeeded"
                    and stored is not None
                ):
                    snapshot = _restore_snapshot(
                        stored[0],
                        observed_at=stored[1],
                        expected_issuer=adapter.spec.code,
                        expected_parser_version=adapter.parser_version,
                    )
                else:
                    snapshot = await self._finite_stage(
                        run_id=run_id,
                        document_id=f"issuer-{adapter.spec.code}",
                        name="discovery",
                        operation=discover,
                        maximum_attempts=adapter.spec.maximum_retries,
                        retry_base_seconds=adapter.spec.retry_base_seconds,
                    )
                if len(snapshot.records) < adapter.spec.minimum_records:
                    raise RuntimeError(
                        f"{snapshot.issuer} discovery returned {len(snapshot.records)} records; "
                        f"minimum is {adapter.spec.minimum_records}"
                    )
                baseline = self.state.last_successful_snapshot_count(snapshot.issuer)
                if (
                    baseline is not None
                    and len(snapshot.records) < baseline * adapter.spec.minimum_retention_ratio
                ):
                    raise RuntimeError(
                        f"{snapshot.issuer} discovery count {len(snapshot.records)} fell below "
                        f"{adapter.spec.minimum_retention_ratio:.2f} of successful baseline {baseline}"
                    )
                snapshots.append(snapshot)
                LOGGER.info(
                    "discovery completed issuer=%s records=%d warnings=%d",
                    snapshot.issuer,
                    len(snapshot.records),
                    len(snapshot.warnings),
                )
                self.state.record_snapshot(
                    run_id=run_id,
                    snapshot_id=snapshot.snapshot_id,
                    issuer=snapshot.issuer,
                    source_sha256=snapshot.snapshot_id,
                    record_count=len(snapshot.records),
                    payload=snapshot.payload,
                )
        records = select_current([record for snapshot in snapshots for record in snapshot.records])
        contract_sha256 = self.contract_sha256

        # Current PDF bytes are part of corpus identity. Discovery-only hashes are
        # insufficient because issuer URLs are sometimes reused for changed files.
        acquired: list[_AcquiredDocument] = []
        unsupported: list[UnsupportedProductRecord] = []
        pdf_cache_hits: set[str] = set()
        pdf_cache_misses: set[str] = set()
        pdf_downloads: set[str] = set()
        pdf_revisions: set[str] = set()
        pdf_cache_revalidations: set[str] = set()
        pdf_cache_not_modified: set[str] = set()

        async def finalize_pdf_activity(result: PipelineResult) -> PipelineResult:
            prune_status = "succeeded"
            pruned_objects = 0
            pruned_bytes = 0
            prune_error: str | None = None
            try:
                protected_sha256s = {item.pdf.sha256 for item in acquired}
                if self.v5_profile is not None:
                    protected_sha256s.update(load_v109_seed_pins(self.state_dir))
                retained_run_ids = self.state.retained_publication_run_ids(limit=self.retained_generations)
                runs_root = self.state_dir / "runs"
                if retained_run_ids:
                    try:
                        runs_root_mode = runs_root.lstat().st_mode
                    except FileNotFoundError as exc:
                        raise RuntimeError("retained publication root is unavailable") from exc
                    if stat.S_ISLNK(runs_root_mode) or not stat.S_ISDIR(runs_root_mode):
                        raise RuntimeError("retained publication root is unsafe")
                for retained_run_id in retained_run_ids:
                    if not retained_run_id or Path(retained_run_id).name != retained_run_id:
                        raise RuntimeError("retained publication run identity is unsafe")
                    run_root = runs_root / retained_run_id
                    sealed_root = run_root / "sealed"
                    seal = sealed_root / "publish.json"
                    try:
                        run_mode = run_root.lstat().st_mode
                        sealed_mode = sealed_root.lstat().st_mode
                        seal_stat = seal.lstat()
                    except FileNotFoundError as exc:
                        raise RuntimeError("retained publication seal is unavailable") from exc
                    if (
                        stat.S_ISLNK(run_mode)
                        or not stat.S_ISDIR(run_mode)
                        or stat.S_ISLNK(sealed_mode)
                        or not stat.S_ISDIR(sealed_mode)
                        or stat.S_ISLNK(seal_stat.st_mode)
                        or not stat.S_ISREG(seal_stat.st_mode)
                    ):
                        raise RuntimeError("retained publication seal is unavailable or unsafe")
                    if seal.resolve(strict=True).parent.parent.parent != runs_root.resolve(strict=True):
                        raise RuntimeError("retained publication seal escapes worker storage")
                    sealed = json.loads(seal.read_text(encoding="utf-8"))
                    if not isinstance(sealed, dict) or str(sealed.get("run_id") or "") != retained_run_id:
                        raise RuntimeError("retained publication seal has the wrong run identity")
                    validated = await self._validate_local_seal(sealed)
                    protected_sha256s.update(
                        document.pdf.sha256
                        for document in validated.manifest.documents
                        if document.pdf is not None
                    )
                pruned = await to_thread_fenced(self.pdf_cache.prune, protected_sha256s)
                pruned_objects = pruned.deleted_objects
                pruned_bytes = pruned.deleted_bytes
            except PDFCachePruneError as exc:
                # Preserve only constructor-bounded numeric progress. The raw
                # filesystem error is not a result or logging trust boundary.
                try:
                    deleted_objects: object = exc.deleted_objects
                    deleted_bytes: object = exc.deleted_bytes
                except Exception:
                    deleted_objects = None
                    deleted_bytes = None
                if (
                    type(deleted_objects) is int
                    and deleted_objects >= 1
                    and type(deleted_bytes) is int
                    and deleted_bytes >= 0
                ):
                    pruned_objects = deleted_objects
                    pruned_bytes = deleted_bytes
                prune_status = "failed"
                prune_error = PDF_CACHE_PRUNE_ERROR
                LOGGER.error("Local PDF cache pruning stopped after partial deletion")
            except Exception:
                # The run/publication is already durable. Cache pruning is a
                # fail-closed storage optimization and never rewrites run truth.
                prune_status = "failed"
                prune_error = PDF_CACHE_PRUNE_ERROR
                LOGGER.error("Local PDF cache pruning failed after durable run completion")
            return replace(
                result,
                pdf_cache_hits=len(pdf_cache_hits),
                pdf_cache_misses=len(pdf_cache_misses),
                pdf_downloads=len(pdf_downloads),
                pdf_revisions=len(pdf_revisions),
                pdf_cache_revalidations=len(pdf_cache_revalidations),
                pdf_cache_not_modified=len(pdf_cache_not_modified),
                pdf_cache_prune_status=prune_status,
                pdf_cache_pruned_objects=pruned_objects,
                pdf_cache_pruned_bytes=pruned_bytes,
                pdf_cache_prune_error=prune_error,
            )

        def log_pdf_progress(completed: int) -> None:
            if completed % 25 == 0 or completed == len(records):
                LOGGER.info(
                    "PDF progress completed=%d total=%d cache_hits=%d cache_misses=%d "
                    "downloads=%d revisions=%d revalidations=%d not_modified=%d unsupported=%d",
                    completed,
                    len(records),
                    len(pdf_cache_hits),
                    len(pdf_cache_misses),
                    len(pdf_downloads),
                    len(pdf_revisions),
                    len(pdf_cache_revalidations),
                    len(pdf_cache_not_modified),
                    len(unsupported),
                )

        async with httpx.AsyncClient(follow_redirects=False, timeout=60) as client:
            for pdf_index, source in enumerate(records, start=1):
                adapter = next(item for item in self.adapters if item.spec.code == source.issuer)
                limited = RateLimitedClient(client, self.limiters[adapter.spec.code])
                source_key = source.source_id
                cache_identity = PDFSourceIdentity.from_source_record(source)

                async def acquire(
                    adapter: IssuerAdapter = adapter,
                    source: SourceRecord = source,
                    identity: PDFSourceIdentity = cache_identity,
                    current_client: RateLimitedClient = limited,
                ) -> DownloadedPDF:
                    cached = self.pdf_cache.lookup(identity)
                    checked_at = datetime.now(UTC)
                    cache_age = None if cached is None else checked_at - cached.origin_checked_at
                    if (
                        cached is not None
                        and cache_age is not None
                        and timedelta(0) <= cache_age < self.pdf_cache_refresh_interval
                    ):
                        pdf_cache_hits.add(source.source_id)
                        LOGGER.debug(
                            "PDF cache hit issuer=%s product_code=%s source_id=%s sha256=%s",
                            source.issuer,
                            source.product_code,
                            source.source_id,
                            cached.sha256,
                        )
                        return cached.as_downloaded_pdf()

                    if cached is None:
                        pdf_cache_misses.add(source.source_id)
                    else:
                        pdf_cache_hits.add(source.source_id)
                        pdf_cache_revalidations.add(source.source_id)
                    previous = self.state.pdf_cache_source_binding(source.source_id)
                    LOGGER.debug(
                        "PDF cache %s issuer=%s product_code=%s source_id=%s",
                        "miss" if cached is None else "revalidation",
                        source.issuer,
                        source.product_code,
                        source.source_id,
                    )
                    request = await adapter.prepare_download(current_client, source)  # type: ignore[arg-type]
                    if cached is not None and (cached.etag is not None or cached.last_modified is not None):
                        headers = {
                            key: value
                            for key, value in request.headers.items()
                            if key.casefold() not in {"if-none-match", "if-modified-since"}
                        }
                        if cached.etag is not None:
                            headers["If-None-Match"] = cached.etag
                        if cached.last_modified is not None:
                            headers["If-Modified-Since"] = cached.last_modified
                        request = replace(request, headers=headers)
                    downloader = SecurePDFDownloader(DownloadPolicy(allowed_hosts=adapter.spec.allowed_hosts))
                    with self.pdf_cache.temporary_download_path() as destination:
                        try:
                            downloaded = await downloader.download(
                                current_client,  # type: ignore[arg-type]
                                request,
                                destination,
                            )
                        except PDFNotModified as not_modified:
                            if cached is None:  # pragma: no cover - downloader guards this too
                                raise RuntimeError("origin returned not-modified for a cache miss") from None
                            observed_at = datetime.now(UTC)
                            ingested = self.pdf_cache.observe_not_modified(
                                identity,
                                cached,
                                final_url=not_modified.final_url,
                                etag=not_modified.etag,
                                last_modified=not_modified.last_modified,
                                observed_at=observed_at,
                                verified_at=observed_at,
                            )
                            pdf_cache_not_modified.add(source.source_id)
                            LOGGER.debug(
                                "PDF cache origin not modified issuer=%s product_code=%s "
                                "source_id=%s sha256=%s",
                                source.issuer,
                                source.product_code,
                                source.source_id,
                                ingested.sha256,
                            )
                            return ingested.as_downloaded_pdf()
                        observed_at = datetime.now(UTC)
                        ingested = self.pdf_cache.ingest_download(
                            identity,
                            downloaded,
                            observed_at=observed_at,
                            verified_at=observed_at,
                        )
                    pdf_downloads.add(source.source_id)
                    if previous is not None and previous.pdf_sha256 != ingested.sha256:
                        pdf_revisions.add(source.source_id)
                        LOGGER.info(
                            "PDF revision detected issuer=%s product_code=%s source_id=%s "
                            "previous_sha256=%s current_sha256=%s",
                            source.issuer,
                            source.product_code,
                            source.source_id,
                            previous.pdf_sha256,
                            ingested.sha256,
                        )
                    else:
                        LOGGER.debug(
                            "PDF download cached issuer=%s product_code=%s source_id=%s sha256=%s",
                            source.issuer,
                            source.product_code,
                            source.source_id,
                            ingested.sha256,
                        )
                    return ingested.as_downloaded_pdf()

                allowance = next(
                    (
                        item
                        for item in adapter.spec.protected_source_allowances
                        if item.source_id == source_key
                    ),
                    None,
                )

                def expected_protected(
                    exc: Exception,
                    current_source: SourceRecord = source,
                    current_allowance: ProtectedSourceAllowance | None = allowance,
                ) -> bool:
                    return (
                        isinstance(exc, ProtectedDocumentError)
                        and current_allowance is not None
                        and current_allowance.product_code == current_source.product_code
                        and current_allowance.source_version == current_source.source_version
                        and current_allowance.source_url == current_source.source_url
                        and current_allowance.magic == exc.magic
                        and current_allowance.sha256 == exc.sha256
                        and current_allowance.size_bytes == exc.size_bytes
                    )

                try:
                    pdf = await self._finite_stage(
                        run_id=run_id,
                        document_id=source_key,
                        name="download",
                        operation=acquire,
                        maximum_attempts=adapter.spec.maximum_retries,
                        retry_base_seconds=adapter.spec.retry_base_seconds,
                        non_retryable_predicate=expected_protected if allowance is not None else None,
                    )
                except ProtectedDocumentError as exc:
                    if not expected_protected(exc):
                        raise
                    self.state.stage_skipped(
                        run_id,
                        source_key,
                        "download",
                        f"unsupported_drm {source.issuer}/{source.product_code}: {exc}",
                    )
                    unsupported.append(
                        UnsupportedProductRecord(
                            source=source,
                            protected_sha256=exc.sha256,
                            protected_size_bytes=exc.size_bytes,
                            protected_magic=exc.magic,
                        )
                    )
                    log_pdf_progress(pdf_index)
                    continue
                acquired.append(_AcquiredDocument(source, pdf))
                log_pdf_progress(pdf_index)

        unresolved_revision_entries: list[UnresolvedRevisionIdentityV5] = []
        historical_pdf_cache_hits = 0
        if self.v5_profile is not None:
            current_acquired = tuple(acquired)
            known_sources = _known_snapshot_sources(
                self.state,
                self.adapters,
                tuple(item.source for item in current_acquired),
            )
            materialized_by_document: dict[str, _AcquiredDocument] = {}
            for current_document in current_acquired:
                history = self.state.pdf_cache_lineage_history(
                    issuer=current_document.source.issuer,
                    product_code=current_document.source.product_code,
                    document_type=current_document.source.document_type,
                )
                history_plan = plan_revision_history_v5(
                    current_source=current_document.source,
                    current_pdf_sha256=current_document.pdf.sha256,
                    rows=history,
                    known_sources=known_sources,
                )
                unresolved_revision_entries.extend(history_plan.unresolved_revisions)
                for candidate in history_plan.candidates:
                    is_current = (
                        candidate.source.source_id == current_document.source.source_id
                        and candidate.revision.pdf_sha256 == current_document.pdf.sha256
                    )
                    if is_current:
                        downloaded = current_document.pdf
                    else:
                        cached = self.pdf_cache.lookup_revision(candidate.revision)
                        if cached is None:
                            unresolved_revision_entries.append(
                                UnresolvedRevisionIdentityV5(
                                    source_id=candidate.source.source_id,
                                    pdf_sha256=candidate.revision.pdf_sha256,
                                    reason_code="pdf_cache_object_unavailable",
                                )
                            )
                            continue
                        downloaded = cached.as_downloaded_pdf()
                        historical_pdf_cache_hits += 1
                    planned = _AcquiredDocument(
                        source=candidate.source,
                        pdf=downloaded,
                        temporal_status=candidate.temporal_status,
                        supersedes_document_id=candidate.supersedes_document_id,
                        is_historical=not is_current,
                    )
                    document_id = candidate.document_id
                    existing_document = materialized_by_document.get(document_id)
                    if existing_document is not None:
                        # The legacy document_id omits metadata-only source
                        # differences. Keep the proven current revision and
                        # report the historical identity as unresolved rather
                        # than mixing two contracts in one run directory.
                        existing_identity = (
                            existing_document.source.source_id,
                            existing_document.pdf.sha256,
                        )
                        planned_identity = (planned.source.source_id, planned.pdf.sha256)
                        if existing_identity == planned_identity:
                            if (
                                existing_document.temporal_status != planned.temporal_status
                                or existing_document.supersedes_document_id != planned.supersedes_document_id
                            ):
                                raise RuntimeError(
                                    "duplicate revision identity has conflicting temporal truth"
                                )
                            continue
                        dropped = existing_document if planned.temporal_status == "current" else planned
                        unresolved_revision_entries.append(
                            UnresolvedRevisionIdentityV5(
                                source_id=dropped.source.source_id,
                                pdf_sha256=dropped.pdf.sha256,
                                reason_code="document_identity_collision",
                            )
                        )
                        if planned.temporal_status == "current":
                            materialized_by_document[document_id] = planned
                        continue
                    materialized_by_document[document_id] = planned
            materialized_ids = set(materialized_by_document)
            acquired = [
                replace(
                    item,
                    supersedes_document_id=(
                        item.supersedes_document_id
                        if item.supersedes_document_id in materialized_ids
                        else None
                    ),
                )
                for item in sorted(
                    materialized_by_document.values(),
                    key=lambda row: (
                        row.source.issuer,
                        row.source.product_code,
                        row.source.effective_date,
                        row.source.source_version,
                        row.pdf.sha256,
                    ),
                )
            ]
        unresolved_revision_ledger = canonical_unresolved_revision_ledger_v5(unresolved_revision_entries)
        unresolved_revision_sha256 = unresolved_revision_ledger_sha256_v5(unresolved_revision_ledger)
        unsupported_payload = sorted(
            (item.payload for item in unsupported),
            key=canonical_json_bytes,
        )
        if self.v5_profile is not None:
            corpus_payload = _v5_corpus_identity_payload(
                acquired=acquired,
                unsupported_documents=unsupported_payload,
                unresolved_revisions=unresolved_revision_ledger,
                unresolved_revision_sha256=unresolved_revision_sha256,
            )
        else:
            # Preserve the v1-v4 canonical corpus bytes exactly.
            corpus_payload = {
                "schema_version": "cardrag.current-corpus.v2",
                "documents": [
                    {
                        "source": item.source.discovery_payload,
                        "pdf_sha256": item.pdf.sha256,
                        "pdf_size_bytes": item.pdf.size_bytes,
                        "page_count": item.pdf.page_count,
                    }
                    for item in acquired
                ],
                "unsupported_documents": unsupported_payload,
            }
        corpus_sha256 = canonical_sha256(corpus_payload)
        current_remote = await self.webdav.validated_current_generation()
        stable_body = await self.webdav.get_bytes(self.webdav.pointer_path)
        cache_healing_generation_id: str | None = None
        cache_healing_seal: dict[str, Any] | None = None
        cache_healing_seal_path: Path | None = None
        cache_healing_validated_seal: _ValidatedSeal | None = None
        if deferred_seal is not None:
            try:
                validated_resume_seal = await self._validate_local_seal(deferred_seal)
            except Exception:
                # A crash while replacing a superseded seal can leave its old
                # control file beside a new database. Fresh discovery has
                # already succeeded, so rebuild from content-addressed stages.
                validated_resume_seal = None
            if (
                validated_resume_seal is not None
                and validated_resume_seal.manifest.corpus_sha256 == corpus_sha256
                and validated_resume_seal.manifest.contract_sha256 == contract_sha256
                and (current_remote is None or current_remote.ocr_failed_document_count == 0)
            ):
                if str(deferred_seal.get("run_id") or "") != run_id:
                    raise RuntimeError("resume publication seal belongs to a different run")
                resume_seal_is_current_deferred = (
                    current_remote is not None
                    and current_remote.generation_id == validated_resume_seal.manifest.generation_id
                    and validated_resume_seal.ocr_cache_publication_deferred > 0
                )
                if not resume_seal_is_current_deferred:
                    return await finalize_pdf_activity(
                        await self._publish_sealed(
                            run_id,
                            deferred_seal,
                            validated=validated_resume_seal,
                        )
                    )
                assert current_remote is not None
                if await self._reconcile_remote_bundle(validated_resume_seal.manifest) is None:
                    raise RuntimeError(
                        "remote generation does not exactly match its retained OCR healing seal"
                    )
                # Stable activation can finish before the local run row is
                # terminalized. Recover that immutable publication truth, but
                # keep running so the per-document resolver can repair partial
                # native cache controls without trying to republish this same
                # generation ID with a changed manifest.
                self.state.record_publish(
                    generation_id=current_remote.generation_id,
                    run_id=run_id,
                    corpus_sha256=validated_resume_seal.manifest.corpus_sha256,
                    contract_sha256=validated_resume_seal.manifest.contract_sha256,
                    serving_sha256=validated_resume_seal.manifest.serving_database.sha256,
                    status="ready",
                    details={"manifest_sha256": validated_resume_seal.manifest.manifest_sha256},
                )
                cache_healing_generation_id = current_remote.generation_id
                cache_healing_seal = dict(deferred_seal)
                cache_healing_seal_path = seal_path
                cache_healing_validated_seal = validated_resume_seal
        existing = self.state.ready_publish(corpus_sha256, contract_sha256)
        current_is_exact_complete = current_remote is not None and (
            current_remote.corpus_sha256 == corpus_sha256
            and current_remote.contract_sha256 == contract_sha256
            and current_remote.ocr_failed_document_count == 0
        )
        if (
            current_remote is not None
            and current_is_exact_complete
            and cache_healing_generation_id is None
            and existing is not None
            and str(existing["generation_id"]) == current_remote.generation_id
        ):
            # A successful generation-only publication deliberately omits the
            # native cache binding. Its retained, strictly validated seal is the
            # durable signal that an otherwise exact no-change run must enter
            # OCRResolver once more to repair READY without calling the provider.
            prior_seal_path = self.state_dir / "runs" / str(existing["run_id"]) / "sealed" / "publish.json"
            if prior_seal_path.is_file() and not prior_seal_path.is_symlink():
                try:
                    candidate_seal = json.loads(prior_seal_path.read_text(encoding="utf-8"))
                    candidate_count = (
                        candidate_seal.get("ocr_cache_publication_deferred", 0)
                        if isinstance(candidate_seal, Mapping)
                        else 0
                    )
                    if type(candidate_count) is int and candidate_count > 0:
                        validated_prior_seal = await self._validate_local_seal(candidate_seal)
                    else:
                        validated_prior_seal = None
                except Exception:
                    # The remote generation is already fully validated. An
                    # unsafe optional local hint cannot suppress its no-change
                    # result or authorize cache repair.
                    validated_prior_seal = None
                if (
                    validated_prior_seal is not None
                    and str(candidate_seal.get("run_id") or "") == str(existing["run_id"])
                    and validated_prior_seal.manifest.generation_id == current_remote.generation_id
                    and validated_prior_seal.manifest.corpus_sha256 == corpus_sha256
                    and validated_prior_seal.manifest.contract_sha256 == contract_sha256
                    and validated_prior_seal.ocr_cache_publication_deferred > 0
                ):
                    if await self._reconcile_remote_bundle(validated_prior_seal.manifest) is None:
                        raise RuntimeError(
                            "remote generation does not exactly match its retained OCR healing seal"
                        )
                    cache_healing_generation_id = current_remote.generation_id
                    cache_healing_seal = dict(candidate_seal)
                    cache_healing_seal_path = prior_seal_path
                    cache_healing_validated_seal = validated_prior_seal
        if current_remote is not None and current_is_exact_complete and cache_healing_generation_id is None:
            self.state.finish_run(
                run_id,
                "no_change",
                corpus_sha256=corpus_sha256,
                contract_sha256=contract_sha256,
            )
            return await finalize_pdf_activity(
                PipelineResult(
                    run_id,
                    "no_change",
                    corpus_sha256,
                    contract_sha256,
                    current_remote.generation_id,
                    len(acquired),
                    0,
                )
            )
        if current_remote is None and stable_body is not None:
            raise RuntimeError("remote stable generation is corrupt; refusing publication")
        if existing is not None and current_remote is None:
            # Missing stable.json can be reconstructed from an exact seal.
            generation_id = str(existing["generation_id"])
            prior_seal_path = self.state_dir / "runs" / str(existing["run_id"]) / "sealed" / "publish.json"
            if not prior_seal_path.is_file() or prior_seal_path.is_symlink():
                raise RuntimeError(
                    "remote generation is missing and its sealed local publication artifact is unavailable"
                )
            prior_seal = json.loads(prior_seal_path.read_text(encoding="utf-8"))
            if (
                prior_seal.get("corpus_sha256") != corpus_sha256
                or prior_seal.get("contract_sha256") != contract_sha256
                or prior_seal.get("generation_id") != generation_id
            ):
                raise RuntimeError("stored publication row does not match its sealed artifact")
            _, validated_prior_seal = await self._publish_remote_only(prior_seal)
            self.state.finish_run(
                run_id,
                "no_change",
                corpus_sha256=corpus_sha256,
                contract_sha256=contract_sha256,
            )
            return await finalize_pdf_activity(
                PipelineResult(
                    run_id,
                    "no_change",
                    corpus_sha256,
                    contract_sha256,
                    generation_id,
                    len(acquired),
                    0,
                    ocr_cache_publication_deferred=(validated_prior_seal.ocr_cache_publication_deferred),
                    v5_metrics=validated_prior_seal.v5_metrics,
                )
            )
        # A different fully valid stable generation wins. Rebuild a new
        # generation for this recurrence and chain it to that current head;
        # OCR/embeddings are reused through their content caches below.

        prior_local_native_sources: dict[str, PriorLocalNativeSource] = {}
        if cache_healing_generation_id is not None:
            if (
                cache_healing_seal is None
                or cache_healing_seal_path is None
                or cache_healing_validated_seal is None
            ):
                raise RuntimeError("OCR cache healing lost its retained publication seal")
            retained_run_id = str(cache_healing_seal.get("run_id") or "")
            expected_seal_path = self.state_dir / "runs" / retained_run_id / "sealed" / "publish.json"
            try:
                if (
                    cache_healing_seal_path.is_symlink()
                    or not cache_healing_seal_path.is_file()
                    or cache_healing_seal_path.resolve(strict=True) != expected_seal_path.resolve(strict=True)
                    or cache_healing_seal_path.read_bytes() != canonical_json_bytes(cache_healing_seal)
                ):
                    raise RuntimeError("retained OCR cache healing seal is unavailable or unsafe")
            except (OSError, RuntimeError) as exc:
                raise RuntimeError("retained OCR cache healing seal is unavailable or unsafe") from exc
            # Revalidate immediately before deriving any local reuse source so
            # a retained object changed after the no-change decision cannot be
            # consumed under an earlier seal validation.
            cache_healing_validated_seal = await self._validate_local_seal(cache_healing_seal)
            healing_manifest = cache_healing_validated_seal.manifest
            if (
                healing_manifest.generation_id != cache_healing_generation_id
                or healing_manifest.corpus_sha256 != corpus_sha256
                or healing_manifest.contract_sha256 != contract_sha256
            ):
                raise RuntimeError("retained OCR cache healing seal identity changed")
            manifest_documents = {document.document_id: document for document in healing_manifest.documents}
            acquired_by_document_id = {item.source.document_id(item.pdf.sha256): item for item in acquired}
            if (
                len(acquired_by_document_id) != len(acquired)
                or set(manifest_documents) != set(acquired_by_document_id)
                or any(document.availability != "available" for document in manifest_documents.values())
            ):
                raise RuntimeError("retained OCR cache healing document set is not exact")
            unbound_documents = tuple(
                document
                for document in healing_manifest.documents
                if document.ocr_cache_kind is None and document.ocr_reuse_key is None
            )
            if len(unbound_documents) != cache_healing_validated_seal.ocr_cache_publication_deferred:
                raise RuntimeError("retained OCR cache healing deferred document set is ambiguous")
            sealed_ocr_objects = {
                (path, digest)
                for path, media_type, digest, _size in cache_healing_validated_seal.objects
                if media_type == "text/markdown; charset=utf-8"
            }
            runs_root = self.state_dir / "runs"
            for document in unbound_documents:
                acquired_document = acquired_by_document_id[document.document_id]
                pdf = acquired_document.pdf
                source = acquired_document.source
                if (
                    document.issuer != source.issuer
                    or document.pdf.sha256 != pdf.sha256
                    or document.pdf.size_bytes != pdf.size_bytes
                    or document.page_count != pdf.page_count
                    or document.ocr is None
                ):
                    raise RuntimeError("retained OCR cache healing document identity is invalid")
                prior_ocr_path = (
                    runs_root / retained_run_id / "documents" / document.document_id / "ocr" / "ocr.md"
                )
                try:
                    resolved_prior_ocr_path = prior_ocr_path.resolve(strict=True)
                except (OSError, RuntimeError) as exc:
                    raise RuntimeError("retained OCR cache healing OCR artifact is unavailable") from exc
                if (resolved_prior_ocr_path, document.ocr.sha256) not in sealed_ocr_objects:
                    raise RuntimeError("retained OCR cache healing OCR artifact is not seal-bound")
                prior_local_native_sources[document.document_id] = PriorLocalNativeSource(
                    runs_root=runs_root,
                    run_id=retained_run_id,
                    generation_id=healing_manifest.generation_id,
                    corpus_sha256=healing_manifest.corpus_sha256,
                    contract_sha256=healing_manifest.contract_sha256,
                    document_id=document.document_id,
                    pdf_sha256=document.pdf.sha256,
                    pdf_size_bytes=document.pdf.size_bytes,
                    page_count=document.page_count,
                    ocr_sha256=document.ocr.sha256,
                    ocr_size_bytes=document.ocr.size_bytes,
                )

        # Cross-run local OCR cache lookup:
        # If corpus changed or documents are unbound from the healing generation,
        # lookup validated OCR artifacts from retained previous runs so we do not
        # re-run expensive OCR across thousands of existing unchanged documents.
        acquired_by_doc_id = {item.source.document_id(item.pdf.sha256): item for item in acquired}
        unbound_doc_ids = set(acquired_by_doc_id.keys()) - set(prior_local_native_sources.keys())
        if unbound_doc_ids:
            candidate_run_ids: list[str] = []
            try:
                candidate_run_ids.extend(
                    self.state.retained_publication_run_ids(limit=max(self.retained_generations, 5))
                )
            except Exception:
                pass
            try:
                candidate_run_ids.extend(reversed(self.state.completed_run_ids()))
            except Exception:
                pass

            seen_runs: set[str] = set()
            runs_root = self.state_dir / "runs"
            for candidate_run_id in candidate_run_ids:
                if not unbound_doc_ids:
                    break
                if candidate_run_id in seen_runs or candidate_run_id == run_id:
                    continue
                seen_runs.add(candidate_run_id)

                candidate_seal_path = runs_root / candidate_run_id / "sealed" / "publish.json"
                if not candidate_seal_path.is_file() or candidate_seal_path.is_symlink():
                    continue

                try:
                    candidate_seal = json.loads(candidate_seal_path.read_text(encoding="utf-8"))
                    validated_seal = await self._validate_local_seal(candidate_seal)
                except Exception:
                    continue

                c_manifest = validated_seal.manifest
                sealed_ocr_objects = {
                    (path, digest)
                    for path, media_type, digest, _size in validated_seal.objects
                    if media_type == "text/markdown; charset=utf-8"
                }

                c_docs_by_id = {d.document_id: d for d in c_manifest.documents}
                matched_in_this_run = 0
                for doc_id in list(unbound_doc_ids):
                    c_doc = c_docs_by_id.get(doc_id)
                    if c_doc is None:
                        continue
                    acquired_item = acquired_by_doc_id[doc_id]
                    pdf = acquired_item.pdf
                    source = acquired_item.source
                    if (
                        c_doc.availability != "available"
                        or c_doc.ocr is None
                        or c_doc.issuer != source.issuer
                        or c_doc.pdf.sha256 != pdf.sha256
                        or c_doc.pdf.size_bytes != pdf.size_bytes
                        or c_doc.page_count != pdf.page_count
                    ):
                        continue

                    prior_ocr_path = (
                        runs_root / candidate_run_id / "documents" / doc_id / "ocr" / "ocr.md"
                    )
                    prior_manifest_path = (
                        runs_root / candidate_run_id / "documents" / doc_id / "ocr" / "native-manifest.json"
                    )
                    if (
                        not prior_manifest_path.is_file()
                        or prior_manifest_path.is_symlink()
                        or not prior_ocr_path.is_file()
                        or prior_ocr_path.is_symlink()
                    ):
                        continue

                    try:
                        resolved_prior_ocr = prior_ocr_path.resolve(strict=True)
                    except (OSError, RuntimeError):
                        continue

                    if (resolved_prior_ocr, c_doc.ocr.sha256) not in sealed_ocr_objects:
                        continue

                    prior_local_native_sources[doc_id] = PriorLocalNativeSource(
                        runs_root=runs_root,
                        run_id=candidate_run_id,
                        generation_id=c_manifest.generation_id,
                        corpus_sha256=c_manifest.corpus_sha256,
                        contract_sha256=c_manifest.contract_sha256,
                        document_id=doc_id,
                        pdf_sha256=c_doc.pdf.sha256,
                        pdf_size_bytes=c_doc.pdf.size_bytes,
                        page_count=c_doc.page_count,
                        ocr_sha256=c_doc.ocr.sha256,
                        ocr_size_bytes=c_doc.ocr.size_bytes,
                    )
                    unbound_doc_ids.remove(doc_id)
                    matched_in_this_run += 1

                if matched_in_this_run > 0:
                    LOGGER.info(
                        "Discovered %d reusable local OCR artifacts from prior run %s (remaining unbound: %d)",
                        matched_in_this_run,
                        candidate_run_id,
                        len(unbound_doc_ids),
                    )

        processed: list[_ProcessedDocument] = []
        ocr_failures: list[OCRFailureRecord] = []
        failed_documents: list[_OCRFailedDocument] = []
        structure_failures: list[StructureFailureRecord] = []
        ocr_cache_publication_deferred = 0
        ocr_cache_reused_count = 0
        ocr_provider_called_count = 0
        ocr_failure_report = run_dir / "reports" / "ocr-failures.json"
        ocr_systemic_failure_report = run_dir / "reports" / "ocr-systemic-failure.json"
        structure_failure_report = run_dir / "reports" / "structure-failures.json"
        for ocr_index, acquired_document in enumerate(acquired, start=1):
            source = acquired_document.source
            pdf = acquired_document.pdf
            document_id = source.document_id(pdf.sha256)
            ocr_output_dir = run_dir / "documents" / document_id / "ocr"

            async def recognize(
                current_document_id: str = document_id,
                current_pdf: DownloadedPDF = pdf,
                current_output_dir: Path = ocr_output_dir,
            ) -> OCRResult:
                result = await self.ocr.resolve(
                    run_id=run_id,
                    document_id=current_document_id,
                    pdf_path=current_pdf.path,
                    pdf_sha256=current_pdf.sha256,
                    pdf_size_bytes=current_pdf.size_bytes,
                    page_count=current_pdf.page_count,
                    output_dir=current_output_dir,
                    prior_local_native=prior_local_native_sources.get(current_document_id),
                )
                prior_local_native = prior_local_native_sources.get(current_document_id)
                if prior_local_native is not None and (
                    result.ocr_sha256 != prior_local_native.ocr_sha256
                    or result.size_bytes != prior_local_native.ocr_size_bytes
                    or len(result.ocr_bytes) != prior_local_native.ocr_size_bytes
                    or hashlib.sha256(result.ocr_bytes).hexdigest() != prior_local_native.ocr_sha256
                ):
                    # A valid cache entry for the same OCR source/contract may
                    # still carry bytes different from the already published
                    # generation OCR (for example after an inconsistent remote
                    # overwrite). Healing must never silently rebind that
                    # generation to the alternate bytes.
                    raise OCRCacheHealingIdentityError() from None
                return result

            ocr_result: OCRResult | None = None
            isolated_failure: Exception | None = None
            failure_reason: OCRFailureReason | None = None
            failure_attempts: int | None = None
            systemic_error: OCRSystemicFailureError | None = None
            systemic_source_exception: Exception | None = None
            terminal_systemic_error: OCRSystemicFailureError | None = None

            def record_systemic_failure(
                exc: Exception,
                current_document_id: str = document_id,
                current_source: SourceRecord = source,
                current_pdf: DownloadedPDF = pdf,
            ) -> str:
                nonlocal systemic_error, systemic_source_exception
                stage = self.state.get_stage(run_id, current_document_id, "ocr")
                if stage is None or stage.status != "running" or stage.attempt_count < 1:
                    raise RuntimeError("OCR systemic failure lost its active attempt")
                classified = _classify_ocr_systemic_failure(exc)
                failure = OCRSystemicFailureRecord(
                    run_id=run_id,
                    document_id=current_document_id,
                    source_id=current_source.source_id,
                    issuer=_bounded_report_text(current_source.issuer, maximum=64),
                    product_code=_bounded_report_text(current_source.product_code, maximum=256),
                    pdf_sha256=current_pdf.sha256,
                    attempt=stage.attempt_count,
                    occurred_at=datetime.now(UTC),
                    reason_code=classified.reason_code,
                    reason=classified.reason,
                    error_class_category=classified.error_class_category,
                    phase=classified.phase,
                    status_code=classified.status_code,
                    error_kind=classified.error_kind,
                    retryable=classified.retryable,
                    publication_attempts=classified.publication_attempts,
                    exit_code=classified.exit_code,
                    stderr_size_bytes=classified.stderr_size_bytes,
                    stderr_sha256=classified.stderr_sha256,
                )
                error = OCRSystemicFailureError(
                    run_id=run_id,
                    report_path=ocr_systemic_failure_report,
                    failure=failure,
                )
                _write_ocr_systemic_failure_report(
                    ocr_systemic_failure_report,
                    failure=failure,
                )
                systemic_source_exception = exc
                systemic_error = error
                return error.stored_error

            def stop_ocr_stage_retry(
                exc: Exception,
                current_document_id: str = document_id,
            ) -> bool:
                if is_isolatable_document_ocr_failure(exc):
                    return False
                if not isinstance(exc, ProviderSystemicError):
                    return True
                classified = _classify_ocr_systemic_failure(exc)
                if classified.retryable is not True:
                    return True
                stage = self.state.get_stage(run_id, current_document_id, "ocr")
                return stage is None or stage.status != "running" or stage.attempt_count >= stage.max_attempts

            try:
                ocr_result = await self._finite_stage(
                    run_id=run_id,
                    document_id=document_id,
                    name="ocr",
                    operation=recognize,
                    non_retryable_predicate=stop_ocr_stage_retry,
                    non_retryable_error_formatter=record_systemic_failure,
                    error_formatter=lambda exc: classify_ocr_failure(exc).stored_error,
                )
            except Exception as exc:
                if not is_isolatable_document_ocr_failure(exc):
                    if systemic_error is None or exc is not systemic_source_exception:
                        raise OCRFailureBookkeepingError() from None
                    # Even a typed exception is mutable: a caller can attach
                    # unsafe notes, args, or context after construction. The
                    # validated report fields above are the sole diagnostic
                    # boundary, so never retain the source object in the
                    # terminal exception chain.
                    terminal_systemic_error = systemic_error
                else:
                    isolated_failure = exc
            if terminal_systemic_error is not None:
                raise terminal_systemic_error
            if isolated_failure is not None:
                try:
                    stage = self.state.get_stage(run_id, document_id, "ocr")
                    failure_reason = classify_ocr_failure(isolated_failure)
                    if (
                        stage is None
                        or stage.status != "failed"
                        or stage.attempt_count != stage.max_attempts
                        or not stage.last_error
                        or stage.last_error != failure_reason.stored_error
                    ):
                        raise RuntimeError("OCR failure isolation requires a fully exhausted failed stage")
                    failure_attempts = stage.attempt_count
                except Exception:
                    raise OCRFailureBookkeepingError() from None
                finally:
                    isolated_failure = None
            if failure_reason is not None:
                if failure_attempts is None:
                    raise RuntimeError("OCR failure isolation lost its attempt count")
                failure_record = OCRFailureRecord(
                    issuer=_bounded_report_text(source.issuer, maximum=64),
                    product_code=_bounded_report_text(source.product_code, maximum=256),
                    product_name=_bounded_report_text(source.product_name, maximum=512),
                    file_name=_bounded_report_text(source.file_name, maximum=512),
                    document_id=document_id,
                    pdf_sha256=pdf.sha256,
                    page_count=pdf.page_count,
                    attempts=failure_attempts,
                    reason_code=failure_reason.reason_code,
                    reason=failure_reason.reason,
                )
                ocr_failures.append(failure_record)
                failed_documents.append(
                    _OCRFailedDocument(
                        record=OCRFailedProductRecord(
                            document_id=document_id,
                            issuer=source.issuer,
                            product_code=source.product_code,
                            product_name=source.product_name,
                            title=source.product_name,
                            pdf_sha256=pdf.sha256,
                            pdf_size_bytes=pdf.size_bytes,
                            page_count=pdf.page_count,
                            reason_code=failure_reason.reason_code,
                            reason=failure_reason.reason,
                            attempts=failure_attempts,
                        ),
                        pdf_path=pdf.path,
                        is_historical=acquired_document.is_historical,
                    )
                )
                _write_ocr_failure_report(
                    ocr_failure_report,
                    run_id=run_id,
                    failures=ocr_failures,
                )
                LOGGER.warning(
                    "OCR attempts exhausted document_id=%s reason_code=%s reason=%s; continuing",
                    document_id,
                    failure_reason.reason_code,
                    failure_reason.reason,
                )
                if ocr_index % 25 == 0 or ocr_index == len(acquired):
                    LOGGER.info(
                        "OCR progress completed=%d total=%d succeeded=%d failed=%d "
                        "cache_publication_deferred=%d",
                        ocr_index,
                        len(acquired),
                        len(processed),
                        len(failed_documents),
                        ocr_cache_publication_deferred,
                    )
                continue
            if ocr_result is None:
                raise RuntimeError("OCR stage returned no result")
            ocr_cache_reused_count += int(ocr_result.cache_reused)
            ocr_provider_called_count += int(ocr_result.provider_called)
            if ocr_result.cache_publication_deferred:
                ocr_cache_publication_deferred += 1
                LOGGER.warning(
                    "OCR cache publication deferred document_id=%s reason_code=%s; "
                    "continuing with generation-local OCR",
                    document_id,
                    ocr_result.cache_publication_reason_code,
                )
            verified_ocr_sha256 = ocr_result.ocr_sha256
            ocr_body = _canonical_ocr_body(ocr_result)
            if hashlib.sha256(ocr_body).hexdigest() != verified_ocr_sha256:
                raise RuntimeError("OCR result bytes changed after verification")
            ocr_path = ocr_output_dir / "ocr.md"
            if not ocr_path.exists() or ocr_path.read_bytes() != ocr_body:
                _atomic_write(ocr_path, ocr_body)
            pages = page_records(document_id, ocr_result)
            structure_artifact: StructureArtifact | None = None
            embedding_views: tuple[DerivedView, ...] = ()
            structure_fallback_reason_code: StructureFallbackReasonCode | None = None
            if self.v5_profile is not None:
                structure_path = run_dir / "documents" / document_id / "structure" / "structure.v2.json"

                def unclassified_fallback_v5(
                    current_pages: tuple[PageRecord, ...] = pages,
                    current_source: SourceRecord = source,
                    current_pdf_sha256: str = pdf.sha256,
                ) -> StructureArtifact:
                    fallback = build_unclassified_fallback_artifact(
                        current_pages,
                        issuer=current_source.issuer,
                        product_code=current_source.product_code,
                        product_name=current_source.product_name,
                        source_version=current_source.source_version,
                        effective_date=current_source.effective_date.isoformat(),
                        document_type=current_source.document_type,
                        source_id=current_source.source_id,
                        pdf_sha256=current_pdf_sha256,
                    )
                    validate_structure_artifact(fallback)
                    return fallback

                def write_structure_v5(
                    artifact: StructureArtifact,
                    destination: Path = structure_path,
                ) -> None:
                    body = artifact.canonical_bytes
                    if destination.exists():
                        if destination.is_symlink() or not destination.is_file():
                            raise RuntimeError("v5 structure checkpoint is not a regular file")
                        if destination.read_bytes() == body:
                            return
                    _atomic_write(destination, body)

                async def structure_v5(
                    current_pages: tuple[PageRecord, ...] = pages,
                    current_source: SourceRecord = source,
                    current_pdf_sha256: str = pdf.sha256,
                    destination: Path = structure_path,
                ) -> tuple[StructureArtifact, StructureFallbackReasonCode | None]:
                    del destination
                    try:
                        artifact = parse_structure_artifact(
                            current_pages,
                            issuer=current_source.issuer,
                            product_code=current_source.product_code,
                            product_name=current_source.product_name,
                            source_version=current_source.source_version,
                            effective_date=current_source.effective_date.isoformat(),
                            document_type=current_source.document_type,
                            source_id=current_source.source_id,
                            pdf_sha256=current_pdf_sha256,
                        )
                        validate_structure_artifact(artifact)
                        fallback_reason: StructureFallbackReasonCode | None = None
                    except Exception:
                        try:
                            artifact = unclassified_fallback_v5()
                        except Exception:
                            raise _StructureFallbackFailed("parser") from None
                        fallback_reason = "parser_failed"
                    write_structure_v5(artifact)
                    return artifact, fallback_reason

                try:
                    structure_artifact, structure_fallback_reason_code = await self._finite_stage(
                        run_id=run_id,
                        document_id=document_id,
                        name="structure",
                        operation=structure_v5,
                        non_retryable_predicate=lambda exc: isinstance(exc, _StructureFallbackFailed),
                        non_retryable_error_formatter=lambda _exc: (
                            "structure_fallback_failed: lossless structure fallback failed"
                        ),
                    )
                except _StructureFallbackFailed as exc:
                    failure = StructureFailureRecord(
                        issuer=source.issuer,
                        product_code=source.product_code,
                        document_id=document_id,
                        source_id=source.source_id,
                        pdf_sha256=pdf.sha256,
                        ocr_sha256=verified_ocr_sha256,
                        source_pages_sha256=_structure_source_pages_sha256(pages),
                        page_count=len(pages),
                        failure_stage=exc.failure_stage,
                    )
                    structure_failures.append(failure)
                    _write_structure_failure_report(
                        structure_failure_report,
                        run_id=run_id,
                        failures=structure_failures,
                    )
                    LOGGER.warning(
                        "Structure fallback failed document_id=%s stage=%s; continuing",
                        document_id,
                        exc.failure_stage,
                    )
                    continue
                assert structure_artifact is not None
                verified_structure_artifact: StructureArtifact = structure_artifact
                views_path = run_dir / "documents" / document_id / "structure" / "views.v1.json"

                async def make_views_v5(
                    artifact: StructureArtifact = verified_structure_artifact,
                    fallback_reason: StructureFallbackReasonCode | None = (structure_fallback_reason_code),
                    destination: Path = views_path,
                ) -> tuple[
                    StructureArtifact,
                    tuple[DerivedView, ...],
                    StructureFallbackReasonCode | None,
                ]:
                    assert self.v5_profile is not None

                    def build_rows(candidate: StructureArtifact) -> tuple[DerivedView, ...]:
                        assert self.v5_profile is not None
                        generated = build_derived_views(
                            candidate,
                            maximum_chars=V5_VIEW_MAXIMUM_CHARACTERS,
                            maximum_tokens=self.v5_profile.maximum_tokens,
                            token_counter=self.embeddings.token_counter,  # type: ignore[union-attr]
                        )
                        if not generated:
                            raise ValueError("structure artifact produced no searchable derived view")
                        return generated

                    final_artifact = artifact
                    final_fallback_reason = fallback_reason
                    try:
                        rows = build_rows(final_artifact)
                    except Exception:
                        if fallback_reason is not None:
                            raise _StructureFallbackFailed("derived_views") from None
                        try:
                            final_artifact = unclassified_fallback_v5()
                            rows = build_rows(final_artifact)
                        except Exception:
                            raise _StructureFallbackFailed("derived_views") from None
                        final_fallback_reason = "derived_view_failed"
                    write_structure_v5(final_artifact)
                    body = canonical_json_bytes(
                        {
                            "embedding_profile_id": self.v5_profile.profile_id,
                            "input_structure_sha256": final_artifact.artifact_sha256,
                            "schema_version": "cardrag.embedding-views.v1",
                            "views": [row.payload for row in rows],
                        }
                    )
                    if destination.exists():
                        if destination.is_symlink() or not destination.is_file():
                            raise RuntimeError("v5 view checkpoint is not a regular file")
                        if destination.read_bytes() == body:
                            return final_artifact, rows, final_fallback_reason
                    _atomic_write(destination, body)
                    return final_artifact, rows, final_fallback_reason

                try:
                    (
                        structure_artifact,
                        embedding_views,
                        structure_fallback_reason_code,
                    ) = await self._finite_stage(
                        run_id=run_id,
                        document_id=document_id,
                        name="views",
                        operation=make_views_v5,
                        non_retryable_predicate=lambda exc: isinstance(exc, _StructureFallbackFailed),
                        non_retryable_error_formatter=lambda _exc: (
                            "structure_fallback_failed: lossless structure views failed"
                        ),
                    )
                except _StructureFallbackFailed as exc:
                    failure = StructureFailureRecord(
                        issuer=source.issuer,
                        product_code=source.product_code,
                        document_id=document_id,
                        source_id=source.source_id,
                        pdf_sha256=pdf.sha256,
                        ocr_sha256=verified_ocr_sha256,
                        source_pages_sha256=_structure_source_pages_sha256(pages),
                        page_count=len(pages),
                        failure_stage=exc.failure_stage,
                    )
                    structure_failures.append(failure)
                    _write_structure_failure_report(
                        structure_failure_report,
                        run_id=run_id,
                        failures=structure_failures,
                    )
                    LOGGER.warning(
                        "Structure fallback views failed document_id=%s; continuing",
                        document_id,
                    )
                    continue
                structured_pages = pages
                chunks: tuple[dict[str, Any], ...] = ()
            else:
                structure_path = run_dir / "documents" / document_id / "structure" / "pages.json"

                async def structure_v4(
                    current_pages: tuple[PageRecord, ...] = pages,
                    current_ocr_sha256: str = verified_ocr_sha256,
                    destination: Path = structure_path,
                ) -> tuple[PageRecord, ...]:
                    payload = {
                        "schema_version": "cardrag.structure.v1",
                        "input_ocr_sha256": current_ocr_sha256,
                        "pages": [
                            {
                                "document_id": page.document_id,
                                "page": page.page,
                                "section_type": _section_type(page.text),
                                "text": page.text,
                                "text_sha256": page.text_sha256,
                            }
                            for page in current_pages
                        ],
                    }
                    body = canonical_json_bytes(payload)
                    if destination.exists():
                        if destination.is_symlink() or not destination.is_file():
                            raise RuntimeError("structure checkpoint is not a regular file")
                        if destination.read_bytes() == body:
                            return current_pages
                    _atomic_write(destination, body)
                    return current_pages

                structured_pages = await self._finite_stage(
                    run_id=run_id,
                    document_id=document_id,
                    name="structure",
                    operation=structure_v4,
                )
                chunks_path = run_dir / "documents" / document_id / "chunks" / "chunks.json"

                async def make_chunks(
                    current_pages: tuple[PageRecord, ...] = structured_pages,
                    current_ocr_sha256: str = verified_ocr_sha256,
                    destination: Path = chunks_path,
                    current_document_id: str = document_id,
                ) -> tuple[dict[str, Any], ...]:
                    rows = chunk_pages(current_document_id, current_pages)
                    payload = {
                        "schema_version": "cardrag.chunk-artifact.v1",
                        "input_sha256": canonical_sha256(
                            {
                                "ocr_sha256": current_ocr_sha256,
                                "chunk_contract": CHUNK_CONTRACT,
                                "pages": [page.text_sha256 for page in current_pages],
                            }
                        ),
                        "chunks": rows,
                    }
                    body = canonical_json_bytes(payload)
                    if destination.exists():
                        if destination.is_symlink() or not destination.is_file():
                            raise RuntimeError("chunk checkpoint is not a regular file")
                        if destination.read_bytes() == body:
                            return rows
                    _atomic_write(destination, body)
                    return rows

                chunks = await self._finite_stage(
                    run_id=run_id,
                    document_id=document_id,
                    name="chunk",
                    operation=make_chunks,
                )
            processed.append(
                _ProcessedDocument(
                    source=source,
                    record=DocumentRecord(
                        document_id=document_id,
                        issuer=source.issuer,
                        product_code=source.product_code,
                        product_name=source.product_name,
                        title=source.product_name,
                        pdf_sha256=pdf.sha256,
                        pdf_size_bytes=pdf.size_bytes,
                        page_count=pdf.page_count,
                        pages=structured_pages,
                    ),
                    pdf_path=pdf.path,
                    ocr_path=ocr_path,
                    ocr_sha256=verified_ocr_sha256,
                    ocr_size_bytes=ocr_result.size_bytes,
                    ocr_cache_kind=ocr_result.cache_kind,
                    ocr_reuse_key=ocr_result.cache_reuse_key,
                    chunks=chunks,
                    temporal_status=acquired_document.temporal_status,
                    supersedes_document_id=acquired_document.supersedes_document_id,
                    is_historical=acquired_document.is_historical,
                    structure_artifact=structure_artifact,
                    embedding_views=embedding_views,
                    structure_fallback_reason_code=structure_fallback_reason_code,
                )
            )

            if ocr_index % 25 == 0 or ocr_index == len(acquired):
                LOGGER.info(
                    "OCR progress completed=%d total=%d succeeded=%d failed=%d cache_publication_deferred=%d",
                    ocr_index,
                    len(acquired),
                    len(processed),
                    len(failed_documents),
                    ocr_cache_publication_deferred,
                )

        raw_issuer_ocr_counts = tuple(
            (
                adapter.spec.code,
                sum(item.source.issuer == adapter.spec.code for item in acquired),
                sum(item.record.issuer == adapter.spec.code for item in processed)
                + sum(item.issuer == adapter.spec.code for item in structure_failures),
                sum(item.record.issuer == adapter.spec.code for item in failed_documents),
            )
            for adapter in sorted(self.adapters, key=lambda item: item.spec.code)
        )
        if structure_failures:
            ordered_structure_failures = tuple(
                sorted(
                    structure_failures,
                    key=lambda item: (item.issuer, item.product_code, item.document_id),
                )
            )
            raise StructureDocumentFailuresError(
                run_id=run_id,
                report_path=structure_failure_report,
                failures=ordered_structure_failures,
            )
        # Historical revisions are included only when their immutable PDF and
        # source metadata are proven.  A failed historical OCR would otherwise
        # silently create a hole in a supposedly complete revision chain; do
        # not downgrade it to the bounded current-corpus disposition path.
        if any(item.is_historical for item in failed_documents):
            ordered_failures = tuple(
                sorted(
                    ocr_failures,
                    key=lambda item: (item.issuer, item.product_code, item.document_id),
                )
            )
            raise OCRDocumentFailuresError(
                run_id=run_id,
                report_path=ocr_failure_report,
                failures=ordered_failures,
            )
        publication_gate_failed = any(
            acquired_count < 1
            or succeeded_count < 1
            or succeeded_count * 100 < acquired_count * 95
            or acquired_count != succeeded_count + failed_count
            for _, acquired_count, succeeded_count, failed_count in raw_issuer_ocr_counts
        )
        if publication_gate_failed and ocr_failures:
            ordered_failures = tuple(
                sorted(
                    ocr_failures,
                    key=lambda item: (item.issuer, item.product_code, item.document_id),
                )
            )
            raise OCRDocumentFailuresError(
                run_id=run_id,
                report_path=ocr_failure_report,
                failures=ordered_failures,
            )
        if publication_gate_failed:
            raise RuntimeError(
                "OCR publication gate failed: every enabled issuer requires at least one success "
                "and a success rate of at least 95 percent"
            )
        issuer_ocr_counts = tuple(
            IssuerOCRCounts(
                issuer=issuer,
                acquired=acquired_count,
                succeeded=succeeded_count,
                failed=failed_count,
            )
            for issuer, acquired_count, succeeded_count, failed_count in raw_issuer_ocr_counts
        )

        if cache_healing_generation_id is not None:
            if (
                cache_healing_validated_seal is None
                or await self._reconcile_remote_bundle(cache_healing_validated_seal.manifest) is None
            ):
                raise RuntimeError("remote generation changed during OCR cache healing")
            healed_current = await self.webdav.validated_current_generation()
            if (
                healed_current is None
                or healed_current.generation_id != cache_healing_generation_id
                or healed_current.corpus_sha256 != corpus_sha256
                or healed_current.contract_sha256 != contract_sha256
                or healed_current.ocr_failed_document_count != 0
            ):
                raise RuntimeError("remote generation changed during OCR cache healing")
            if (
                not failed_documents
                and cache_healing_seal is not None
                and cache_healing_seal_path is not None
            ):
                # The seal's deferred count is outstanding recovery state, not
                # part of the immutable remote generation identity. Atomically
                # advance it after every fully processed healing pass so a
                # successful repair restores the ordinary no-change fast path.
                healed_seal = dict(cache_healing_seal)
                healed_seal["ocr_cache_publication_deferred"] = ocr_cache_publication_deferred
                validated_healed_seal = await self._validate_local_seal(healed_seal)
                if validated_healed_seal.manifest.generation_id != cache_healing_generation_id:
                    raise RuntimeError("OCR cache healing seal changed generation identity")
                _atomic_write(cache_healing_seal_path, canonical_json_bytes(healed_seal))
            self.state.finish_run(
                run_id,
                "no_change",
                corpus_sha256=corpus_sha256,
                contract_sha256=contract_sha256,
            )
            return await finalize_pdf_activity(
                PipelineResult(
                    run_id=run_id,
                    status="no_change",
                    corpus_sha256=corpus_sha256,
                    contract_sha256=contract_sha256,
                    generation_id=cache_healing_generation_id,
                    document_count=len(acquired),
                    evidence_count=0,
                    ocr_cache_publication_deferred=ocr_cache_publication_deferred,
                    v5_metrics=cache_healing_validated_seal.v5_metrics,
                )
            )

        if self.v5_profile is not None:
            published_result = await self._build_v5_generation(
                run_id=run_id,
                run_dir=run_dir,
                seal_path=seal_path,
                corpus_sha256=corpus_sha256,
                contract_sha256=contract_sha256,
                processed=processed,
                unsupported=unsupported,
                failed_documents=failed_documents,
                issuer_ocr_counts=issuer_ocr_counts,
                ocr_cache_publication_deferred=ocr_cache_publication_deferred,
                unresolved_revision_ledger=unresolved_revision_ledger,
                unresolved_revision_sha256=unresolved_revision_sha256,
                historical_pdf_cache_hits=historical_pdf_cache_hits,
                ocr_cache_reused_count=ocr_cache_reused_count,
                ocr_provider_called_count=ocr_provider_called_count,
            )
            return await finalize_pdf_activity(
                replace(
                    published_result,
                    ocr_cache_publication_deferred=ocr_cache_publication_deferred,
                )
            )

        chunk_rows = tuple(row for document in processed for row in document.chunks)
        embedding_path = run_dir / "embeddings" / "corpus.json"
        embedding_input_sha = canonical_sha256(
            {
                "contract": {
                    "provider": self.embeddings.provider,
                    "model": self.embeddings.model,
                    "dimension": self.embeddings.dimension,
                    "policy": EMBEDDING_POLICY_VERSION,
                    "prefix": DOCUMENT_EMBEDDING_PREFIX,
                },
                "chunks": [(row["evidence_id"], row["text"]) for row in chunk_rows],
            }
        )
        embedding_contract_sha = canonical_sha256(
            {
                "provider": self.embeddings.provider,
                "model": self.embeddings.model,
                "dimension": self.embeddings.dimension,
                "policy": EMBEDDING_POLICY_VERSION,
                "prefix": DOCUMENT_EMBEDDING_PREFIX,
            }
        )

        async def embed() -> list[list[float]]:
            if embedding_path.exists():
                payload = json.loads(embedding_path.read_text(encoding="utf-8"))
                if payload.get("input_sha256") == embedding_input_sha:
                    cached_vectors = payload.get("vectors")
                    if isinstance(cached_vectors, list) and len(cached_vectors) == len(chunk_rows):
                        normalized = []
                        for vector in cached_vectors:
                            normalized.append(
                                list(struct.unpack(f"<{EMBEDDING_DIMENSION}f", encode_embedding(vector)))
                            )
                        return normalized
            vectors: list[list[float] | None] = [None] * len(chunk_rows)
            misses: list[int] = []
            cache_keys: list[str] = []
            for index, row in enumerate(chunk_rows):
                text_sha = sha256_bytes(str(row["text"]).encode())
                cache_key = canonical_sha256(
                    {
                        "schema_version": "cardrag.embedding-cache-key.v1",
                        "contract_sha256": embedding_contract_sha,
                        "text_sha256": text_sha,
                    }
                )
                cache_keys.append(cache_key)
                cached = self.state.get_embedding(cache_key)
                if cached is None:
                    misses.append(index)
                else:
                    vectors[index] = list(struct.unpack(f"<{EMBEDDING_DIMENSION}f", cached))
            for batch_start in range(0, len(misses), 64):
                batch_indices = misses[batch_start : batch_start + 64]
                generated = await self.embeddings.embed_documents(
                    [str(chunk_rows[index]["text"]) for index in batch_indices]
                )
                if len(generated) != len(batch_indices):
                    raise RuntimeError("embedding provider returned the wrong batch count")
                for index, vector in zip(batch_indices, generated, strict=True):
                    encoded = encode_embedding(vector)
                    normalized_vector = list(struct.unpack(f"<{EMBEDDING_DIMENSION}f", encoded))
                    vectors[index] = normalized_vector
                    self.state.put_embedding(
                        cache_key=cache_keys[index],
                        contract_sha256=embedding_contract_sha,
                        text_sha256=sha256_bytes(str(chunk_rows[index]["text"]).encode()),
                        embedding=encoded,
                    )
            complete = [vector for vector in vectors if vector is not None]
            if len(complete) != len(chunk_rows):
                raise RuntimeError("embedding cache/provider left incomplete evidence")
            _atomic_write(
                embedding_path,
                canonical_json_bytes({"input_sha256": embedding_input_sha, "vectors": complete}),
            )
            return complete

        vectors = await self._finite_stage(
            run_id=run_id,
            document_id="corpus",
            name="embedding",
            operation=embed,
        )
        evidence = tuple(
            EvidenceRecord(**row, embedding=vector) for row, vector in zip(chunk_rows, vectors, strict=True)
        )
        generation_id = f"g-{run_id[:24]}-{corpus_sha256[:12]}"
        database_path = run_dir / "sealed" / "index.sqlite3"
        export = self.exporter.export(
            database_path,
            generation_id=generation_id,
            corpus_sha256=corpus_sha256,
            contract_sha256=contract_sha256,
            embedding_provider=self.embeddings.provider,
            embedding_model=self.embeddings.model,
            issuers=[adapter.spec for adapter in self.adapters],
            documents=[document.record for document in processed],
            evidence=evidence,
            unsupported_products=unsupported,
            ocr_failed_products=[item.record for item in failed_documents],
        )
        current_remote = await self.webdav.validated_current_generation()
        if current_remote is None and await self.webdav.get_bytes(self.webdav.pointer_path) is not None:
            raise RuntimeError("remote stable generation is corrupt; refusing publication")
        previous_id = current_remote.generation_id if current_remote is not None else None
        generation_documents = tuple(
            sorted(
                tuple(
                    GenerationDocument(
                        document_id=document.record.document_id,
                        issuer=document.record.issuer,
                        pdf=ArtifactRef.for_cas(
                            sha256=document.record.pdf_sha256,
                            size_bytes=document.record.pdf_size_bytes,
                            media_type="application/pdf",
                        ),
                        ocr=ArtifactRef.for_cas(
                            sha256=document.ocr_sha256,
                            size_bytes=document.ocr_size_bytes,
                            media_type="text/markdown; charset=utf-8",
                        ),
                        page_count=document.record.page_count,
                        ocr_cache_kind=document.ocr_cache_kind,
                        ocr_reuse_key=document.ocr_reuse_key,
                        availability="available",
                    )
                    for document in processed
                )
                + tuple(
                    GenerationDocument(
                        document_id=document.record.document_id,
                        issuer=document.record.issuer,
                        pdf=ArtifactRef.for_cas(
                            sha256=document.record.pdf_sha256,
                            size_bytes=document.record.pdf_size_bytes,
                            media_type="application/pdf",
                        ),
                        page_count=document.record.page_count,
                        availability="ocr_failed",
                        ocr_failure=GenerationOCRFailure(
                            reason_code=document.record.reason_code,
                            reason=document.record.reason,
                            attempts=document.record.attempts,
                        ),
                    )
                    for document in failed_documents
                ),
                key=lambda row: row.document_id,
            )
        )
        manifest = GenerationManifest(
            schema_version=GENERATION_SCHEMA_ID,
            generation_id=generation_id,
            created_at=datetime.now(UTC),
            serving_schema=SERVING_SCHEMA_ID,
            serving_database=ArtifactRef(
                sha256=export.sha256,
                size_bytes=export.size_bytes,
                media_type="application/vnd.sqlite3",
                path=generation_database_path(generation_id).as_posix(),
            ),
            corpus_sha256=corpus_sha256,
            contract_sha256=contract_sha256,
            embedding_contract=EmbeddingContract(
                provider=self.embeddings.provider,
                model=self.embeddings.model,
                dimension=1536,
                count=len(evidence),
            ),
            issuer_codes=tuple(sorted(adapter.spec.code for adapter in self.adapters)),
            counts=GenerationCounts(
                documents=len(processed) + len(failed_documents),
                pdf_objects=len(
                    {row.record.pdf_sha256 for row in processed}
                    | {row.record.pdf_sha256 for row in failed_documents}
                ),
                ocr_objects=len({row.ocr_sha256 for row in processed}),
                chunks=len(evidence),
            ),
            documents=generation_documents,
            issuer_ocr_counts=issuer_ocr_counts,
            previous_generation_id=previous_id,
        )
        sealed = {
            "schema_version": "cardrag.worker-seal.v1",
            "run_id": run_id,
            "generation_id": generation_id,
            "corpus_sha256": corpus_sha256,
            "contract_sha256": contract_sha256,
            "database_path": str(database_path),
            "database_sha256": export.sha256,
            "database_size_bytes": export.size_bytes,
            "ocr_cache_publication_deferred": ocr_cache_publication_deferred,
            "manifest": manifest.model_dump(mode="json"),
            "objects": [
                {
                    "path": str(document.pdf_path),
                    "sha256": document.record.pdf_sha256,
                    "size_bytes": document.record.pdf_size_bytes,
                    "media_type": "application/pdf",
                }
                for document in processed
            ]
            + [
                {
                    "path": str(document.pdf_path),
                    "sha256": document.record.pdf_sha256,
                    "size_bytes": document.record.pdf_size_bytes,
                    "media_type": "application/pdf",
                }
                for document in failed_documents
            ]
            + [
                {
                    "path": str(document.ocr_path),
                    "sha256": document.ocr_sha256,
                    "size_bytes": document.ocr_size_bytes,
                    "media_type": "text/markdown; charset=utf-8",
                }
                for document in processed
            ],
        }
        _atomic_write(seal_path, canonical_json_bytes(sealed))
        published_result = await self._publish_sealed(run_id, sealed)
        return await finalize_pdf_activity(
            replace(
                published_result,
                ocr_cache_publication_deferred=ocr_cache_publication_deferred,
            )
        )

    async def _build_v5_generation(
        self,
        *,
        run_id: str,
        run_dir: Path,
        seal_path: Path,
        corpus_sha256: str,
        contract_sha256: str,
        processed: Sequence[_ProcessedDocument],
        unsupported: Sequence[UnsupportedProductRecord],
        failed_documents: Sequence[_OCRFailedDocument],
        issuer_ocr_counts: tuple[IssuerOCRCounts, ...],
        ocr_cache_publication_deferred: int,
        unresolved_revision_ledger: Sequence[UnresolvedRevisionLedgerEntryV5],
        unresolved_revision_sha256: str,
        historical_pdf_cache_hits: int,
        ocr_cache_reused_count: int,
        ocr_provider_called_count: int,
    ) -> PipelineResult:
        """Build and publish one structure-preserving v5 generation."""

        profile = self.v5_profile
        provider = self.embeddings
        if profile is None or not isinstance(provider, OpenRouterQwenEmbeddingProviderV5):
            raise RuntimeError("v5 generation lost its sealed embedding provider")
        if not processed:
            raise RuntimeError("v5 generation requires at least one structured document")
        ordered_documents = tuple(
            sorted(
                processed,
                key=lambda row: (
                    row.source.issuer,
                    row.source.product_code,
                    row.record.document_id,
                ),
            )
        )
        artifacts: list[StructureArtifact] = []
        ordered_view_pairs: list[tuple[_ProcessedDocument, DerivedView]] = []
        for document in ordered_documents:
            artifact = document.structure_artifact
            if artifact is None:
                raise RuntimeError("v5 document has no structure artifact")
            validate_structure_artifact(artifact)
            if document.structure_fallback_reason_code is not None and any(
                node.node_type not in {"ROOT", "ITEM", "UNCLASSIFIED"}
                or node.major_class != "UNKNOWN"
                or node.raw_heading is not None
                for node in artifact.nodes
            ):
                raise RuntimeError("v5 structure fallback artifact violates its neutral policy")
            artifacts.append(artifact)
            ordered_view_pairs.extend((document, view) for view in document.embedding_views)
        if not ordered_view_pairs:
            raise RuntimeError("v5 structure produced no embedding views")
        raw_structure_fallback_documents = tuple(
            {
                "contract_revision_id": artifact.contract_revision_id,
                "document_id": document.record.document_id,
                "reason_code": document.structure_fallback_reason_code,
                "structure_artifact_sha256": artifact.artifact_sha256,
            }
            for document, artifact in zip(ordered_documents, artifacts, strict=True)
            if document.structure_fallback_reason_code is not None
        )
        structure_fallback_ledger = _canonical_structure_fallback_ledger(raw_structure_fallback_documents)
        structure_fallback_documents = tuple(structure_fallback_ledger["documents"])
        structure_fallback_ledger_sha256 = canonical_sha256(structure_fallback_ledger)
        empty_structure_failure_ledger_sha256 = _structure_failure_ledger_sha256(())

        # Materialize the exact non-vector exporter DTO set before embedding.
        # The capacity ledger below therefore cannot drift from the rows later
        # handed to ServingDatabaseExporterV5.
        parser_profiles_by_issuer = {
            adapter.spec.code: issuer_parser_profile(adapter.spec.code) for adapter in self.adapters
        }
        for artifact in artifacts:
            expected_profile = parser_profiles_by_issuer.get(artifact.issuer)
            if expected_profile is None or artifact.issuer_profile != expected_profile:
                raise RuntimeError("v5 structure artifact differs from its sealed issuer parser profile")
        parser_policy_sha256 = canonical_sha256(
            {
                "contextual_item_policy": contextual_item_policy_payload(),
                "profiles": [
                    parser_profiles_by_issuer[issuer].payload for issuer in sorted(parser_profiles_by_issuer)
                ],
                "schema_version": "cardrag.structure-parser-policy.v1",
                "unclassified_fallback_policy": unclassified_fallback_policy_payload(),
            }
        )
        embedding_policy_sha256 = canonical_sha256(
            {
                "document_policy": QWEN3_DOCUMENT_POLICY,
                "endpoint_metadata_sha256": profile.endpoint_metadata_sha256,
                "maximum_tokens": profile.maximum_tokens,
                "profile_id": profile.profile_id,
                "schema_version": "cardrag.embedding-policy.v1",
                "tokenizer_revision": QWEN_TOKENIZER_REVISION,
                "tokenizer_sha256": QWEN_TOKENIZER_SHA256,
                "view_maximum_characters": V5_VIEW_MAXIMUM_CHARACTERS,
            }
        )
        retrieval_policy_sha256 = canonical_sha256(self.v5_retrieval_policy)
        issuer_rows = tuple(
            IssuerInput(
                code=adapter.spec.code,
                display_name=adapter.spec.display_name,
                sort_order=adapter.spec.sort_order,
            )
            for adapter in self.adapters
        )
        document_artifacts = tuple(zip(ordered_documents, artifacts, strict=True))
        current_lineage_rows: dict[str, tuple[_ProcessedDocument, StructureArtifact]] = {}
        for document, artifact in document_artifacts:
            if document.temporal_status != "current":
                continue
            if artifact.product_lineage_id in current_lineage_rows:
                raise RuntimeError("v5 lineage has multiple current processed documents")
            current_lineage_rows[artifact.product_lineage_id] = (document, artifact)
        artifact_lineage_ids = {artifact.product_lineage_id for artifact in artifacts}
        if set(current_lineage_rows) != artifact_lineage_ids:
            raise RuntimeError("v5 lineage does not have exactly one proven current revision")
        lineage_rows = tuple(
            ProductLineageInput(
                product_lineage_id=artifact.product_lineage_id,
                issuer=document.source.issuer,
                product_code=document.source.product_code,
                document_type=document.source.document_type,
                name=document.source.product_name,
            )
            for document, artifact in sorted(
                current_lineage_rows.values(), key=lambda row: row[1].product_lineage_id
            )
        )
        revision_id_by_document_id = {
            document.record.document_id: artifact.contract_revision_id
            for document, artifact in document_artifacts
        }
        revision_rows = tuple(
            ContractRevisionInput(
                contract_revision_id=artifact.contract_revision_id,
                product_lineage_id=artifact.product_lineage_id,
                document_id=document.record.document_id,
                source_id=document.source.source_id,
                source_version=document.source.source_version,
                source_url=document.source.source_url,
                effective_date=document.source.effective_date.isoformat(),
                pdf_sha256=document.record.pdf_sha256,
                pdf_size_bytes=document.record.pdf_size_bytes,
                page_count=document.record.page_count,
                temporal_status=document.temporal_status,
                supersedes_revision_id=(
                    None
                    if document.supersedes_document_id is None
                    else revision_id_by_document_id.get(document.supersedes_document_id)
                ),
            )
            for document, artifact in document_artifacts
        )
        if any(
            document.supersedes_document_id is not None and revision.supersedes_revision_id is None
            for (document, _artifact), revision in zip(document_artifacts, revision_rows, strict=True)
        ):
            raise RuntimeError("v5 revision predecessor is absent from the sealed generation")
        page_rows = tuple(
            DocumentPageInput(
                contract_revision_id=artifact.contract_revision_id,
                page=page.page,
                text=page.text,
                text_sha256=page.text_sha256,
            )
            for artifact in artifacts
            for page in artifact.pages
        )
        node_rows = tuple(
            StructureNodeInput(
                node_id=node.node_id,
                contract_revision_id=node.contract_revision_id,
                parent_id=node.parent_id,
                parent_contract_revision_id=(None if node.parent_id is None else node.contract_revision_id),
                node_type=node.node_type,
                major_class=node.major_class,
                raw_heading=node.raw_heading,
                ordinal=node.ordinal,
                display_text=node.display_text,
                table_headers=node.table_headers,
                table_cells=node.table_cells,
                table_role=node.table_role,
            )
            for artifact in artifacts
            for node in artifact.nodes
        )
        span_rows = tuple(
            NodeSpanInput(
                node_id=node.node_id,
                contract_revision_id=node.contract_revision_id,
                page=span.page,
                source_start=span.source_start,
                source_end=span.source_end,
                text_sha256=span.text_sha256,
                span_ordinal=span.span_ordinal,
                is_canonical=span.is_canonical,
            )
            for artifact in artifacts
            for node in artifact.nodes
            for span in node.spans
        )
        link_rows = tuple(
            NodeLinkInput(
                from_node_id=link.from_node_id,
                from_contract_revision_id=artifact.contract_revision_id,
                to_node_id=link.to_node_id,
                to_contract_revision_id=artifact.contract_revision_id,
                link_type=link.link_type,
                ordinal=link.ordinal,
            )
            for artifact in artifacts
            for link in artifact.links
        )
        exported_profile = EmbeddingProfileInput(
            profile_id=profile.profile_id,
            provider=profile.provider,
            model=profile.model,
            provider_id=profile.provider_id,
            dimension=profile.dimension,
            dtype=profile.dtype,
            normalization=profile.normalization,
            document_policy=profile.document_policy,
            query_policy=profile.query_policy,
            maximum_tokens=profile.maximum_tokens,
        )
        unsupported_rows = tuple(
            UnsupportedProductInput(
                issuer=row.source.issuer,
                product_code=row.source.product_code,
                name=row.source.product_name,
                disposition=row.disposition,
                source_id=row.source.source_id,
                source_version=row.source.source_version,
                source_url=row.source.source_url,
                protected_magic=row.protected_magic,
                protected_sha256=row.protected_sha256,
                protected_size_bytes=row.protected_size_bytes,
                source_payload_json=row.source_payload_json,
            )
            for row in unsupported
        )
        failed_rows = tuple(
            OCRFailedProductInput(
                issuer=row.record.issuer,
                product_code=row.record.product_code,
                name=row.record.product_name,
                document_id=row.record.document_id,
                title=row.record.title,
                pdf_sha256=row.record.pdf_sha256,
                pdf_size_bytes=row.record.pdf_size_bytes,
                page_count=row.record.page_count,
                reason_code=row.record.reason_code,
                reason=row.record.reason,
                attempts=row.record.attempts,
            )
            for row in failed_documents
        )
        extra_metadata = {
            "embedding_endpoint_metadata_sha256": profile.endpoint_metadata_sha256,
            "embedding_endpoint_name": profile.endpoint_name,
            "embedding_policy_sha256": embedding_policy_sha256,
            "parser_policy_sha256": parser_policy_sha256,
            "retrieval_policy_sha256": retrieval_policy_sha256,
            "revision_history_policy_version": REVISION_HISTORY_POLICY_VERSION,
            "historical_revision_unresolved_count": str(len(unresolved_revision_ledger)),
            "historical_revision_unresolved_sha256": unresolved_revision_sha256,
            "structure_fallback_document_count": str(len(structure_fallback_documents)),
            "structure_fallback_documents_sha256": structure_fallback_ledger_sha256,
            "structure_fallback_policy_version": str(
                unclassified_fallback_policy_payload()["schema_version"]
            ),
            "structure_failed_document_count": "0",
            "structure_failed_documents_sha256": empty_structure_failure_ledger_sha256,
            "tokenizer_revision": QWEN_TOKENIZER_REVISION,
            "tokenizer_sha256": QWEN_TOKENIZER_SHA256,
            **{
                f"parser_profile_id.{issuer}": parser_profiles_by_issuer[issuer].profile_id
                for issuer in sorted(parser_profiles_by_issuer)
            },
            **{
                f"parser_profile_sha256.{issuer}": parser_profiles_by_issuer[issuer].sha256
                for issuer in sorted(parser_profiles_by_issuer)
            },
            **(
                {"aggregation_profile_artifact_sha256": self.document_aggregation.artifact_sha256}
                if self.document_aggregation is not None
                else {}
            ),
        }
        database_ledger = build_v5_database_ledger(
            issuers=issuer_rows,
            product_lineages=lineage_rows,
            unsupported_products=unsupported_rows,
            ocr_failed_products=failed_rows,
            contract_revisions=revision_rows,
            document_pages=page_rows,
            structure_nodes=node_rows,
            node_spans=span_rows,
            node_links=link_rows,
            embedding_profiles=(exported_profile,),
            derived_views=tuple(view for _document, view in ordered_view_pairs),
            primary_embedding_profile_id=profile.profile_id,
            extra_metadata=extra_metadata,
            sealed_profile=self.document_aggregation is not None,
        )
        predicted_database_bytes = predict_serving_database_bytes(
            payload_bytes=database_ledger.payload_bytes,
            row_count=database_ledger.row_count,
            fts_indexed_text_bytes=database_ledger.fts_indexed_text_bytes,
            secondary_index_text_bytes=database_ledger.secondary_index_text_bytes,
        )

        embedding_cache_hit_counts: Counter[str] = Counter()
        embedding_cache_miss_counts: Counter[str] = Counter()
        embedding_download_counts: Counter[str] = Counter()
        embedding_provider_wire_attempt_baseline = provider.wire_attempt_count
        embedding_provider_call_count = 0
        sealed_cache_bindings: tuple[tuple[str, str, str], ...] | None = None

        async def embed_views() -> None:
            nonlocal sealed_cache_bindings
            embedding_cache_hit_counts.clear()
            embedding_cache_miss_counts.clear()
            embedding_download_counts.clear()
            sealed_cache_bindings = None
            cache_bindings: list[tuple[str, str]] = []
            vector_sha256_by_cache_key: dict[str, str] = {}
            formatted_token_counts: list[int] = []
            indices_by_cache_key: dict[str, list[int]] = {}
            for index, (_document, view) in enumerate(ordered_view_pairs):
                formatted = format_embedding_input("document", view.embedding_input)
                token_count = provider.token_counter(formatted)
                if (
                    isinstance(token_count, bool)
                    or not isinstance(token_count, int)
                    or token_count < 1
                    or token_count > profile.maximum_tokens
                ):
                    raise RuntimeError(
                        "derived view exceeds the sealed exact-token limit; truncation is forbidden"
                    )
                cache_key, input_sha256 = embedding_cache_key(
                    profile,
                    kind="document",
                    formatted_input=formatted,
                )
                if input_sha256 != view.input_sha256:
                    raise RuntimeError("derived view hash differs from its exact document input")
                cache_bindings.append((cache_key, input_sha256))
                formatted_token_counts.append(token_count)
                bound_indices = indices_by_cache_key.setdefault(cache_key, [])
                if bound_indices and cache_bindings[bound_indices[0]][1] != input_sha256:
                    raise RuntimeError("v5 embedding cache key collision changed its exact input")
                bound_indices.append(index)

            unique_misses: list[int] = []
            for cache_key, bound_indices in indices_by_cache_key.items():
                representative = bound_indices[0]
                input_sha256 = cache_bindings[representative][1]
                cached = self.state.get_embedding_v5(
                    cache_key,
                    profile_id=profile.profile_id,
                    input_sha256=input_sha256,
                    dimension=profile.dimension,
                    dtype=profile.dtype,
                    normalization=profile.normalization,
                )
                if cached is None:
                    unique_misses.append(representative)
                    for index in bound_indices:
                        embedding_cache_miss_counts[ordered_view_pairs[index][1].view_type] += 1
                else:
                    vector_sha256_by_cache_key[cache_key] = hashlib.sha256(cached.embedding).hexdigest()
                    for index in bound_indices:
                        embedding_cache_hit_counts[ordered_view_pairs[index][1].view_type] += 1

            try:
                try:
                    wal_baseline = self.state.observe_embedding_cache_v5_wal()
                except WorkerStateWALCapacityError as exc:
                    raise V5CapacityError(
                        "Worker v5 embedding cache WAL baseline could not be sealed"
                    ) from exc
                prediction = predict_v5_local_artifacts(
                    derived_view_count=len(ordered_view_pairs),
                    database_payload_bytes=database_ledger.payload_bytes,
                    database_row_count=database_ledger.row_count,
                    database_fts_indexed_text_bytes=database_ledger.fts_indexed_text_bytes,
                    database_secondary_index_text_bytes=(database_ledger.secondary_index_text_bytes),
                    embedding_cache_miss_count=len(unique_misses),
                    embedding_cache_wal_baseline_bytes=wal_baseline.size_bytes,
                )

                def check_cache_wal_capacity(*, boundary: str) -> None:
                    try:
                        wal_capacity = self.state.check_embedding_cache_v5_wal_capacity(
                            baseline=wal_baseline,
                            maximum_wal_growth_bytes=(
                                prediction.embedding_cache_transaction_bytes
                                - prediction.embedding_cache_wal_baseline_bytes
                            ),
                        )
                    except WorkerStateWALCapacityError as exc:
                        LOGGER.error(
                            "reason_code=v5_embedding_cache_wal_capacity_failed boundary=%s",
                            boundary,
                        )
                        raise V5CapacityError(
                            "Worker v5 embedding cache WAL exceeded its predicted capacity bound"
                        ) from exc
                    LOGGER.info(
                        "V5 embedding cache WAL capacity boundary=%s baseline_bytes=%d "
                        "wal_size_bytes=%d wal_limit_bytes=%d",
                        boundary,
                        wal_capacity.baseline_wal_size_bytes,
                        wal_capacity.wal_size_bytes,
                        wal_capacity.maximum_wal_bytes,
                    )

                capacity_policy = self.capacity_policy_v5
                if capacity_policy is None:
                    raise V5CapacityError("v5 generation lost its local capacity policy")
                capacity_snapshot = await to_thread_fenced(
                    preflight_v5_capacity,
                    self.state_dir,
                    prediction,
                    policy=capacity_policy,
                )
            except V5CapacityError:
                LOGGER.error(
                    "reason_code=v5_capacity_preflight_failed derived_views=%d "
                    "unique_cache_misses=%d database_payload_bytes=%d database_rows=%d",
                    len(ordered_view_pairs),
                    len(unique_misses),
                    database_ledger.payload_bytes,
                    database_ledger.row_count,
                )
                raise
            LOGGER.info(
                "V5 capacity preflight passed derived_views=%d unique_cache_misses=%d "
                "wal_baseline_bytes=%d sidecar_bytes=%d database_bytes=%d "
                "logical_growth_bytes=%d peak_growth_bytes=%d "
                "state_usage_bytes=%d filesystem_free_bytes=%d",
                len(ordered_view_pairs),
                len(unique_misses),
                prediction.embedding_cache_wal_baseline_bytes,
                prediction.vector_sidecar_bytes,
                prediction.serving_database_bytes,
                prediction.logical_growth_bytes,
                prediction.peak_growth_bytes,
                capacity_snapshot.state_usage_bytes,
                capacity_snapshot.filesystem_free_bytes,
            )
            miss_batches = _embedding_miss_batches(
                unique_misses,
                formatted_token_counts,
                maximum_tokens=profile.maximum_tokens,
            )
            remaining_unique_misses = len(unique_misses)
            for batch_indices in miss_batches:
                check_cache_wal_capacity(boundary="before-provider-batch")
                try:
                    await to_thread_fenced(
                        preflight_v5_remaining_free_capacity,
                        self.state_dir,
                        prediction,
                        remaining_embedding_cache_miss_count=remaining_unique_misses,
                        policy=capacity_policy,
                    )
                except V5CapacityError:
                    LOGGER.error(
                        "reason_code=v5_remaining_capacity_failed remaining_unique_cache_misses=%d",
                        remaining_unique_misses,
                    )
                    raise
                generated = await provider.embed_documents(
                    [ordered_view_pairs[index][1].embedding_input for index in batch_indices]
                )
                if len(generated) != len(batch_indices):
                    raise RuntimeError("Qwen provider returned the wrong view batch count")
                for representative, vector in zip(batch_indices, generated, strict=True):
                    cache_key, input_sha256 = cache_bindings[representative]
                    cached = self.state.put_embedding_v5(
                        cache_key=cache_key,
                        profile_id=profile.profile_id,
                        input_sha256=input_sha256,
                        dimension=profile.dimension,
                        dtype=profile.dtype,
                        normalization=profile.normalization,
                        values=vector,
                    )
                    vector_sha256_by_cache_key[cache_key] = hashlib.sha256(cached.embedding).hexdigest()
                    # Downloads count unique cache keys, attributed
                    # deterministically to the key's first view type. Hits and
                    # misses above remain per derived sidecar row.
                    embedding_download_counts[ordered_view_pairs[representative][1].view_type] += 1
                check_cache_wal_capacity(boundary="after-provider-batch")
                remaining_unique_misses -= len(batch_indices)
            if remaining_unique_misses != 0:
                raise RuntimeError("v5 embedding batching left unprocessed unique cache misses")
            if len(vector_sha256_by_cache_key) != len(indices_by_cache_key):
                raise RuntimeError("v5 embedding cache rows were not all digest-sealed")
            sealed_cache_bindings = tuple(
                (cache_key, input_sha256, vector_sha256_by_cache_key[cache_key])
                for cache_key, input_sha256 in cache_bindings
            )

        await self._finite_stage(
            run_id=run_id,
            document_id="corpus-v5",
            name="embedding-v5",
            operation=embed_views,
            non_retryable_predicate=lambda exc: (
                isinstance(exc, V5CapacityError)
                or (isinstance(exc, EmbeddingV5Error) and not isinstance(exc, EmbeddingV5TransientError))
            ),
            non_retryable_error_formatter=_safe_v5_embedding_terminal_error,
        )
        embedding_provider_call_count = provider.wire_attempt_count - embedding_provider_wire_attempt_baseline
        if embedding_provider_call_count < 0:
            raise RuntimeError("Qwen provider wire-attempt counter moved backwards")
        if sealed_cache_bindings is None or len(sealed_cache_bindings) != len(ordered_view_pairs):
            raise RuntimeError("v5 embedding cache/provider left incomplete view bindings")
        try:
            final_wal_baseline = self.state.observe_embedding_cache_v5_wal()
        except WorkerStateWALCapacityError as exc:
            raise V5CapacityError("Worker v5 embedding cache WAL baseline could not be resealed") from exc
        final_prediction = predict_v5_local_artifacts(
            derived_view_count=len(ordered_view_pairs),
            database_payload_bytes=database_ledger.payload_bytes,
            database_row_count=database_ledger.row_count,
            database_fts_indexed_text_bytes=database_ledger.fts_indexed_text_bytes,
            database_secondary_index_text_bytes=database_ledger.secondary_index_text_bytes,
            embedding_cache_miss_count=0,
            embedding_cache_wal_baseline_bytes=final_wal_baseline.size_bytes,
        )
        capacity_policy = self.capacity_policy_v5
        if capacity_policy is None:
            raise V5CapacityError("v5 generation lost its local capacity policy")
        try:
            final_capacity_snapshot = await to_thread_fenced(
                preflight_v5_capacity,
                self.state_dir,
                final_prediction,
                policy=capacity_policy,
            )
        except V5CapacityError:
            LOGGER.error(
                "reason_code=v5_preexport_capacity_failed derived_views=%d",
                len(ordered_view_pairs),
            )
            raise
        LOGGER.info(
            "V5 pre-export capacity recheck passed state_usage_bytes=%d "
            "filesystem_free_bytes=%d peak_growth_bytes=%d",
            final_capacity_snapshot.state_usage_bytes,
            final_capacity_snapshot.filesystem_free_bytes,
            final_prediction.peak_growth_bytes,
        )

        def lazy_cache_vector(
            *,
            cache_key: str,
            input_sha256: str,
            expected_vector_sha256: str,
        ) -> LazyEmbeddingVector:
            def load() -> bytes:
                cached = self.state.get_embedding_v5(
                    cache_key,
                    profile_id=profile.profile_id,
                    input_sha256=input_sha256,
                    dimension=profile.dimension,
                    dtype=profile.dtype,
                    normalization=profile.normalization,
                )
                if cached is None:
                    raise RuntimeError("sealed v5 embedding cache row disappeared before export")
                expected_identity = (
                    cache_key,
                    profile.profile_id,
                    input_sha256,
                    profile.dimension,
                    profile.dtype,
                    profile.normalization,
                )
                actual_identity = (
                    cached.cache_key,
                    cached.profile_id,
                    cached.input_sha256,
                    cached.dimension,
                    cached.dtype,
                    cached.normalization,
                )
                if actual_identity != expected_identity:
                    raise RuntimeError("sealed v5 embedding cache row changed identity before export")
                if hashlib.sha256(cached.embedding).hexdigest() != expected_vector_sha256:
                    raise RuntimeError("sealed v5 embedding cache row changed bytes before export")
                return cached.embedding

            return LazyEmbeddingVector(
                cache_identity=cache_key,
                profile_id=profile.profile_id,
                input_sha256=input_sha256,
                expected_vector_sha256=expected_vector_sha256,
                loader=load,
                dimension=profile.dimension,
                dtype=profile.dtype,
                normalization=profile.normalization,
            )

        view_rows = tuple(
            EmbeddingViewInput(
                row_index=index,
                node_id=view.node_id,
                contract_revision_id=view.contract_revision_id,
                view_type=view.view_type,
                embedding_input=view.embedding_input,
                input_sha256=view.input_sha256,
                profile_id=profile.profile_id,
                display_text=view.display_text,
                source_spans=tuple(
                    ViewSourceSpanInput(
                        page=span.page,
                        source_start=span.source_start,
                        source_end=span.source_end,
                        text_sha256=span.text_sha256,
                    )
                    for span in view.spans
                ),
                vector=lazy_cache_vector(
                    cache_key=cache_binding[0],
                    input_sha256=cache_binding[1],
                    expected_vector_sha256=cache_binding[2],
                ),
            )
            for index, ((_document, view), cache_binding) in enumerate(
                zip(ordered_view_pairs, sealed_cache_bindings, strict=True)
            )
        )

        generation_id = f"g-{run_id[:24]}-{corpus_sha256[:12]}"
        database_path = run_dir / "sealed" / "index.sqlite3"
        vector_path = run_dir / "sealed" / "vectors.f32"
        publication_seal = run_dir / "sealed" / "publish.json"
        run_status = self.state.connection.execute(
            "SELECT status FROM run WHERE run_id=?",
            (run_id,),
        ).fetchone()
        publish_status = self.state.connection.execute(
            "SELECT status FROM publish WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if (
            run_status is None
            or str(run_status["status"]) != "running"
            or publish_status is not None
            or os.path.lexists(publication_seal)
        ):
            raise RuntimeError("v5 incomplete-target replacement requires an unsealed running run")
        export = self.exporter_v5.export(
            database_path,
            vector_path,
            generation_id=generation_id,
            corpus_sha256=corpus_sha256,
            contract_sha256=contract_sha256,
            primary_embedding_profile_id=profile.profile_id,
            issuers=issuer_rows,
            product_lineages=lineage_rows,
            unsupported_products=unsupported_rows,
            ocr_failed_products=failed_rows,
            contract_revisions=revision_rows,
            document_pages=page_rows,
            structure_nodes=node_rows,
            node_spans=span_rows,
            node_links=link_rows,
            embedding_profiles=(exported_profile,),
            embedding_views=view_rows,
            document_aggregation_policy=(
                self.document_aggregation.profile.aggregation_policy
                if self.document_aggregation is not None
                else None
            ),
            sealed_profile_sha256=(
                self.document_aggregation.profile_sha256 if self.document_aggregation is not None else None
            ),
            expected_exact_row_corpus_sha256=(
                self.document_aggregation.profile.exact_row_corpus_sha256
                if self.document_aggregation is not None
                else None
            ),
            predicted_serving_database_bytes=predicted_database_bytes,
            maximum_serving_database_bytes=capacity_policy.maximum_serving_database_bytes,
            maximum_vector_sidecar_bytes=capacity_policy.maximum_vector_sidecar_bytes,
            reserved_free_space_bytes=capacity_policy.reserved_free_space_bytes,
            replace_incomplete_owned_targets=True,
            extra_metadata=extra_metadata,
        )
        previous_id: str | None
        if self.document_aggregation is not None:
            current_aggregation_head = await self._validated_document_aggregation_head()
            previous_id = current_aggregation_head.generation_id
        else:
            current_remote = await self.webdav.validated_current_generation()
            if current_remote is None and await self.webdav.get_bytes(self.webdav.pointer_path) is not None:
                raise RuntimeError("remote stable generation is corrupt; refusing publication")
            previous_id = current_remote.generation_id if current_remote is not None else None
        generation_documents = tuple(
            sorted(
                tuple(
                    GenerationDocument(
                        document_id=document.record.document_id,
                        issuer=document.record.issuer,
                        pdf=ArtifactRef.for_cas(
                            sha256=document.record.pdf_sha256,
                            size_bytes=document.record.pdf_size_bytes,
                            media_type="application/pdf",
                        ),
                        ocr=ArtifactRef.for_cas(
                            sha256=document.ocr_sha256,
                            size_bytes=document.ocr_size_bytes,
                            media_type="text/markdown; charset=utf-8",
                        ),
                        page_count=document.record.page_count,
                        ocr_cache_kind=document.ocr_cache_kind,
                        ocr_reuse_key=document.ocr_reuse_key,
                        availability="available",
                    )
                    for document in ordered_documents
                )
                + tuple(
                    GenerationDocument(
                        document_id=document.record.document_id,
                        issuer=document.record.issuer,
                        pdf=ArtifactRef.for_cas(
                            sha256=document.record.pdf_sha256,
                            size_bytes=document.record.pdf_size_bytes,
                            media_type="application/pdf",
                        ),
                        page_count=document.record.page_count,
                        availability="ocr_failed",
                        ocr_failure=GenerationOCRFailure(
                            reason_code=document.record.reason_code,
                            reason=document.record.reason,
                            attempts=document.record.attempts,
                        ),
                    )
                    for document in failed_documents
                ),
                key=lambda row: row.document_id,
            )
        )
        node_counts: Counter[str] = Counter(
            node.node_type for artifact in artifacts for node in artifact.nodes
        )
        major_counts: Counter[str] = Counter(
            node.major_class
            for artifact in artifacts
            for node in artifact.nodes
            if node.node_type == "MAJOR_SECTION"
        )
        view_counts = Counter(view.view_type for _document, view in ordered_view_pairs)
        core_profile = EmbeddingProfile.qwen3(
            provider_id=profile.provider_id,
            maximum_tokens=profile.maximum_tokens,
        )
        manifest = GenerationManifest(
            schema_version="cardrag.generation.v5",
            generation_id=generation_id,
            created_at=datetime.now(UTC),
            serving_schema="cardrag.serving-db.v5",
            serving_database=ArtifactRef(
                sha256=export.database_sha256,
                size_bytes=export.database_size_bytes,
                media_type="application/vnd.sqlite3",
                path=generation_database_path(generation_id).as_posix(),
            ),
            corpus_sha256=corpus_sha256,
            contract_sha256=contract_sha256,
            embedding_contract=EmbeddingContract(
                provider=profile.provider,
                model=profile.model,
                dimension=4096,
                count=len(view_rows),
            ),
            issuer_codes=tuple(sorted(adapter.spec.code for adapter in self.adapters)),
            counts=GenerationCounts(
                documents=len(ordered_documents) + len(failed_documents),
                pdf_objects=len(
                    {row.record.pdf_sha256 for row in ordered_documents}
                    | {row.record.pdf_sha256 for row in failed_documents}
                ),
                ocr_objects=len({row.ocr_sha256 for row in ordered_documents}),
                chunks=len(view_rows),
            ),
            documents=generation_documents,
            issuer_ocr_counts=issuer_ocr_counts,
            structure_contract=StructureContract(
                schema_version="cardrag.structure.v2",
                parser_profiles=tuple(
                    ManifestIssuerParserProfile(
                        issuer=issuer,
                        profile_id=parser_profiles_by_issuer[issuer].profile_id,
                        profile_sha256=parser_profiles_by_issuer[issuer].sha256,
                    )
                    for issuer in sorted(parser_profiles_by_issuer)
                ),
                node_counts=StructureNodeCounts(
                    total=len(node_rows),
                    root=node_counts["ROOT"],
                    major_section=node_counts["MAJOR_SECTION"],
                    item=node_counts["ITEM"],
                    paragraph=node_counts["PARAGRAPH"],
                    list_item=node_counts["LIST_ITEM"],
                    table=node_counts["TABLE"],
                    table_row=node_counts["TABLE_ROW"],
                    footnote=node_counts["FOOTNOTE"],
                    boilerplate=node_counts["BOILERPLATE"],
                    unclassified=node_counts["UNCLASSIFIED"],
                ),
                major_class_counts=StructureMajorClassCounts(
                    total=node_counts["MAJOR_SECTION"],
                    benefit=major_counts["BENEFIT"],
                    notice=major_counts["NOTICE"],
                    mixed=major_counts["MIXED"],
                    unknown=major_counts["UNKNOWN"],
                ),
                source_coverage=StructureSourceCoverage(
                    source_non_whitespace_characters=export.source_non_whitespace_count,
                    covered_non_whitespace_characters=export.covered_non_whitespace_count,
                    source_non_whitespace_sha256=export.source_coverage_sha256,
                    covered_non_whitespace_sha256=export.source_coverage_sha256,
                ),
                revision_counts=StructureRevisionCounts(
                    total=export.contract_revision_count,
                    current=export.current_revision_count,
                    superseded=export.superseded_revision_count,
                    ambiguous=export.ambiguous_revision_count,
                ),
                cross_contract_parent_count=0,
                cross_contract_link_count=0,
                lineages_with_multiple_current_revisions=0,
            ),
            embedding_profiles=(core_profile,),
            primary_embedding_profile_id=core_profile.profile_id,
            embedding_view_counts=tuple(
                EmbeddingViewCount(view_type=view_type, count=view_counts[view_type])
                for view_type in EMBEDDING_VIEW_TYPES
            ),
            vector_sidecar=EmbeddingVectorSidecar(
                artifact=ArtifactRef(
                    sha256=export.vector_sha256,
                    size_bytes=export.vector_size_bytes,
                    media_type="application/octet-stream",
                    path=generation_vectors_path(generation_id).as_posix(),
                ),
                profile_id=core_profile.profile_id,
                row_count=export.vector_row_count,
                dimension=4096,
                dtype="float32",
                byte_order="little-endian",
                layout="row-major",
                normalization="l2",
            ),
            parser_policy_sha256=parser_policy_sha256,
            embedding_policy_sha256=embedding_policy_sha256,
            retrieval_policy_sha256=retrieval_policy_sha256,
            document_aggregation_profile=(
                self.document_aggregation.profile if self.document_aggregation is not None else None
            ),
            document_aggregation_policy=(
                self.document_aggregation.profile.aggregation_policy
                if self.document_aggregation is not None
                else None
            ),
            sealed_profile_sha256=(
                self.document_aggregation.profile_sha256 if self.document_aggregation is not None else None
            ),
            exact_row_corpus_sha256=(
                export.exact_row_corpus_sha256 if self.document_aggregation is not None else None
            ),
            previous_generation_id=previous_id,
        )
        parser_profile_document_counts = Counter(artifact.issuer_profile_id for artifact in artifacts)
        unknown_unclassified_count = node_counts["UNCLASSIFIED"] + major_counts["UNKNOWN"]
        unknown_unclassified_denominator = len(node_rows) + node_counts["MAJOR_SECTION"]
        source_coverage_percent = (
            100.0
            if export.source_non_whitespace_count == 0
            else 100.0 * export.covered_non_whitespace_count / export.source_non_whitespace_count
        )
        v5_metrics = {
            "schema_version": "cardrag.worker-v5-metrics.v3",
            "parser_profile_document_counts": {
                profile_id: parser_profile_document_counts[profile_id]
                for profile_id in sorted(parser_profile_document_counts)
            },
            "node_type_counts": {
                node_type: node_counts[node_type]
                for node_type in (
                    "BOILERPLATE",
                    "FOOTNOTE",
                    "ITEM",
                    "LIST_ITEM",
                    "MAJOR_SECTION",
                    "PARAGRAPH",
                    "ROOT",
                    "TABLE",
                    "TABLE_ROW",
                    "UNCLASSIFIED",
                )
            },
            "major_class_counts": {
                major_class: major_counts[major_class]
                for major_class in ("BENEFIT", "MIXED", "NOTICE", "UNKNOWN")
            },
            "unknown_unclassified_count": unknown_unclassified_count,
            "unknown_unclassified_denominator": unknown_unclassified_denominator,
            "unknown_unclassified_ratio": (
                0.0
                if unknown_unclassified_denominator == 0
                else unknown_unclassified_count / unknown_unclassified_denominator
            ),
            "source_non_whitespace_count": export.source_non_whitespace_count,
            "covered_non_whitespace_count": export.covered_non_whitespace_count,
            "source_coverage_percent": source_coverage_percent,
            "cross_page_continuation_count": sum(
                link.link_type == "CONTINUATION_OF" for artifact in artifacts for link in artifact.links
            ),
            "table_node_count": node_counts["TABLE"],
            "footnote_node_count": node_counts["FOOTNOTE"],
            "contract_revision_count": export.contract_revision_count,
            "current_revision_count": export.current_revision_count,
            "superseded_revision_count": export.superseded_revision_count,
            "ambiguous_revision_count": export.ambiguous_revision_count,
            "revision_history_policy_version": REVISION_HISTORY_POLICY_VERSION,
            "historical_revision_unresolved_count": len(unresolved_revision_ledger),
            "historical_revision_unresolved_identities": list(unresolved_revision_ledger),
            "historical_revision_unresolved_sha256": unresolved_revision_sha256,
            "historical_pdf_cache_hits": historical_pdf_cache_hits,
            "structure_fallback_policy_version": str(
                unclassified_fallback_policy_payload()["schema_version"]
            ),
            "structure_fallback_document_count": len(structure_fallback_documents),
            "structure_fallback_documents": list(structure_fallback_documents),
            "structure_fallback_documents_sha256": structure_fallback_ledger_sha256,
            "structure_failed_document_count": 0,
            "structure_failed_documents_sha256": empty_structure_failure_ledger_sha256,
            "embedding_view_counts": {
                view_type: {
                    "downloads": embedding_download_counts[view_type],
                    "hits": embedding_cache_hit_counts[view_type],
                    "misses": embedding_cache_miss_counts[view_type],
                }
                for view_type in EMBEDDING_VIEW_TYPES
            },
            "embedding_provider_call_count": embedding_provider_call_count,
            "embedding_provider": profile.provider,
            "embedding_model": profile.model,
            "embedding_dimension": profile.dimension,
            "embedding_profile_id": profile.profile_id,
            "vector_sidecar_size_bytes": export.vector_size_bytes,
            "ocr_cache_reused_count": ocr_cache_reused_count,
            "ocr_provider_called_count": ocr_provider_called_count,
        }
        _validated_v5_metrics(v5_metrics, manifest=manifest)
        sealed = {
            "schema_version": "cardrag.worker-seal.v1",
            "run_id": run_id,
            "generation_id": generation_id,
            "corpus_sha256": corpus_sha256,
            "contract_sha256": contract_sha256,
            "database_path": str(database_path),
            "database_sha256": export.database_sha256,
            "database_size_bytes": export.database_size_bytes,
            "vector_path": str(vector_path),
            "vector_sha256": export.vector_sha256,
            "vector_size_bytes": export.vector_size_bytes,
            "ocr_cache_publication_deferred": ocr_cache_publication_deferred,
            "v5_metrics": v5_metrics,
            "manifest": manifest.model_dump(mode="json"),
            "objects": [
                {
                    "path": str(document.pdf_path),
                    "sha256": document.record.pdf_sha256,
                    "size_bytes": document.record.pdf_size_bytes,
                    "media_type": "application/pdf",
                }
                for document in ordered_documents
            ]
            + [
                {
                    "path": str(document.pdf_path),
                    "sha256": document.record.pdf_sha256,
                    "size_bytes": document.record.pdf_size_bytes,
                    "media_type": "application/pdf",
                }
                for document in failed_documents
            ]
            + [
                {
                    "path": str(document.ocr_path),
                    "sha256": document.ocr_sha256,
                    "size_bytes": document.ocr_size_bytes,
                    "media_type": "text/markdown; charset=utf-8",
                }
                for document in ordered_documents
            ],
        }
        _atomic_write(seal_path, canonical_json_bytes(sealed))
        return await self._publish_sealed(run_id, sealed)

    async def _align_seal_to_current(
        self,
        sealed: Mapping[str, Any],
        *,
        validated: _ValidatedSeal,
        current_generation_id: str | None,
    ) -> dict[str, Any]:
        """Fence a seal to the exact head observed before it was built.

        A historical corpus can be published again only by a new run that has
        freshly discovered and downloaded it. Re-chaining an old failed seal
        would let `resume` roll the latest-only channel back to stale content.
        """

        if validated.manifest.previous_generation_id != current_generation_id:
            raise RuntimeError(
                "sealed publication was superseded by a different stable generation; "
                "rerun with fresh discovery"
            )
        return dict(sealed)

    async def _validate_local_seal(self, sealed: Mapping[str, Any]) -> _ValidatedSeal:
        if sealed.get("schema_version") != "cardrag.worker-seal.v1":
            raise RuntimeError("unknown worker publication seal schema")
        seal_sha256 = canonical_sha256(sealed)
        sealed_run_id = validate_identifier(str(sealed.get("run_id") or ""), label="seal run ID")
        local_root = (self.state_dir / "runs" / sealed_run_id).resolve(strict=True)
        pdf_cache_root = self._seal_pdf_cache_objects_root.resolve(strict=True)

        def sealed_file(
            raw_path: object,
            *,
            label: str,
            allow_pdf_cache: bool = False,
        ) -> Path:
            candidate = Path(str(raw_path))
            if candidate.is_symlink() or not candidate.is_file():
                raise RuntimeError(f"sealed {label} is not a regular non-symlink file")
            resolved = candidate.resolve(strict=True)
            approved_roots = (local_root, pdf_cache_root) if allow_pdf_cache else (local_root,)
            if not any(resolved.is_relative_to(root) for root in approved_roots):
                raise RuntimeError(f"sealed {label} escapes approved worker storage")
            return resolved

        database_path = sealed_file(sealed.get("database_path"), label="serving database")
        if not isinstance(sealed.get("manifest"), Mapping):
            raise RuntimeError("sealed generation manifest is not an object")
        raw_manifest = canonical_json_bytes(sealed["manifest"])
        manifest = GenerationManifest.model_validate_json(raw_manifest)
        if manifest.canonical_bytes() != raw_manifest:
            raise RuntimeError("sealed generation manifest is not canonical JSON")
        available_document_count = sum(
            document.availability == "available" for document in manifest.documents
        )
        raw_deferred_count = sealed.get("ocr_cache_publication_deferred", 0)
        if (
            type(raw_deferred_count) is not int
            or raw_deferred_count < 0
            or raw_deferred_count > available_document_count
        ):
            raise RuntimeError("sealed OCR cache publication deferred count is invalid")
        ocr_cache_publication_deferred = raw_deferred_count
        generation_id = str(sealed.get("generation_id") or "")
        corpus_sha256 = str(sealed.get("corpus_sha256") or "")
        contract_sha256 = str(sealed.get("contract_sha256") or "")
        if (
            manifest.generation_id != generation_id
            or manifest.corpus_sha256 != corpus_sha256
            or manifest.contract_sha256 != contract_sha256
        ):
            raise RuntimeError("sealed generation manifest identity/contract mismatch")
        expected_schema_pair = {
            GENERATION_SCHEMA_ID: SERVING_SCHEMA_ID,
            GENERATION_SCHEMA_ID_V5: SERVING_SCHEMA_ID_V5,
        }
        if expected_schema_pair.get(manifest.schema_version) != manifest.serving_schema:
            raise RuntimeError("sealed generation manifest schema is not a worker v4/v5 bundle")
        if manifest.serving_database.path != generation_database_path(generation_id).as_posix():
            raise RuntimeError("sealed serving database has the wrong generation path")
        database_sha, database_size = await to_thread_fenced(sha256_file, database_path)
        if (
            database_sha != sealed.get("database_sha256")
            or database_sha != manifest.serving_database.sha256
            or database_size != sealed.get("database_size_bytes")
            or database_size != manifest.serving_database.size_bytes
        ):
            raise RuntimeError("sealed serving database hash/size mismatch")

        vector_path: Path | None = None
        v5_metrics: Mapping[str, Any] | None = None
        if manifest.schema_version == GENERATION_SCHEMA_ID_V5:
            if manifest.vector_sidecar is None:
                raise RuntimeError("sealed v5 generation has no vector sidecar")
            vector_path = sealed_file(sealed.get("vector_path"), label="vector sidecar")
            vector_sha, vector_size = await to_thread_fenced(sha256_file, vector_path)
            vector_artifact = manifest.vector_sidecar.artifact
            if (
                vector_sha != sealed.get("vector_sha256")
                or vector_sha != vector_artifact.sha256
                or vector_size != sealed.get("vector_size_bytes")
                or vector_size != vector_artifact.size_bytes
                or vector_artifact.path != generation_vectors_path(generation_id).as_posix()
            ):
                raise RuntimeError("sealed vector sidecar hash/size/path mismatch")
            v5_metrics = _validated_v5_metrics(sealed.get("v5_metrics"), manifest=manifest)
        elif any(
            sealed.get(key) is not None for key in ("vector_path", "vector_sha256", "vector_size_bytes")
        ):
            raise RuntimeError("sealed legacy generation unexpectedly contains a vector sidecar")
        elif sealed.get("v5_metrics") is not None:
            raise RuntimeError("sealed legacy generation unexpectedly contains v5 Worker metrics")

        def verify_database_binding() -> None:
            connection = sqlite3.connect(f"{database_path.as_uri()}?mode=ro&immutable=1", uri=True)
            try:
                integrity = connection.execute("PRAGMA integrity_check").fetchone()
                if integrity is None or integrity[0] != "ok":
                    raise RuntimeError("sealed serving database failed integrity_check")
                if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise RuntimeError("sealed serving database failed foreign_key_check")
                metadata = {
                    str(key): str(value)
                    for key, value in connection.execute("SELECT key,value FROM metadata")
                }
                if manifest.schema_version == GENERATION_SCHEMA_ID_V5:
                    structure_contract = manifest.structure_contract
                    vector_contract = manifest.vector_sidecar
                    if (
                        structure_contract is None
                        or vector_contract is None
                        or vector_path is None
                        or v5_metrics is None
                    ):
                        raise RuntimeError("sealed v5 manifest lost its structure/vector contract")
                    expected_metadata = {
                        "schema_id": SERVING_SCHEMA_ID_V5,
                        "generation_id": generation_id,
                        "corpus_sha256": corpus_sha256,
                        "contract_sha256": contract_sha256,
                        "embedding_provider": manifest.embedding_contract.provider,
                        "embedding_model": manifest.embedding_contract.model,
                        "embedding_dimension": str(manifest.embedding_contract.dimension),
                        "embedding_count": str(manifest.embedding_contract.count),
                        "embedding_input_policy_version": QWEN3_DOCUMENT_POLICY,
                        "primary_embedding_profile_id": manifest.primary_embedding_profile_id,
                        "vector_sidecar_sha256": vector_contract.artifact.sha256,
                        "vector_sidecar_size_bytes": str(vector_contract.artifact.size_bytes),
                        "vector_sidecar_row_count": str(vector_contract.row_count),
                        "vector_sidecar_dimension": str(vector_contract.dimension),
                        "vector_sidecar_dtype": vector_contract.dtype,
                        "vector_sidecar_normalization": vector_contract.normalization,
                        "vector_sidecar_byte_order": vector_contract.byte_order,
                        "vector_sidecar_layout": vector_contract.layout,
                        "vector_sidecar_profile_id": vector_contract.profile_id,
                        "embedding_profile_count": str(len(manifest.embedding_profiles)),
                        "parser_policy_sha256": str(manifest.parser_policy_sha256),
                        "embedding_policy_sha256": str(manifest.embedding_policy_sha256),
                        "retrieval_policy_sha256": str(manifest.retrieval_policy_sha256),
                        "source_non_whitespace_count": str(
                            structure_contract.source_coverage.source_non_whitespace_characters
                        ),
                        "covered_non_whitespace_count": str(
                            structure_contract.source_coverage.covered_non_whitespace_characters
                        ),
                        "source_coverage_sha256": (
                            structure_contract.source_coverage.source_non_whitespace_sha256
                        ),
                        "contract_revision_count": str(structure_contract.revision_counts.total),
                        "current_revision_count": str(structure_contract.revision_counts.current),
                        "superseded_revision_count": str(structure_contract.revision_counts.superseded),
                        "ambiguous_revision_count": str(structure_contract.revision_counts.ambiguous),
                        "structure_node_count": str(structure_contract.node_counts.total),
                        "revision_history_policy_version": str(v5_metrics["revision_history_policy_version"]),
                        "historical_revision_unresolved_count": str(
                            v5_metrics["historical_revision_unresolved_count"]
                        ),
                        "historical_revision_unresolved_sha256": str(
                            v5_metrics["historical_revision_unresolved_sha256"]
                        ),
                        "structure_fallback_document_count": str(
                            v5_metrics["structure_fallback_document_count"]
                        ),
                        "structure_fallback_documents_sha256": str(
                            v5_metrics["structure_fallback_documents_sha256"]
                        ),
                        "structure_fallback_policy_version": str(
                            v5_metrics["structure_fallback_policy_version"]
                        ),
                        "structure_failed_document_count": str(v5_metrics["structure_failed_document_count"]),
                        "structure_failed_documents_sha256": str(
                            v5_metrics["structure_failed_documents_sha256"]
                        ),
                    }
                    exact_row_metadata = metadata.get("exact_row_corpus_sha256")
                    if (
                        exact_row_metadata is None
                        or re.fullmatch(r"[0-9a-f]{64}", exact_row_metadata) is None
                    ):
                        raise RuntimeError("sealed v5 database exact-row corpus identity is invalid")
                    if manifest.document_aggregation_profile is None:
                        expected_metadata.update(
                            {
                                "document_aggregation_status": "candidate_default",
                                "document_aggregation_policy": "max_child",
                            }
                        )
                        if any(
                            key in metadata
                            for key in (
                                "sealed_profile_sha256",
                                "aggregation_profile_artifact_sha256",
                            )
                        ):
                            raise RuntimeError("unsealed M0 database contains a sealed profile identity")
                    else:
                        selected = self.document_aggregation
                        if (
                            selected is None
                            or manifest.document_aggregation_profile != selected.profile
                            or manifest.sealed_profile_sha256 != selected.profile_sha256
                            or manifest.exact_row_corpus_sha256 != selected.profile.exact_row_corpus_sha256
                        ):
                            raise RuntimeError("sealed M1 manifest lacks its supplied profile identity")
                        expected_metadata.update(
                            {
                                "document_aggregation_status": "sealed",
                                "document_aggregation_policy": str(manifest.document_aggregation_policy),
                                "sealed_profile_sha256": selected.profile_sha256,
                                "exact_row_corpus_sha256": selected.profile.exact_row_corpus_sha256,
                                "aggregation_profile_artifact_sha256": selected.artifact_sha256,
                            }
                        )
                    for parser_profile in structure_contract.parser_profiles:
                        expected_metadata[f"parser_profile_id.{parser_profile.issuer}"] = (
                            parser_profile.profile_id
                        )
                        expected_metadata[f"parser_profile_sha256.{parser_profile.issuer}"] = (
                            parser_profile.profile_sha256
                        )
                    node_count_values = {
                        "ROOT": structure_contract.node_counts.root,
                        "MAJOR_SECTION": structure_contract.node_counts.major_section,
                        "ITEM": structure_contract.node_counts.item,
                        "PARAGRAPH": structure_contract.node_counts.paragraph,
                        "LIST_ITEM": structure_contract.node_counts.list_item,
                        "TABLE": structure_contract.node_counts.table,
                        "TABLE_ROW": structure_contract.node_counts.table_row,
                        "FOOTNOTE": structure_contract.node_counts.footnote,
                        "BOILERPLATE": structure_contract.node_counts.boilerplate,
                        "UNCLASSIFIED": structure_contract.node_counts.unclassified,
                    }
                    for node_type, count in node_count_values.items():
                        expected_metadata[f"structure_node_count.{node_type}"] = str(count)
                    major_count_values = {
                        "BENEFIT": structure_contract.major_class_counts.benefit,
                        "NOTICE": structure_contract.major_class_counts.notice,
                        "MIXED": structure_contract.major_class_counts.mixed,
                        "UNKNOWN": structure_contract.major_class_counts.unknown,
                    }
                    for major_class, count in major_count_values.items():
                        expected_metadata[f"structure_major_class_count.{major_class}"] = str(count)
                    for view_count in manifest.embedding_view_counts:
                        expected_metadata[f"embedding_view_count.{view_count.view_type}"] = str(
                            view_count.count
                        )
                    if any(metadata.get(key) != value for key, value in expected_metadata.items()):
                        raise RuntimeError("sealed v5 database metadata does not bind its manifest")
                    profile_rows = tuple(
                        tuple(row)
                        for row in connection.execute(
                            """SELECT profile_id,provider,model,provider_id,dimension,dtype,
                                      normalization,document_policy,query_policy,maximum_tokens
                                 FROM embedding_profiles ORDER BY profile_id"""
                        )
                    )
                    expected_profiles = tuple(
                        (
                            profile.profile_id,
                            profile.provider,
                            profile.model,
                            profile.provider_id,
                            profile.dimension,
                            profile.dtype,
                            profile.normalization,
                            profile.document_policy,
                            profile.query_policy,
                            profile.maximum_tokens,
                        )
                        for profile in manifest.embedding_profiles
                    )
                    if profile_rows != expected_profiles:
                        raise RuntimeError("sealed v5 database profiles do not bind its manifest")
                    table_counts = {
                        "SELECT count(*) FROM contract_revisions": (
                            "contract_revisions",
                            structure_contract.revision_counts.total,
                        ),
                        "SELECT count(*) FROM structure_nodes": (
                            "structure_nodes",
                            structure_contract.node_counts.total,
                        ),
                        "SELECT count(*) FROM embedding_profiles": (
                            "embedding_profiles",
                            len(manifest.embedding_profiles),
                        ),
                        "SELECT count(*) FROM embedding_views": (
                            "embedding_views",
                            manifest.counts.chunks,
                        ),
                        "SELECT count(*) FROM embedding_view_spans": (
                            "embedding_view_spans",
                            int(metadata["embedding_view_span_count"]),
                        ),
                        "SELECT count(*) FROM embedding_views_fts": (
                            "embedding_views_fts",
                            manifest.counts.chunks,
                        ),
                        "SELECT count(*) FROM ocr_failed_products": (
                            "ocr_failed_products",
                            sum(document.availability == "ocr_failed" for document in manifest.documents),
                        ),
                    }
                    for query, (table, expected_count) in table_counts.items():
                        actual_count = int(connection.execute(query).fetchone()[0])
                        if actual_count != expected_count:
                            raise RuntimeError(f"sealed v5 database {table} count differs from manifest")
                    unsupported_count = int(
                        connection.execute("SELECT count(*) FROM unsupported_products").fetchone()[0]
                    )
                    if unsupported_count != int(metadata.get("unsupported_document_count", "-1")):
                        raise RuntimeError("sealed v5 unsupported disposition count is inconsistent")
                    failed_count = table_counts["SELECT count(*) FROM ocr_failed_products"][1]
                    if failed_count != int(metadata.get("ocr_failed_document_count", "-1")):
                        raise RuntimeError("sealed v5 OCR-failed disposition count is inconsistent")
                    continuation_count = int(
                        connection.execute(
                            "SELECT count(*) FROM node_links WHERE link_type='CONTINUATION_OF'"
                        ).fetchone()[0]
                    )
                    if continuation_count != v5_metrics["cross_page_continuation_count"]:
                        raise RuntimeError("sealed v5 continuation metrics differ from the database")
                    row_stats = connection.execute(
                        "SELECT count(*),min(row_index),max(row_index) FROM embedding_views"
                    ).fetchone()
                    if tuple(row_stats) != (
                        manifest.counts.chunks,
                        0,
                        manifest.counts.chunks - 1,
                    ):
                        raise RuntimeError("sealed v5 embedding rows are not contiguous")
                    columns = {
                        str(row[1]) for row in connection.execute("PRAGMA table_info(embedding_views)")
                    }
                    if "embedding" in columns or "vector" in columns:
                        raise RuntimeError("sealed v5 database contains inline vectors")
                    row_struct = struct.Struct(f"<{vector_contract.dimension}f")
                    with vector_path.open("rb") as sidecar:
                        for row_index in range(vector_contract.row_count):
                            raw = sidecar.read(row_struct.size)
                            if len(raw) != row_struct.size:
                                raise RuntimeError("sealed vector sidecar ended early")
                            values = row_struct.unpack(raw)
                            norm_squared = sum(value * value for value in values)
                            if not all(math.isfinite(value) for value in values) or not math.isclose(
                                norm_squared,
                                1.0,
                                rel_tol=2e-5,
                                abs_tol=2e-5,
                            ):
                                raise RuntimeError(f"sealed vector sidecar row {row_index} is invalid")
                        if sidecar.read(1):
                            raise RuntimeError("sealed vector sidecar contains trailing bytes")
                    return
                expected_metadata = {
                    "schema_id": SERVING_SCHEMA_ID,
                    "generation_id": generation_id,
                    "corpus_sha256": corpus_sha256,
                    "contract_sha256": contract_sha256,
                    "embedding_provider": manifest.embedding_contract.provider,
                    "embedding_model": manifest.embedding_contract.model,
                    "embedding_dimension": str(manifest.embedding_contract.dimension),
                    "embedding_count": str(manifest.embedding_contract.count),
                    "embedding_input_policy_version": EMBEDDING_POLICY_VERSION,
                    "embedding_document_prefix": DOCUMENT_EMBEDDING_PREFIX,
                    "embedding_query_prefix": QUERY_EMBEDDING_PREFIX,
                }
                if any(metadata.get(key) != value for key, value in expected_metadata.items()):
                    raise RuntimeError("sealed serving database metadata does not bind its manifest")
                evidence_count = int(connection.execute("SELECT count(*) FROM evidence").fetchone()[0])
                if evidence_count != manifest.counts.chunks:
                    raise RuntimeError("sealed serving database evidence count differs from manifest")
            finally:
                connection.close()

        await to_thread_fenced(verify_database_binding)
        rows = sealed.get("objects")
        if not isinstance(rows, list):
            raise RuntimeError("sealed CAS object list is invalid")
        expected_references = Counter(
            (
                reference.sha256,
                reference.size_bytes,
                reference.media_type,
                reference.path,
            )
            for document in manifest.documents
            for reference in (document.pdf, document.ocr)
            if reference is not None
        )
        actual_references: Counter[tuple[str, int, str, str]] = Counter()
        validated_objects: list[tuple[Path, str, str, int]] = []
        for raw_row in rows:
            if not isinstance(raw_row, Mapping):
                raise RuntimeError("sealed CAS object row is invalid")
            media_type = str(raw_row.get("media_type") or "")
            path = sealed_file(
                raw_row.get("path"),
                label="CAS object",
                allow_pdf_cache=media_type == "application/pdf",
            )
            digest, size = await to_thread_fenced(sha256_file, path)
            declared_sha = str(raw_row.get("sha256") or "")
            declared_size = raw_row.get("size_bytes")
            if digest != declared_sha or size != declared_size or not media_type:
                raise RuntimeError(f"sealed CAS input changed or is unbound: {path}")
            remote_path = object_path(digest).as_posix()
            actual_references[(digest, size, media_type, remote_path)] += 1
            validated_objects.append((path, media_type, digest, size))
        if actual_references != expected_references:
            raise RuntimeError("sealed CAS rows do not exactly match generation manifest references")
        if canonical_sha256(sealed) != seal_sha256:
            raise RuntimeError("worker publication seal changed during validation")

        return _ValidatedSeal(
            manifest,
            database_path,
            vector_path,
            tuple(validated_objects),
            ocr_cache_publication_deferred,
            v5_metrics,
            seal_sha256,
        )

    async def _publish_remote_only(
        self,
        sealed: Mapping[str, Any],
        *,
        validated: _ValidatedSeal | None = None,
    ) -> tuple[PublishedBundle, _ValidatedSeal]:
        if validated is None:
            validated = await self._validate_local_seal(sealed)
        elif canonical_sha256(sealed) != validated.seal_sha256:
            raise RuntimeError("validated worker publication seal identity changed")
        generation_id = validated.manifest.generation_id

        # No remote mutation occurs until every seal/database/object check above succeeds.
        for path, media_type, declared_sha, declared_size in validated.objects:
            published_digest, remote_path = await self.webdav.put_cas_file(
                path,
                media_type=media_type,
                expected_sha256=declared_sha,
                expected_size_bytes=declared_size,
            )
            if published_digest != declared_sha or remote_path != object_path(published_digest).as_posix():
                raise RuntimeError("CAS publisher returned a mismatched identity")
        published = await WebDAVBundlePublisher(self.webdav).publish(
            generation_id=generation_id,
            database=validated.database_path,
            manifest=sealed["manifest"],
            vectors=validated.vector_path,
        )
        return published, validated

    async def _publish_sealed(
        self,
        run_id: str,
        sealed: Mapping[str, Any],
        *,
        validated: _ValidatedSeal | None = None,
    ) -> PipelineResult:
        if str(sealed.get("run_id") or "") != run_id:
            raise RuntimeError("sealed publication belongs to a different run")
        if validated is None:
            validated = await self._validate_local_seal(sealed)
        elif canonical_sha256(sealed) != validated.seal_sha256:
            raise RuntimeError("validated worker publication seal identity changed")
        current = await self.webdav.validated_current_generation()
        stable_body = await self.webdav.get_bytes(self.webdav.pointer_path)
        if current is None and stable_body is not None:
            raise RuntimeError("remote stable generation is corrupt; refusing sealed publication")
        sealed_manifest = validated.manifest
        if current is not None and (
            current.corpus_sha256 == sealed_manifest.corpus_sha256
            and current.contract_sha256 == sealed_manifest.contract_sha256
        ):
            if current.generation_id == sealed_manifest.generation_id:
                # The exact sealed generation is durable even when its v4
                # manifest explicitly contains isolated OCR failures. Record
                # that publication truth; a later fresh run still bypasses
                # no-change and retries the failed documents.
                if await self._reconcile_remote_bundle(sealed_manifest) is None:
                    raise RuntimeError("same-generation remote publication does not match its local seal")
                self.state.record_publish(
                    generation_id=current.generation_id,
                    run_id=run_id,
                    corpus_sha256=sealed_manifest.corpus_sha256,
                    contract_sha256=sealed_manifest.contract_sha256,
                    serving_sha256=sealed_manifest.serving_database.sha256,
                    status="ready",
                    details={"manifest_sha256": sealed_manifest.manifest_sha256},
                )
                self.state.finish_run(
                    run_id,
                    "succeeded",
                    corpus_sha256=sealed_manifest.corpus_sha256,
                    contract_sha256=sealed_manifest.contract_sha256,
                )
                return PipelineResult(
                    run_id=run_id,
                    status="succeeded",
                    corpus_sha256=sealed_manifest.corpus_sha256,
                    contract_sha256=sealed_manifest.contract_sha256,
                    generation_id=current.generation_id,
                    document_count=sealed_manifest.counts.documents,
                    evidence_count=sealed_manifest.counts.chunks,
                    ocr_cache_publication_deferred=validated.ocr_cache_publication_deferred,
                    v5_metrics=validated.v5_metrics,
                )
            if current.ocr_failed_document_count == 0:
                self.state.finish_run(
                    run_id,
                    "no_change",
                    corpus_sha256=sealed_manifest.corpus_sha256,
                    contract_sha256=sealed_manifest.contract_sha256,
                )
                return PipelineResult(
                    run_id=run_id,
                    status="no_change",
                    corpus_sha256=sealed_manifest.corpus_sha256,
                    contract_sha256=sealed_manifest.contract_sha256,
                    generation_id=current.generation_id,
                    document_count=sealed_manifest.counts.documents,
                    evidence_count=0,
                    ocr_cache_publication_deferred=validated.ocr_cache_publication_deferred,
                    v5_metrics=validated.v5_metrics,
                )
            # A different partial generation with the same corpus is not a
            # no-change proof. Fall through to the predecessor fence, which
            # prevents this stale seal from overwriting that head.
        aligned = await self._align_seal_to_current(
            sealed,
            validated=validated,
            current_generation_id=current.generation_id if current is not None else None,
        )
        publication_failure: _WorkerPhaseFailure | None = None
        try:
            published, published_seal = await self._publish_remote_only(
                aligned,
                validated=validated,
            )
        except Exception as exc:
            # MOVE can commit stable.json immediately before its destination
            # readback fails. Retain only an allowlisted diagnostic snapshot,
            # then reconcile against the fully validated generation and exact
            # manifest bytes.
            category, status_code, error_number = _classify_worker_failure(exc)
            publication_failure = _WorkerPhaseFailure(
                phase="remote_publication",
                error_class_category=category,
                status_code=status_code,
                errno=error_number,
            )
        if publication_failure is not None:
            try:
                reconciled = await self._reconcile_remote_bundle(validated.manifest)
            except Exception:
                raise publication_failure from None
            if reconciled is None:
                raise publication_failure from None
            published = reconciled
            published_seal = validated
        manifest = published_seal.manifest
        self.state.record_publish(
            generation_id=published.generation_id,
            run_id=run_id,
            corpus_sha256=str(aligned["corpus_sha256"]),
            contract_sha256=str(aligned["contract_sha256"]),
            serving_sha256=published.index_sha256,
            status="ready",
            details={"manifest_sha256": published.manifest_sha256},
        )
        self.state.finish_run(
            run_id,
            "succeeded",
            corpus_sha256=str(aligned["corpus_sha256"]),
            contract_sha256=str(aligned["contract_sha256"]),
        )
        return PipelineResult(
            run_id=run_id,
            status="succeeded",
            corpus_sha256=str(aligned["corpus_sha256"]),
            contract_sha256=str(aligned["contract_sha256"]),
            generation_id=published.generation_id,
            document_count=manifest.counts.documents,
            evidence_count=manifest.counts.chunks,
            ocr_cache_publication_deferred=validated.ocr_cache_publication_deferred,
            v5_metrics=published_seal.v5_metrics,
        )


class SealedPublicationResumer(WorkerPipeline):
    """Resume only an already sealed generation, without constructing providers.

    This deliberately reuses the production seal validator, predecessor fence,
    constant-space publisher, and exact remote reconciliation from
    :class:`WorkerPipeline`.  Its constructor does not call ``WorkerPipeline``'s
    provider-bearing constructor and exposes no discovery/OCR/embedding path.
    """

    def __init__(
        self,
        *,
        state: WorkerState,
        state_dir: Path,
        webdav: WebDAVClient,
        stable_publication_approved: bool = False,
        document_aggregation: VerifiedAggregationProfileV5 | None = None,
    ) -> None:
        self._guard_publication_channel(webdav, stable_publication_approved)
        self.state = state
        self.state_dir = state_dir
        self.webdav = webdav
        self.document_aggregation = document_aggregation
        self.stable_publication_approved = stable_publication_approved

    @staticmethod
    def _guard_publication_channel(
        webdav: WebDAVClient,
        stable_publication_approved: bool,
    ) -> None:
        if type(stable_publication_approved) is not bool:
            raise ValueError("stable publication approval must be boolean")
        if webdav.channel == "stable":
            if not stable_publication_approved or not webdav.stable_publication_approved:
                raise ValueError("stable v1.0.14 publication requires explicit approval")
        elif webdav.channel != "candidate-v1.0.11":
            raise ValueError("v1.0.14 Worker publication channel must be candidate-v1.0.11 or stable")

    @property
    def _seal_pdf_cache_objects_root(self) -> Path:
        # Publication-only recovery must not create or mutate cache directories.
        return self.state_dir / "pdf-cache" / "objects" / "sha256"

    @staticmethod
    def _validate_run_id(run_id: str) -> None:
        if re.fullmatch(r"[0-9a-f]{32}", run_id) is None:
            raise ValueError("publication resume run ID is invalid")

    def _load_seal(self, run_id: str) -> tuple[bytes, dict[str, Any]]:
        """Read the exact run seal through a no-follow descriptor chain."""

        self._validate_run_id(run_id)
        nofollow = getattr(os, "O_NOFOLLOW", None)
        nonblock = getattr(os, "O_NONBLOCK", None)
        if nofollow is None or nonblock is None:
            raise RuntimeError("publication resume requires safe file descriptors")
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | nofollow | getattr(os, "O_CLOEXEC", 0)
        file_flags = os.O_RDONLY | nofollow | nonblock | getattr(os, "O_CLOEXEC", 0)
        descriptors: list[int] = []
        try:
            state_descriptor = os.open(self.state_dir, directory_flags)
            descriptors.append(state_descriptor)
            runs_descriptor = os.open("runs", directory_flags, dir_fd=state_descriptor)
            descriptors.append(runs_descriptor)
            run_descriptor = os.open(run_id, directory_flags, dir_fd=runs_descriptor)
            descriptors.append(run_descriptor)
            sealed_descriptor = os.open("sealed", directory_flags, dir_fd=run_descriptor)
            descriptors.append(sealed_descriptor)
            seal_descriptor = os.open("publish.json", file_flags, dir_fd=sealed_descriptor)
            descriptors.append(seal_descriptor)
            initial = os.fstat(seal_descriptor)
            if (
                not stat.S_ISREG(initial.st_mode)
                or initial.st_nlink != 1
                or initial.st_size < 2
                or initial.st_size > MAX_WORKER_SEAL_BYTES
            ):
                raise RuntimeError("resume publication seal is not a bounded regular file")
            body = bytearray()
            while len(body) <= MAX_WORKER_SEAL_BYTES:
                chunk = os.read(
                    seal_descriptor,
                    min(1024 * 1024, MAX_WORKER_SEAL_BYTES + 1 - len(body)),
                )
                if not chunk:
                    break
                body.extend(chunk)
            final = os.fstat(seal_descriptor)
            if (
                len(body) != initial.st_size
                or len(body) > MAX_WORKER_SEAL_BYTES
                or (final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns, final.st_ctime_ns)
                != (
                    initial.st_dev,
                    initial.st_ino,
                    initial.st_size,
                    initial.st_mtime_ns,
                    initial.st_ctime_ns,
                )
            ):
                raise RuntimeError("resume publication seal changed while reading")
        except OSError:
            raise RuntimeError("resume publication seal is unavailable or unsafe") from None
        finally:
            for descriptor in reversed(descriptors):
                with suppress(OSError):
                    os.close(descriptor)
        raw = bytes(body)
        try:
            sealed = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise RuntimeError("resume publication seal is not canonical JSON") from None
        if not isinstance(sealed, dict) or canonical_json_bytes(sealed) != raw:
            raise RuntimeError("resume publication seal is not a canonical JSON object")
        if str(sealed.get("run_id") or "") != run_id:
            raise RuntimeError("resume publication seal belongs to a different run")
        return raw, sealed

    @staticmethod
    def _phase_failure(phase: str, exc: Exception) -> _WorkerPhaseFailure:
        if isinstance(exc, _WorkerPhaseFailure):
            return exc
        category, status_code, error_number = _classify_worker_failure(exc)
        return _WorkerPhaseFailure(
            phase=phase,
            error_class_category=category,
            status_code=status_code,
            errno=error_number,
        )

    async def _reconcile_validated_publication(
        self,
        run_id: str,
        validated: _ValidatedSeal,
    ) -> bool:
        """Reconcile cancellation without repeating the full local seal scan."""

        try:
            manifest = validated.manifest
            if await self._reconcile_remote_bundle(manifest) is None:
                return False
            self.state.record_publish(
                generation_id=manifest.generation_id,
                run_id=run_id,
                corpus_sha256=manifest.corpus_sha256,
                contract_sha256=manifest.contract_sha256,
                serving_sha256=manifest.serving_database.sha256,
                status="ready",
                details={"manifest_sha256": manifest.manifest_sha256},
            )
            self.state.finish_run_if_running(
                run_id,
                "succeeded",
                corpus_sha256=manifest.corpus_sha256,
                contract_sha256=manifest.contract_sha256,
            )
            return True
        except Exception:
            LOGGER.error("Cancelled sealed publication could not be reconciled exactly")
            return False

    async def _resume_publication_locked(self, run_id: str) -> PipelineResult:
        """Validate and publish one failed run's exact immutable local seal."""

        self._validate_run_id(run_id)
        self.state.assert_publication_resumable(run_id)
        validated: _ValidatedSeal | None = None
        unexpected_failure: WorkerUnexpectedFailureError | None = None
        try:
            try:
                seal_body, sealed = self._load_seal(run_id)
                validated = await self._validate_local_seal(sealed)
                expected_generation_id = f"g-{run_id[:24]}-{validated.manifest.corpus_sha256[:12]}"
                if validated.manifest.generation_id != expected_generation_id:
                    raise RuntimeError("sealed generation identity is not bound to its run and corpus")
                if self._load_seal(run_id)[0] != seal_body:
                    raise RuntimeError("resume publication seal changed after validation")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise self._phase_failure("local_seal_validation", exc) from None
            try:
                return await self._publish_sealed(run_id, sealed, validated=validated)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise self._phase_failure("sealed_publication", exc) from None
        except asyncio.CancelledError:
            published = False
            if validated is not None:
                reconciliation = asyncio.create_task(self._reconcile_validated_publication(run_id, validated))
                while not reconciliation.done():
                    try:
                        await asyncio.shield(reconciliation)
                    except asyncio.CancelledError:
                        continue
                    except BaseException:
                        break
                try:
                    published = reconciliation.result()
                except BaseException:
                    published = False
                    LOGGER.error("Cancelled sealed publication reconciliation failed")
            if not published:
                self.state.finish_run_if_running(
                    run_id,
                    "interrupted",
                    error="worker_cancelled: Pipeline execution was interrupted.",
                )
            raise asyncio.CancelledError() from None
        except Exception as exc:
            category, status_code, error_number = _classify_worker_failure(exc)
            failure = WorkerUnexpectedFailureRecord(
                run_id=run_id,
                occurred_at=datetime.now(UTC),
                error_class_category=category,
                phase=exc.phase if isinstance(exc, _WorkerPhaseFailure) else None,
                status_code=status_code,
                errno=error_number,
            )
            report_path = self.state_dir / "runs" / run_id / "reports" / "worker-failure.json"
            error = WorkerUnexpectedFailureError(
                run_id=run_id,
                report_path=report_path,
                failure=failure,
            )
            try:
                _write_worker_failure_report(report_path, failure=failure)
            except Exception:
                LOGGER.error("Worker failure report could not be written")
            self.state.finish_run_if_running(run_id, "failed", error=error.stored_error)
            unexpected_failure = error
        if unexpected_failure is None:
            raise RuntimeError("sealed publication did not reach terminal state")
        raise unexpected_failure from None

    async def run(self, *, resume_run_id: str | None = None) -> PipelineResult:
        del resume_run_id
        raise RuntimeError("sealed publication resumer cannot execute the general pipeline")


async def resume_sealed_publication(
    *,
    run_id: str,
    state_dir: Path,
    webdav: WebDAVClient,
    stable_publication_approved: bool = False,
    document_aggregation: VerifiedAggregationProfileV5 | None = None,
) -> PipelineResult:
    """Lock, open existing state, and run the provider-free publication API."""

    SealedPublicationResumer._validate_run_id(run_id)
    SealedPublicationResumer._guard_publication_channel(webdav, stable_publication_approved)
    with (
        worker_lock(state_dir / "worker.lock"),
        WorkerState(state_dir / "worker-state.sqlite3", create=False) as state,
    ):
        return await SealedPublicationResumer(
            state=state,
            state_dir=state_dir,
            webdav=webdav,
            stable_publication_approved=stable_publication_approved,
            document_aggregation=document_aggregation,
        )._resume_publication_locked(run_id)
