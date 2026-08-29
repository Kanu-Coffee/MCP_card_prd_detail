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
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar

import httpx
from cardrag_core import (
    DOCUMENT_EMBEDDING_PREFIX,
    EMBEDDING_DIMENSION,
    EMBEDDING_POLICY_VERSION,
    QUERY_EMBEDDING_PREFIX,
    ArtifactRef,
    EmbeddingContract,
    GenerationCounts,
    GenerationDocument,
    GenerationManifest,
    GenerationOCRFailure,
    IssuerOCRCounts,
    OCRCacheKind,
    WebDAVError,
    WebDAVHTTPError,
    WebDAVIntegrityError,
    canonical_json_bytes,
    canonical_sha256,
    generation_database_path,
    generation_manifest_path,
    object_path,
    sha256_bytes,
    sha256_file,
)

from .async_utils import to_thread_fenced
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
from .exporter import ServingDatabaseExporter, encode_embedding
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
from .state import WorkerState, retry_delay, worker_lock
from .webdav import PublishedBundle, WebDAVBundlePublisher, WebDAVClient

T = TypeVar("T")
CHUNK_CONTRACT = "cardrag.page-window.v1"
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


@dataclass(frozen=True, slots=True)
class _ProcessedDocument:
    record: DocumentRecord
    pdf_path: Path
    ocr_path: Path
    ocr_sha256: str
    ocr_size_bytes: int
    ocr_cache_kind: OCRCacheKind | None
    ocr_reuse_key: str | None
    chunks: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class _AcquiredDocument:
    source: SourceRecord
    pdf: DownloadedPDF


@dataclass(frozen=True, slots=True)
class _OCRFailedDocument:
    record: OCRFailedProductRecord
    pdf_path: Path


@dataclass(frozen=True, slots=True)
class _ValidatedSeal:
    manifest: GenerationManifest
    database_path: Path
    objects: tuple[tuple[Path, str, str], ...]
    ocr_cache_publication_deferred: int


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


def _classify_worker_failure(exc: Exception) -> tuple[str, int | None, int | None]:
    """Return only allowlisted diagnostic categories and bounded integers."""

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


class WorkerPipeline:
    def __init__(
        self,
        *,
        state: WorkerState,
        state_dir: Path,
        adapters: Sequence[IssuerAdapter],
        ocr: OCRResolver,
        embeddings: EmbeddingProvider,
        webdav: WebDAVClient,
        maximum_attempts: int = 4,
        retry_cap_seconds: float = 30,
        pdf_cache_refresh_hours: float = 168,
        collect_remote_garbage: bool = True,
        retained_generations: int = 2,
        garbage_grace_days: int = 30,
        retained_incomplete_runs: int = 2,
    ) -> None:
        if not adapters:
            raise ValueError("at least one issuer adapter must be enabled")
        if embeddings.dimension != EMBEDDING_DIMENSION:
            raise ValueError(f"embedding dimension must be {EMBEDDING_DIMENSION}")
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
        self.webdav = webdav
        self.maximum_attempts = maximum_attempts
        self.retry_cap_seconds = retry_cap_seconds
        self.pdf_cache_refresh_interval = pdf_cache_refresh_interval
        self.collect_remote_garbage = collect_remote_garbage
        self.retained_generations = retained_generations
        self.garbage_grace_days = garbage_grace_days
        self.retained_incomplete_runs = retained_incomplete_runs
        self.exporter = ServingDatabaseExporter()
        self.limiters = {
            adapter.spec.code: IssuerRateLimiter(adapter.spec.minimum_interval_seconds)
            for adapter in self.adapters
        }

    @property
    def contract_sha256(self) -> str:
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
        unsupported_payload = sorted(
            (item.payload for item in unsupported),
            key=canonical_json_bytes,
        )
        corpus_sha256 = canonical_sha256(
            {
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
        )
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
                    return await finalize_pdf_activity(await self._publish_sealed(run_id, deferred_seal))
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
                for path, media_type, digest in cache_healing_validated_seal.objects
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

        processed: list[_ProcessedDocument] = []
        ocr_failures: list[OCRFailureRecord] = []
        failed_documents: list[_OCRFailedDocument] = []
        ocr_cache_publication_deferred = 0
        ocr_failure_report = run_dir / "reports" / "ocr-failures.json"
        ocr_systemic_failure_report = run_dir / "reports" / "ocr-systemic-failure.json"
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

            try:
                ocr_result = await self._finite_stage(
                    run_id=run_id,
                    document_id=document_id,
                    name="ocr",
                    operation=recognize,
                    non_retryable_predicate=lambda exc: not is_isolatable_document_ocr_failure(exc),
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
            structure_path = run_dir / "documents" / document_id / "structure" / "pages.json"

            async def structure(
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
                operation=structure,
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
                sum(item.record.issuer == adapter.spec.code for item in processed),
                sum(item.record.issuer == adapter.spec.code for item in failed_documents),
            )
            for adapter in sorted(self.adapters, key=lambda item: item.spec.code)
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
        local_root = (self.state_dir / "runs").resolve(strict=True)
        pdf_cache_root = self.pdf_cache.objects_root.resolve(strict=True)

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
        manifest = GenerationManifest.model_validate_json(canonical_json_bytes(sealed["manifest"]))
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
            or manifest.schema_version != GENERATION_SCHEMA_ID
            or manifest.serving_schema != SERVING_SCHEMA_ID
        ):
            raise RuntimeError("sealed generation manifest identity/contract mismatch")
        if manifest.serving_database.path != generation_database_path(generation_id).as_posix():
            raise RuntimeError("sealed serving database has the wrong generation path")
        database_sha, database_size = await asyncio.to_thread(sha256_file, database_path)
        if (
            database_sha != sealed.get("database_sha256")
            or database_sha != manifest.serving_database.sha256
            or database_size != sealed.get("database_size_bytes")
            or database_size != manifest.serving_database.size_bytes
        ):
            raise RuntimeError("sealed serving database hash/size mismatch")

        def verify_database_binding() -> None:
            connection = sqlite3.connect(f"{database_path.as_uri()}?mode=ro&immutable=1", uri=True)
            try:
                integrity = connection.execute("PRAGMA integrity_check").fetchone()
                if integrity is None or integrity[0] != "ok":
                    raise RuntimeError("sealed serving database failed integrity_check")
                metadata = {
                    str(key): str(value)
                    for key, value in connection.execute("SELECT key,value FROM metadata")
                }
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

        await asyncio.to_thread(verify_database_binding)
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
        validated_objects: list[tuple[Path, str, str]] = []
        for raw_row in rows:
            if not isinstance(raw_row, Mapping):
                raise RuntimeError("sealed CAS object row is invalid")
            media_type = str(raw_row.get("media_type") or "")
            path = sealed_file(
                raw_row.get("path"),
                label="CAS object",
                allow_pdf_cache=media_type == "application/pdf",
            )
            digest, size = await asyncio.to_thread(sha256_file, path)
            declared_sha = str(raw_row.get("sha256") or "")
            declared_size = raw_row.get("size_bytes")
            if digest != declared_sha or size != declared_size or not media_type:
                raise RuntimeError(f"sealed CAS input changed or is unbound: {path}")
            remote_path = object_path(digest).as_posix()
            actual_references[(digest, size, media_type, remote_path)] += 1
            validated_objects.append((path, media_type, digest))
        if actual_references != expected_references:
            raise RuntimeError("sealed CAS rows do not exactly match generation manifest references")

        return _ValidatedSeal(
            manifest,
            database_path,
            tuple(validated_objects),
            ocr_cache_publication_deferred,
        )

    async def _publish_remote_only(self, sealed: Mapping[str, Any]) -> tuple[PublishedBundle, _ValidatedSeal]:
        validated = await self._validate_local_seal(sealed)
        generation_id = validated.manifest.generation_id

        # No remote mutation occurs until every seal/database/object check above succeeds.
        for path, media_type, declared_sha in validated.objects:
            published_digest, remote_path = await self.webdav.put_cas_file(path, media_type=media_type)
            if published_digest != declared_sha or remote_path != object_path(published_digest).as_posix():
                raise RuntimeError("CAS publisher returned a mismatched identity")
        published = await WebDAVBundlePublisher(self.webdav).publish(
            generation_id=generation_id,
            database=validated.database_path,
            manifest=sealed["manifest"],
        )
        return published, validated

    async def _publish_sealed(self, run_id: str, sealed: Mapping[str, Any]) -> PipelineResult:
        if str(sealed.get("run_id") or "") != run_id:
            raise RuntimeError("sealed publication belongs to a different run")
        validated = await self._validate_local_seal(sealed)
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
                )
            # A different partial generation with the same corpus is not a
            # no-change proof. Fall through to the predecessor fence, which
            # prevents this stale seal from overwriting that head.
        aligned = await self._align_seal_to_current(
            sealed,
            validated=validated,
            current_generation_id=current.generation_id if current is not None else None,
        )
        publication_failed = False
        try:
            published, published_seal = await self._publish_remote_only(aligned)
        except Exception:
            # MOVE can commit stable.json immediately before its destination
            # readback fails. Drop the raw source exception, then reconcile
            # against the fully validated generation and exact manifest bytes.
            publication_failed = True
        if publication_failed:
            reconciled = await self._reconcile_remote_bundle(validated.manifest)
            if reconciled is None:
                raise RuntimeError(
                    "remote publication failed and its stable commit could not be reconciled"
                ) from None
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
        )
