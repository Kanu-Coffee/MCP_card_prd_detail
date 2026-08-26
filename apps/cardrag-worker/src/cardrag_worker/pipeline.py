"""One finite latest-only pipeline from discovery to an immutable serving bundle."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import struct
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, TypeVar

import httpx
from cardrag_core import (
    DOCUMENT_EMBEDDING_PREFIX,
    EMBEDDING_DIMENSION,
    EMBEDDING_POLICY_VERSION,
    QUERY_EMBEDDING_PREFIX,
    STABLE_POINTER_PATH,
    ArtifactRef,
    EmbeddingContract,
    GenerationCounts,
    GenerationDocument,
    GenerationManifest,
    OCRCacheKind,
    canonical_json_bytes,
    canonical_sha256,
    generation_database_path,
    object_path,
    sha256_bytes,
    sha256_file,
)

from .contracts import (
    SERVING_SCHEMA_ID,
    DocumentRecord,
    EvidenceRecord,
    IssuerAdapter,
    PageRecord,
    ProtectedSourceAllowance,
    SourceRecord,
    SourceSnapshot,
    UnsupportedProductRecord,
)
from .downloader import (
    DownloadedPDF,
    DownloadPolicy,
    ProtectedDocumentError,
    SecurePDFDownloader,
    validate_pdf,
)
from .exporter import ServingDatabaseExporter, encode_embedding
from .ocr import OCRResolver, OCRResult, page_records
from .providers import EmbeddingProvider
from .rate_limit import IssuerRateLimiter, RateLimitedClient
from .state import WorkerState, retry_delay, worker_lock
from .webdav import PublishedBundle, WebDAVBundlePublisher, WebDAVClient

T = TypeVar("T")
CHUNK_CONTRACT = "cardrag.page-window.v1"


class CorpusConflictError(RuntimeError):
    pass


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
class _ValidatedSeal:
    manifest: GenerationManifest
    database_path: Path
    objects: tuple[tuple[Path, str, str], ...]


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
    return (
        "\n\n".join(f"## Page {page}\n\n{text}" for page, text in enumerate(result.pages, 1)) + "\n"
    ).encode("utf-8")


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
        collect_remote_garbage: bool = True,
        retained_generations: int = 3,
        garbage_grace_days: int = 30,
        retained_incomplete_runs: int = 10,
    ) -> None:
        if not adapters:
            raise ValueError("at least one issuer adapter must be enabled")
        if embeddings.dimension != EMBEDDING_DIMENSION:
            raise ValueError(f"embedding dimension must be {EMBEDDING_DIMENSION}")
        if retained_generations < 1 or garbage_grace_days < 1 or retained_incomplete_runs < 1:
            raise ValueError("garbage retention and grace must be positive")
        self.state = state
        self.state_dir = state_dir
        self.adapters = tuple(adapters)
        self.ocr = ocr
        self.embeddings = embeddings
        self.webdav = webdav
        self.maximum_attempts = maximum_attempts
        self.retry_cap_seconds = retry_cap_seconds
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
    ) -> T:
        maximum = maximum_attempts or self.maximum_attempts
        self.state.ensure_stage(run_id, document_id, name, max_attempts=maximum)
        row = self.state.get_stage(run_id, document_id, name)
        if row is not None and row.status == "succeeded":
            return await operation()
        while True:
            attempt = self.state.stage_started(run_id, document_id, name)
            try:
                result = await operation()
            except Exception as exc:
                if non_retryable_predicate is not None and non_retryable_predicate(exc):
                    raise
                delay = retry_delay(
                    attempt,
                    base_seconds=retry_base_seconds,
                    cap_seconds=self.retry_cap_seconds,
                )
                status = self.state.stage_failed(run_id, document_id, name, exc, delay_seconds=delay)
                if status == "failed":
                    raise
                await asyncio.sleep(delay)
            else:
                self.state.stage_succeeded(run_id, document_id, name)
                return result

    async def run(self, *, resume_run_id: str | None = None) -> PipelineResult:
        with worker_lock(self.state_dir / "worker.lock"):
            run_id = resume_run_id or self.state.start_run()
            if resume_run_id:
                self.state.assert_resumable(run_id)
            self._cleanup_local_runs(exclude_run_id=run_id)
            try:
                result = await self._run_locked(
                    run_id,
                    refresh_sources=resume_run_id is not None,
                )
            except Exception as exc:
                self.state.finish_run(run_id, "failed", error=str(exc))
                raise
            gc_status: str | None = None
            gc_deleted = 0
            gc_error: str | None = None
            if self.collect_remote_garbage:
                try:
                    from .gc import collect_garbage

                    gc_result = await collect_garbage(
                        webdav=self.webdav,
                        state=self.state,
                        apply=True,
                        retain_generations=self.retained_generations,
                        grace_days=self.garbage_grace_days,
                    )
                    gc_status = "succeeded"
                    gc_deleted = len(gc_result.deleted)
                except Exception as exc:
                    # Publication/no-change is already durable. GC is fail-closed
                    # and reported independently rather than rewriting run truth.
                    gc_status = "failed"
                    gc_error = str(exc)
            self._cleanup_local_runs(exclude_run_id=run_id)
            return replace(
                result,
                unsupported_document_count=self.state.stage_status_count(run_id, "download", "skipped"),
                gc_status=gc_status,
                gc_deleted=gc_deleted,
                gc_error=gc_error,
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

        download_root = run_dir / ("resume-downloads" if refresh_sources else "downloads")
        if refresh_sources and download_root.exists():
            if download_root.is_symlink() or not download_root.is_dir():
                raise RuntimeError("resume download staging is not a regular directory")
            resolved_download_root = download_root.resolve(strict=True)
            if resolved_download_root.parent != run_dir.resolve(strict=True):
                raise RuntimeError("resume download staging escaped its run directory")
            shutil.rmtree(resolved_download_root)

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
        async with httpx.AsyncClient(follow_redirects=False, timeout=60) as client:
            for source in records:
                adapter = next(item for item in self.adapters if item.spec.code == source.issuer)
                limited = RateLimitedClient(client, self.limiters[adapter.spec.code])
                source_key = source.source_id
                download_path = download_root / f"{source_key}.pdf"

                async def acquire(
                    adapter: IssuerAdapter = adapter,
                    source: SourceRecord = source,
                    destination: Path = download_path,
                    current_client: RateLimitedClient = limited,
                ) -> DownloadedPDF:
                    if destination.exists():
                        digest, size, pages = validate_pdf(destination)
                        return DownloadedPDF(destination, digest, size, pages, source.source_url)
                    request = await adapter.prepare_download(current_client, source)  # type: ignore[arg-type]
                    downloader = SecurePDFDownloader(DownloadPolicy(allowed_hosts=adapter.spec.allowed_hosts))
                    return await downloader.download(current_client, request, destination)  # type: ignore[arg-type]

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
                    continue
                acquired.append(_AcquiredDocument(source, pdf))
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
            ):
                return await self._publish_sealed(run_id, deferred_seal)
        existing = self.state.ready_publish(corpus_sha256, contract_sha256)
        current_remote = await self.webdav.validated_current_generation()
        stable_body = await self.webdav.get_bytes(STABLE_POINTER_PATH)
        if current_remote is not None and (
            current_remote.corpus_sha256 == corpus_sha256
            and current_remote.contract_sha256 == contract_sha256
        ):
            self.state.finish_run(
                run_id,
                "no_change",
                corpus_sha256=corpus_sha256,
                contract_sha256=contract_sha256,
            )
            return PipelineResult(
                run_id,
                "no_change",
                corpus_sha256,
                contract_sha256,
                current_remote.generation_id,
                len(acquired),
                0,
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
            await self._publish_remote_only(prior_seal)
            self.state.finish_run(
                run_id,
                "no_change",
                corpus_sha256=corpus_sha256,
                contract_sha256=contract_sha256,
            )
            return PipelineResult(
                run_id,
                "no_change",
                corpus_sha256,
                contract_sha256,
                generation_id,
                len(acquired),
                0,
            )
        # A different fully valid stable generation wins. Rebuild a new
        # generation for this recurrence and chain it to that current head;
        # OCR/embeddings are reused through their content caches below.

        processed: list[_ProcessedDocument] = []
        for acquired_document in acquired:
            source = acquired_document.source
            pdf = acquired_document.pdf
            document_id = source.document_id(pdf.sha256)
            ocr_output_dir = run_dir / "documents" / document_id / "ocr"

            async def recognize(
                current_document_id: str = document_id,
                current_pdf: DownloadedPDF = pdf,
                current_output_dir: Path = ocr_output_dir,
            ) -> OCRResult:
                return await self.ocr.resolve(
                    run_id=run_id,
                    document_id=current_document_id,
                    pdf_path=current_pdf.path,
                    pdf_sha256=current_pdf.sha256,
                    pdf_size_bytes=current_pdf.size_bytes,
                    page_count=current_pdf.page_count,
                    output_dir=current_output_dir,
                )

            ocr_result = await self._finite_stage(
                run_id=run_id,
                document_id=document_id,
                name="ocr",
                operation=recognize,
            )
            ocr_body = _canonical_ocr_body(ocr_result)
            if hashlib.sha256(ocr_body).hexdigest() != ocr_result.ocr_sha256:
                raise RuntimeError("OCR result bytes changed after verification")
            ocr_path = ocr_output_dir / "ocr.md"
            if not ocr_path.exists() or ocr_path.read_bytes() != ocr_body:
                _atomic_write(ocr_path, ocr_body)
            pages = page_records(document_id, ocr_result)
            structure_path = run_dir / "documents" / document_id / "structure" / "pages.json"

            async def structure(
                current_pages: tuple[PageRecord, ...] = pages,
                current_ocr_sha256: str = ocr_result.ocr_sha256,
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
                current_ocr_sha256: str = ocr_result.ocr_sha256,
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
                    ocr_sha256=ocr_result.ocr_sha256,
                    ocr_size_bytes=ocr_result.size_bytes,
                    ocr_cache_kind=ocr_result.cache_kind,
                    ocr_reuse_key=ocr_result.cache_reuse_key,
                    chunks=chunks,
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
            embedding_provider=self.embeddings.provider,
            embedding_model=self.embeddings.model,
            issuers=[adapter.spec for adapter in self.adapters],
            documents=[document.record for document in processed],
            evidence=evidence,
            unsupported_products=unsupported,
            extra_metadata={
                "contract_sha256": contract_sha256,
            },
        )
        current_remote = await self.webdav.validated_current_generation()
        if current_remote is None and await self.webdav.get_bytes(STABLE_POINTER_PATH) is not None:
            raise RuntimeError("remote stable generation is corrupt; refusing publication")
        previous_id = current_remote.generation_id if current_remote is not None else None
        generation_documents = tuple(
            sorted(
                (
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
                    )
                    for document in processed
                ),
                key=lambda row: row.document_id,
            )
        )
        manifest = GenerationManifest(
            generation_id=generation_id,
            created_at=datetime.now(UTC),
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
            issuer_codes=tuple(sorted({row.record.issuer for row in processed})),
            counts=GenerationCounts(
                documents=len(processed),
                pdf_objects=len({row.record.pdf_sha256 for row in processed}),
                ocr_objects=len({row.ocr_sha256 for row in processed}),
                chunks=len(evidence),
            ),
            documents=generation_documents,
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
                    "path": str(document.ocr_path),
                    "sha256": document.ocr_sha256,
                    "size_bytes": document.ocr_size_bytes,
                    "media_type": "text/markdown; charset=utf-8",
                }
                for document in processed
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
        local_root = (self.state_dir / "runs").resolve(strict=True)

        def sealed_file(raw_path: object, *, label: str) -> Path:
            candidate = Path(str(raw_path))
            if candidate.is_symlink() or not candidate.is_file():
                raise RuntimeError(f"sealed {label} is not a regular non-symlink file")
            resolved = candidate.resolve(strict=True)
            if not resolved.is_relative_to(local_root):
                raise RuntimeError(f"sealed {label} escapes the worker run directory")
            return resolved

        database_path = sealed_file(sealed.get("database_path"), label="serving database")
        if not isinstance(sealed.get("manifest"), Mapping):
            raise RuntimeError("sealed generation manifest is not an object")
        manifest = GenerationManifest.model_validate_json(canonical_json_bytes(sealed["manifest"]))
        generation_id = str(sealed.get("generation_id") or "")
        corpus_sha256 = str(sealed.get("corpus_sha256") or "")
        contract_sha256 = str(sealed.get("contract_sha256") or "")
        if (
            manifest.generation_id != generation_id
            or manifest.corpus_sha256 != corpus_sha256
            or manifest.contract_sha256 != contract_sha256
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
            path = sealed_file(raw_row.get("path"), label="CAS object")
            digest, size = await asyncio.to_thread(sha256_file, path)
            declared_sha = str(raw_row.get("sha256") or "")
            declared_size = raw_row.get("size_bytes")
            media_type = str(raw_row.get("media_type") or "")
            if digest != declared_sha or size != declared_size or not media_type:
                raise RuntimeError(f"sealed CAS input changed or is unbound: {path}")
            remote_path = object_path(digest).as_posix()
            actual_references[(digest, size, media_type, remote_path)] += 1
            validated_objects.append((path, media_type, digest))
        if actual_references != expected_references:
            raise RuntimeError("sealed CAS rows do not exactly match generation manifest references")

        return _ValidatedSeal(manifest, database_path, tuple(validated_objects))

    async def _publish_remote_only(
        self, sealed: Mapping[str, Any]
    ) -> tuple[PublishedBundle, GenerationManifest]:
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
        return published, validated.manifest

    async def _publish_sealed(self, run_id: str, sealed: Mapping[str, Any]) -> PipelineResult:
        if str(sealed.get("run_id") or "") != run_id:
            raise RuntimeError("sealed publication belongs to a different run")
        validated = await self._validate_local_seal(sealed)
        current = await self.webdav.validated_current_generation()
        stable_body = await self.webdav.get_bytes(STABLE_POINTER_PATH)
        if current is None and stable_body is not None:
            raise RuntimeError("remote stable generation is corrupt; refusing sealed publication")
        sealed_manifest = validated.manifest
        if current is not None and (
            current.corpus_sha256 == sealed_manifest.corpus_sha256
            and current.contract_sha256 == sealed_manifest.contract_sha256
        ):
            if current.generation_id == sealed_manifest.generation_id:
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
                )
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
            )
        aligned = await self._align_seal_to_current(
            sealed,
            validated=validated,
            current_generation_id=current.generation_id if current is not None else None,
        )
        published, manifest = await self._publish_remote_only(aligned)
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
        )
