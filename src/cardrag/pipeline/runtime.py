"""Production offline stage handlers wired through durable PostgreSQL jobs."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from cardrag.acquisition import DownloadPolicy, PDFValidationError, SecurePDFDownloader
from cardrag.acquisition.download import DownloadSecurityError
from cardrag.config import Settings
from cardrag.db import Postgres
from cardrag.domain import (
    ArtifactManifest,
    ArtifactType,
    Issuer,
    Lineage,
    ManifestAttribute,
    canonical_sha256,
)
from cardrag.issuers import DiscoveryMode, SourceRecord
from cardrag.issuers.base import IssuerMarkupChanged, UnsupportedCategory
from cardrag.issuers.registry import adapter_for
from cardrag.jobs import ClaimedJob, JobRepository, LostLeaseError
from cardrag.observability import (
    Observability,
    PostgresMetricRollupWriter,
    WorkerMaintenance,
    bind_context,
    get_observability,
    hash_identifier,
    log_event,
)
from cardrag.pdf import PDF_RENDERER_ID
from cardrag.pipeline.chunks import CHUNK_POLICY_VERSION, EvidenceChunk, build_chunks
from cardrag.pipeline.ocr import (
    OCR_PROMPT_VERSION,
    CodexExecBackend,
    OCRPageCheckpoint,
    OCRProcessor,
    OCRResumeCheckpoint,
    OpenRouterOCRBackend,
    render_pdf,
    split_pages,
)
from cardrag.pipeline.structure import STRUCTURE_SCHEMA_VERSION, extract_structure
from cardrag.search.embeddings import OpenRouterEmbeddingProvider
from cardrag.storage import ContentAddressedObjectStore

StageHandler = Callable[[ClaimedJob], Awaitable[None]]

ISSUER_MIN_INTERVAL_SECONDS = {
    Issuer.WOORI: 0.75,
    Issuer.KB: 0.75,
    Issuer.SHINHAN: 1.0,
}
ISSUER_RETRY_BASE_SECONDS = {
    Issuer.WOORI: 5.0,
    Issuer.KB: 5.0,
    Issuer.SHINHAN: 10.0,
}


class PermanentStageError(RuntimeError):
    pass


PERMANENT_PIPELINE_ERRORS = (
    IssuerMarkupChanged,
    UnsupportedCategory,
    PDFValidationError,
    DownloadSecurityError,
)


def allow_daily_ocr_fallback(
    *,
    bulk: bool,
    attempt_no: int,
    max_attempts: int,
    has_api_key: bool,
) -> bool:
    """Switch provider only after the durable Codex retry budget is exhausted."""

    return not bulk and has_api_key and attempt_no >= max_attempts


def authoritative_is_latest(source: SourceRecord) -> bool:
    """Trust the issuer adapter's current-listing marker, never history ordering."""

    return source.is_current


def validate_discovery_volume(
    *,
    issuer: Issuer,
    observed: int,
    absolute_minimum: int,
    previous_observed: int | None,
    minimum_previous_ratio: float,
) -> None:
    """Fail closed before a suspicious snapshot can tombstone catalog rows."""

    ratio_minimum = (
        0 if previous_observed is None else int(previous_observed * minimum_previous_ratio + 0.999999)
    )
    required = max(absolute_minimum, ratio_minimum)
    if observed < required:
        raise IssuerMarkupChanged(
            f"{issuer.value} discovery yielded {observed}; anomaly threshold is {required}"
        )


def attempt_provenance(claim: ClaimedJob, pipeline: OfflinePipeline) -> tuple[str, str, str]:
    """Stable execution identity recorded on every successful durable attempt."""

    settings = pipeline.settings
    if claim.stage == "ocr":
        actual = claim.payload.get("_successful_ocr_provenance")
        if isinstance(actual, dict):
            provider = str(actual["provider"])
            model = str(actual["model"])
            config_hash = str(actual["config_hash"])
            return provider, model, config_hash
        provider = "codex-exec"
        model = settings.ocr_model
        config = {
            "chunk_pages": settings.ocr_chunk_pages,
            "prompt_version": OCR_PROMPT_VERSION,
            "reasoning_effort": settings.ocr_reasoning_effort if provider == "codex-exec" else None,
            "renderer": PDF_RENDERER_ID,
            "render_scale": settings.render_scale,
        }
    elif claim.stage == "index":
        provider = "openrouter"
        model = settings.embedding_model
        config = {"chunk_policy": CHUNK_POLICY_VERSION, "dimension": settings.embedding_dimension}
    else:
        provider = "cardrag"
        model = claim.stage
        config = {"stage": claim.stage}
    return provider, model, canonical_sha256(config)


class PostgresIssuerRateLimiter:
    """Reserve issuer-specific request slots across all worker processes."""

    def __init__(
        self,
        database: Postgres,
        intervals: dict[Issuer, float] | None = None,
        *,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.database = database
        self.intervals = intervals or ISSUER_MIN_INTERVAL_SECONDS
        self.sleeper = sleeper

    async def wait(self, issuer: Issuer) -> None:
        interval = self.intervals[issuer]
        with self.database.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (f"cardrag-rate-limit:{issuer.value}",),
            )
            cursor.execute(
                """
                INSERT INTO issuer_rate_limits(issuer, next_allowed_at)
                VALUES (%s, clock_timestamp())
                ON CONFLICT (issuer) DO NOTHING
                """,
                (issuer.value,),
            )
            cursor.execute(
                """
                SELECT GREATEST(0.0, EXTRACT(EPOCH FROM (next_allowed_at - clock_timestamp())))
                           AS wait_seconds
                FROM issuer_rate_limits WHERE issuer=%s FOR UPDATE
                """,
                (issuer.value,),
            )
            row = cursor.fetchone()
            if row is None:
                raise RuntimeError("issuer rate-limit reservation disappeared")
            cursor.execute(
                """
                UPDATE issuer_rate_limits
                SET next_allowed_at=GREATEST(next_allowed_at, clock_timestamp())
                                    + make_interval(secs => %s),
                    updated_at=clock_timestamp()
                WHERE issuer=%s
                """,
                (interval, issuer.value),
            )
            connection.commit()
        wait_seconds = float(row["wait_seconds"])
        if wait_seconds > 0:
            await self.sleeper(wait_seconds)

    def event_hook(self, issuer: Issuer) -> Callable[[httpx.Request], Awaitable[None]]:
        async def throttle(_: httpx.Request) -> None:
            await self.wait(issuer)

        return throttle


def http_retry_policy(exc: httpx.HTTPError, issuer: Issuer) -> tuple[bool, float, float]:
    """Return retryable/base-delay/minimum-delay for one issuer HTTP failure."""

    base = ISSUER_RETRY_BASE_SECONDS[issuer]
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        retryable = status == 429 or status >= 500
        retry_after = exc.response.headers.get("retry-after")
        try:
            minimum = max(0.0, float(retry_after)) if retry_after is not None else 0.0
        except ValueError:
            minimum = 0.0
        return retryable, base, minimum
    return isinstance(exc, (httpx.TimeoutException, httpx.TransportError)), base, 0.0


class OfflinePipeline:
    def __init__(self, settings: Settings, database: Postgres, jobs: JobRepository) -> None:
        self.settings = settings
        self.database = database
        self.jobs = jobs
        self.objects = ContentAddressedObjectStore(settings.storage_root)
        self.rate_limiter = PostgresIssuerRateLimiter(database)
        self.observability = get_observability(service="worker", environment=settings.environment)
        self.handlers: dict[str, StageHandler] = {
            "discover": self.discover,
            "download": self.download,
            "ocr": self.ocr,
            "structure": self.structure,
            "index": self.index,
            "materialize": self.materialize,
        }

    async def handle(self, claim: ClaimedJob) -> None:
        try:
            handler = self.handlers[claim.stage]
        except KeyError as exc:
            raise PermanentStageError(f"unknown pipeline stage: {claim.stage}") from exc
        await handler(claim)

    async def discover(self, claim: ClaimedJob) -> None:
        issuer = Issuer(claim.issuer)
        absolute_minimum = self.settings.issuer_discovery_minimum(issuer.value)
        adapter = adapter_for(issuer, expected_minimum=absolute_minimum)
        mode = DiscoveryMode(str(claim.payload.get("mode") or "current"))
        raw_categories = claim.payload.get("categories")
        categories = frozenset(map(str, raw_categories)) if raw_categories else None
        timeout = httpx.Timeout(self.settings.issuer_request_timeout_seconds)
        limits = httpx.Limits(max_connections=4, max_keepalive_connections=2)
        async with httpx.AsyncClient(
            timeout=timeout,
            limits=limits,
            follow_redirects=False,
            headers={"User-Agent": "CardRAG-MCP/0.1 (+operator-approved disclosure collector)"},
            event_hooks={"request": [self.rate_limiter.event_hook(issuer)]},
        ) as client:
            snapshot = await adapter.discover(client, mode=mode, categories=categories)
        with self.database.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT observed_count FROM source_snapshots
                WHERE issuer=%s AND discovery_mode=%s
                ORDER BY completed_at DESC, snapshot_id DESC LIMIT 1
                """,
                (issuer.value, mode.value),
            )
            previous = cursor.fetchone()
        validate_discovery_volume(
            issuer=issuer,
            observed=snapshot.observed_count,
            absolute_minimum=absolute_minimum,
            previous_observed=(int(previous["observed_count"]) if previous is not None else None),
            minimum_previous_ratio=self.settings.discovery_minimum_previous_ratio,
        )
        snapshot_body = (
            json.dumps(snapshot.model_dump(mode="json"), ensure_ascii=False, sort_keys=True) + "\n"
        ).encode()
        snapshot_object = self.objects.put_bytes(snapshot_body)
        generation_id = str(claim.payload.get("generation_id") or "")
        with self.database.connection() as connection, connection.cursor() as cursor:
            self._assert_current(claim, cursor)
            cursor.execute(
                """
                INSERT INTO source_snapshots(snapshot_id, issuer, discovery_mode, parser_version, source_url,
                                             observed_count, payload_sha256, created_at, completed_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (snapshot_id) DO NOTHING
                """,
                (
                    snapshot.snapshot_id,
                    issuer.value,
                    mode.value,
                    snapshot.parser_version,
                    str(snapshot.source_url),
                    snapshot.observed_count,
                    snapshot_object.sha256,
                    snapshot.started_at,
                    snapshot.finished_at,
                ),
            )
            if not generation_id:
                raise PermanentStageError("discovery job has no candidate generation")
            cursor.execute(
                """
                INSERT INTO generation_snapshots(
                    generation_id, issuer, snapshot_id, discovery_mode, completed_at
                ) VALUES (%s,%s,%s,%s,%s)
                ON CONFLICT (generation_id, issuer) DO UPDATE SET
                    snapshot_id=EXCLUDED.snapshot_id,
                    discovery_mode=EXCLUDED.discovery_mode,
                    completed_at=EXCLUDED.completed_at
                """,
                (generation_id, issuer.value, snapshot.snapshot_id, mode.value, snapshot.finished_at),
            )
            seen_ids: list[str] = []
            for record in snapshot.records:
                identity = record.document_identity
                document_id = identity.stable_id
                seen_ids.append(document_id)
                cursor.execute(
                    """
                    INSERT INTO source_documents(document_id, discovery_id, issuer, product_code, product_name, document_type,
                                                 effective_date, source_version, version_sort_key, source_snapshot_id,
                                                 source_url, last_seen_at, metadata)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s::jsonb)
                    ON CONFLICT (document_id) DO UPDATE SET
                        product_name = EXCLUDED.product_name, source_snapshot_id = EXCLUDED.source_snapshot_id,
                        source_url = EXCLUDED.source_url, last_seen_at = EXCLUDED.last_seen_at,
                        tombstoned_at = NULL,
                        metadata = source_documents.metadata || EXCLUDED.metadata
                    """,
                    (
                        document_id,
                        document_id,
                        issuer.value,
                        record.product_code,
                        record.product_name,
                        record.document_type,
                        record.effective_date,
                        record.source_version,
                        json.dumps(identity.version_sort_key),
                        snapshot.snapshot_id,
                        str(record.source_url),
                        snapshot.finished_at,
                        json.dumps({"source": record.model_dump(mode="json")}, ensure_ascii=False),
                    ),
                )
                cursor.execute(
                    """
                    INSERT INTO generation_expected_documents(
                        generation_id, discovery_id, issuer, source_snapshot_id, discovery_mode,
                        is_current, product_code, document_type, effective_date, source_version, source_url,
                        discovered_at
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (generation_id, discovery_id) DO UPDATE SET
                        source_snapshot_id=EXCLUDED.source_snapshot_id,
                        discovery_mode=EXCLUDED.discovery_mode,
                        is_current=EXCLUDED.is_current,
                        product_code=EXCLUDED.product_code,
                        document_type=EXCLUDED.document_type,
                        effective_date=EXCLUDED.effective_date,
                        source_version=EXCLUDED.source_version,
                        source_url=EXCLUDED.source_url,
                        discovered_at=EXCLUDED.discovered_at
                    """,
                    (
                        generation_id,
                        document_id,
                        issuer.value,
                        snapshot.snapshot_id,
                        mode.value,
                        record.is_current,
                        record.product_code,
                        record.document_type,
                        record.effective_date,
                        record.source_version,
                        str(record.source_url),
                        record.discovered_at,
                    ),
                )
            if mode == DiscoveryMode.CURRENT and seen_ids:
                cursor.execute(
                    """
                    UPDATE source_documents SET tombstoned_at = now()
                    WHERE issuer = %s AND tombstoned_at IS NULL AND NOT (discovery_id = ANY(%s))
                      AND source_snapshot_id <> %s
                    """,
                    (issuer.value, seen_ids, snapshot.snapshot_id),
                )
                cursor.execute(
                    """
                    UPDATE generation_documents gd SET is_latest=false, updated_at=now()
                    WHERE gd.generation_id=%s AND gd.issuer=%s AND gd.is_latest
                      AND NOT EXISTS (
                          SELECT 1 FROM source_documents d
                          WHERE d.document_id=gd.document_id AND d.tombstoned_at IS NULL
                            AND d.discovery_id=ANY(%s)
                      )
                    """,
                    (generation_id, issuer.value, seen_ids),
                )
                cursor.execute(
                    """
                    UPDATE evidence e SET is_latest=false
                    WHERE e.generation_id=%s AND e.issuer=%s AND e.is_latest
                      AND NOT EXISTS (
                          SELECT 1 FROM source_documents d
                          WHERE d.document_id=e.document_id AND d.tombstoned_at IS NULL
                            AND d.discovery_id=ANY(%s)
                      )
                    """,
                    (generation_id, issuer.value, seen_ids),
                )
            connection.commit()
        for record in snapshot.records:
            identity = record.document_identity
            acquisition_scope = generation_id or str(claim.payload.get("run_id") or claim.id)
            self.jobs.enqueue_child(
                claim,
                stage="download",
                document_id=identity.stable_id,
                # Re-fetch once per run so a source that silently changes bytes
                # behind the same filename/version is detected by SHA-256.
                idempotency_key=f"download:{acquisition_scope}:{identity.stable_id}",
                payload={
                    "run_id": str(claim.payload.get("run_id") or ""),
                    "source": record.model_dump(mode="json"),
                    "source_snapshot_id": snapshot.snapshot_id,
                    "source_snapshot_sha256": snapshot_object.sha256,
                    "generation_id": generation_id,
                    "bulk": bool(claim.payload.get("bulk")),
                },
                max_attempts=self.settings.max_job_attempts,
            )

    async def download(self, claim: ClaimedJob) -> None:
        source = SourceRecord.model_validate(claim.payload["source"])
        adapter = adapter_for(source.issuer)
        request_source = source
        method = "GET"
        form: dict[str, str] | None = None
        if source.issuer == Issuer.WOORI:
            url, form = adapter.download_request(source)  # type: ignore[attr-defined]
            request_source = source.model_copy(update={"source_url": url})
            method = "POST"
        elif source.issuer == Issuer.SHINHAN:
            form = adapter.download_form(source)  # type: ignore[attr-defined]
            method = "POST"
        policy = DownloadPolicy(
            allowed_hosts=adapter.allowed_hosts,
            maximum_bytes=self.settings.issuer_max_download_bytes,
            timeout_seconds=self.settings.issuer_request_timeout_seconds,
        )
        downloader = SecurePDFDownloader(policy)
        temp_root = self.settings.build_root / "downloads"
        temp_root.mkdir(parents=True, exist_ok=True)
        temp_pdf = temp_root / f"{claim.id}-{claim.fencing_token}.pdf"
        async with httpx.AsyncClient(
            follow_redirects=False,
            trust_env=False,
            event_hooks={"request": [self.rate_limiter.event_hook(source.issuer)]},
        ) as client:
            downloaded = await downloader.download(client, request_source, temp_pdf, method=method, form=form)
        try:
            stored = self.objects.put_file(downloaded.path)
        finally:
            temp_pdf.unlink(missing_ok=True)
        if claim.document_id is None:
            raise PermanentStageError("download job has no document ID")
        discovery_id = claim.document_id
        final_identity = source.document_identity_for(stored.sha256)
        final_document_id = final_identity.stable_id
        target_generation = str(claim.payload.get("generation_id") or "")
        if not target_generation:
            raise PermanentStageError("download job has no candidate generation")
        snapshot_sha256 = str(claim.payload.get("source_snapshot_sha256") or "")
        pdf_manifest = ArtifactManifest(
            artifact_type=ArtifactType.SOURCE_PDF,
            content_sha256=stored.sha256,
            size_bytes=stored.size_bytes,
            media_type=downloaded.media_type,
            created_at=datetime.now(UTC),
            lineage=Lineage(
                processor=f"{source.issuer.value}-secure-download",
                processor_version=adapter.parser_version,
                config_sha256=canonical_sha256(
                    {
                        "allowed_hosts": sorted(adapter.allowed_hosts),
                        "maximum_bytes": policy.maximum_bytes,
                        "timeout_seconds": policy.timeout_seconds,
                    }
                ),
                input_sha256=(snapshot_sha256,) if len(snapshot_sha256) == 64 else (),
                source_snapshot_id=str(claim.payload.get("source_snapshot_id") or "unknown"),
            ),
            document=final_identity,
            page_count=downloaded.page_count,
            attributes=(
                ManifestAttribute(name="final_url", value=downloaded.final_url),
                ManifestAttribute(name="source_post_id", value=source.source_post_id),
            ),
        )
        with self.database.connection() as connection, connection.cursor() as cursor:
            self._assert_current(claim, cursor)
            cursor.execute(
                """
                DELETE FROM source_documents WHERE document_id = %s AND pdf_sha256 IS NULL
                """,
                (discovery_id,),
            )
            cursor.execute(
                """
                INSERT INTO source_documents(
                    document_id, discovery_id, issuer, product_code, product_name, document_type,
                    effective_date, source_version, version_sort_key, source_snapshot_id, source_url,
                    pdf_sha256, raw_object_key, last_seen_at, tombstoned_at, metadata
                )
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,now(),NULL,%s::jsonb)
                ON CONFLICT (document_id) DO UPDATE SET
                    product_name=EXCLUDED.product_name,
                    source_snapshot_id=EXCLUDED.source_snapshot_id,
                    source_url=EXCLUDED.source_url,
                    last_seen_at=EXCLUDED.last_seen_at,
                    tombstoned_at=NULL,
                    metadata=source_documents.metadata || EXCLUDED.metadata
                """,
                (
                    final_document_id,
                    discovery_id,
                    source.issuer.value,
                    source.product_code,
                    source.product_name,
                    source.document_type,
                    source.effective_date,
                    source.source_version,
                    json.dumps(final_identity.version_sort_key),
                    str(claim.payload.get("source_snapshot_id") or "unknown"),
                    str(source.source_url),
                    stored.sha256,
                    stored.relative_path.as_posix(),
                    json.dumps(
                        {
                            "source": source.model_dump(mode="json"),
                            "pdf_size": stored.size_bytes,
                            "page_count": downloaded.page_count,
                            "discovery_id": discovery_id,
                        },
                        ensure_ascii=False,
                    ),
                ),
            )
            # Issuer discovery is authoritative. A history row with a newer
            # date/sort key must never demote the listing's explicit current row.
            is_latest = authoritative_is_latest(source)
            if is_latest:
                cursor.execute(
                    """
                    UPDATE generation_documents SET is_latest=false, updated_at=now()
                    WHERE generation_id=%s AND issuer=%s AND product_code=%s
                      AND document_type=%s AND document_id<>%s AND is_latest
                    """,
                    (
                        target_generation,
                        source.issuer.value,
                        source.product_code,
                        source.document_type,
                        final_document_id,
                    ),
                )
                cursor.execute(
                    """
                    UPDATE evidence SET is_latest=false
                    WHERE generation_id=%s AND issuer=%s AND product_code=%s
                      AND document_type=%s AND document_id<>%s AND is_latest
                    """,
                    (
                        target_generation,
                        source.issuer.value,
                        source.product_code,
                        source.document_type,
                        final_document_id,
                    ),
                )
            cursor.execute(
                """
                INSERT INTO generation_documents(
                    generation_id, document_id, issuer, product_code, product_name, document_type,
                    effective_date, source_version, version_sort_key, source_snapshot_id, source_url,
                    discovered_at, pdf_sha256, raw_object_key, pdf_size_bytes, pdf_page_count,
                    is_latest
                )
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (generation_id, document_id) DO UPDATE SET
                    product_name=EXCLUDED.product_name,
                    source_snapshot_id=EXCLUDED.source_snapshot_id,
                    source_url=EXCLUDED.source_url,
                    discovered_at=EXCLUDED.discovered_at,
                    pdf_sha256=EXCLUDED.pdf_sha256,
                    raw_object_key=EXCLUDED.raw_object_key,
                    pdf_size_bytes=EXCLUDED.pdf_size_bytes,
                    pdf_page_count=EXCLUDED.pdf_page_count,
                    is_latest=EXCLUDED.is_latest,
                    updated_at=now()
                """,
                (
                    target_generation,
                    final_document_id,
                    source.issuer.value,
                    source.product_code,
                    source.product_name,
                    source.document_type,
                    source.effective_date,
                    source.source_version,
                    json.dumps(final_identity.version_sort_key),
                    str(claim.payload.get("source_snapshot_id") or "unknown"),
                    str(source.source_url),
                    source.discovered_at,
                    stored.sha256,
                    stored.relative_path.as_posix(),
                    stored.size_bytes,
                    downloaded.page_count,
                    is_latest,
                ),
            )
            self._record_artifact(cursor, target_generation, final_document_id, pdf_manifest)
            cursor.execute(
                """
                SELECT
                    d.ocr_object_key AS reusable_ocr_object_key,
                    d.ocr_sha256 AS reusable_ocr_sha256,
                    d.ocr_sha256 IS NOT NULL
                    AND cardrag_ocr_manifest_reusable(
                        d.ocr_manifest, d.pdf_sha256, d.ocr_sha256,
                        %s, %s, %s, %s, %s, %s, %s
                    )
                    AND (
                        d.ocr_manifest->>'schema_version' IS DISTINCT FROM 'cardrag.legacy-ocr-adoption.v1'
                        OR cardrag_legacy_adoption_bound(
                            d.ocr_manifest, d.document_id, d.pdf_sha256, d.ocr_sha256,
                            ARRAY['processing','finalizing','succeeded']::text[]
                        )
                    ) AS reusable_ocr,
                    d.structure_schema_version=%s
                    AND d.chunk_policy=%s
                    AND d.embedding_provider='openrouter'
                    AND d.embedding_model=target_generation.embedding_model
                    AND d.embedding_dimension=target_generation.embedding_dimension
                    AND d.generation_id<>target_generation.generation_id
                    AND EXISTS (
                        SELECT 1 FROM evidence e
                        WHERE e.generation_id=d.generation_id AND e.document_id=d.document_id
                          AND e.embedding IS NOT NULL
                    ) AS reusable_evidence
                FROM generations target_generation
                JOIN LATERAL (
                    SELECT candidate.*
                    FROM generation_documents candidate
                    WHERE candidate.document_id=%s
                      AND (
                          candidate.generation_id=target_generation.generation_id
                          OR candidate.generation_id=(
                              SELECT generation_id FROM active_generation WHERE singleton=true
                          )
                      )
                    -- Prefer the published source so its evidence can be
                    -- materialized. The target row is only the legacy-import
                    -- fallback when no active document exists.
                    ORDER BY (candidate.generation_id=target_generation.generation_id) ASC
                    LIMIT 1
                ) d ON true
                WHERE target_generation.generation_id=%s
                """,
                (
                    OCR_PROMPT_VERSION,
                    PDF_RENDERER_ID,
                    self.settings.ocr_reasoning_effort,
                    self.settings.render_scale,
                    self.settings.ocr_chunk_pages,
                    self.settings.ocr_model,
                    self.settings.ocr_fallback_model,
                    STRUCTURE_SCHEMA_VERSION,
                    CHUNK_POLICY_VERSION,
                    final_document_id,
                    target_generation,
                ),
            )
            compatibility = cursor.fetchone()
            reusable_ocr = bool(compatibility and compatibility["reusable_ocr"])
            reusable_evidence = bool(
                compatibility and compatibility["reusable_ocr"] and compatibility["reusable_evidence"]
            )
            reusable_ocr_object_key = (
                str(compatibility["reusable_ocr_object_key"])
                if reusable_ocr and compatibility and compatibility["reusable_ocr_object_key"]
                else None
            )
            reusable_ocr_sha256 = (
                str(compatibility["reusable_ocr_sha256"])
                if reusable_ocr and compatibility and compatibility["reusable_ocr_sha256"]
                else None
            )
            if not reusable_evidence:
                cursor.execute(
                    "DELETE FROM evidence WHERE generation_id=%s AND document_id=%s",
                    (target_generation, final_document_id),
                )
                if reusable_ocr:
                    cursor.execute(
                        """
                        UPDATE generation_documents SET structured_sha256=NULL,
                            structured_object_key=NULL, structure_schema_version=NULL,
                            embedding_provider=NULL, embedding_model=NULL,
                            embedding_dimension=NULL, chunk_policy=NULL, chunk_count=NULL,
                            embedding_count=NULL, index_count=NULL, updated_at=now()
                        WHERE generation_id=%s AND document_id=%s
                        """,
                        (target_generation, final_document_id),
                    )
                    cursor.execute(
                        """
                        DELETE FROM generation_artifacts WHERE generation_id=%s AND document_id=%s
                          AND artifact_type IN ('structured','embedding','lexical_index','vector_index')
                        """,
                        (target_generation, final_document_id),
                    )
                else:
                    cursor.execute(
                        """
                        UPDATE generation_documents SET ocr_sha256=NULL, ocr_object_key=NULL,
                            ocr_pages=NULL, ocr_manifest=NULL, structured_sha256=NULL,
                            structured_object_key=NULL, structure_schema_version=NULL,
                            embedding_provider=NULL, embedding_model=NULL,
                            embedding_dimension=NULL, chunk_policy=NULL, chunk_count=NULL,
                            embedding_count=NULL, index_count=NULL, updated_at=now()
                        WHERE generation_id=%s AND document_id=%s
                        """,
                        (target_generation, final_document_id),
                    )
                    cursor.execute(
                        """
                        DELETE FROM generation_artifacts WHERE generation_id=%s AND document_id=%s
                          AND artifact_type IN (
                              'ocr_markdown','ocr_page_map','structured','embedding',
                              'lexical_index','vector_index'
                          )
                        """,
                        (target_generation, final_document_id),
                    )
            connection.commit()
        child_payload = {
            **claim.payload,
            "pdf_sha256": stored.sha256,
            "raw_object_key": stored.relative_path.as_posix(),
        }
        scope = str(claim.payload.get("generation_id") or claim.id)
        if reusable_evidence:
            self.jobs.enqueue_child(
                claim,
                stage="materialize",
                document_id=final_document_id,
                idempotency_key=f"materialize:{scope}:{final_document_id}:{stored.sha256}",
                payload=child_payload,
                max_attempts=self.settings.max_job_attempts,
            )
        elif reusable_ocr_object_key and reusable_ocr_sha256:
            self.jobs.enqueue_child(
                claim,
                stage="structure",
                document_id=final_document_id,
                idempotency_key=(
                    f"structure:{scope}:{final_document_id}:{reusable_ocr_sha256}:structured-document.v1"
                ),
                payload={
                    **child_payload,
                    "ocr_object_key": reusable_ocr_object_key,
                    "ocr_sha256": reusable_ocr_sha256,
                },
                max_attempts=self.settings.max_job_attempts,
            )
        else:
            self.jobs.enqueue_child(
                claim,
                stage="ocr",
                document_id=final_document_id,
                idempotency_key=(
                    f"ocr:{scope}:{final_document_id}:{stored.sha256}:{self.settings.codex_bin}"
                ),
                payload=child_payload,
                max_attempts=self.settings.max_job_attempts,
            )

    async def ocr(self, claim: ClaimedJob) -> None:
        if claim.document_id is None:
            raise PermanentStageError("OCR job has no document ID")
        object_key = Path(str(claim.payload["raw_object_key"]))
        pdf_path = (self.settings.storage_root / object_key).resolve(strict=True)
        if not pdf_path.is_relative_to(self.settings.storage_root.resolve()):
            raise PermanentStageError("OCR source escaped storage root")
        # A stale and a reclaimed worker may briefly overlap until fencing is
        # observed.  Never let their page checkpoints or provider attempts
        # share a filesystem namespace; only the fenced DB commit can publish
        # the winner's immutable object.
        attempt_root = (
            self.settings.build_root
            / "ocr"
            / str(claim.generation_id or claim.payload.get("generation_id") or "no-generation")
            / claim.document_id
            / f"{claim.id}-{claim.attempt_no}-{claim.fencing_token}"
        )
        rendered = render_pdf(pdf_path, attempt_root / "rendered", scale=self.settings.render_scale)
        primary = CodexExecBackend(
            executable=self.settings.codex_bin,
            model=self.settings.ocr_model,
            timeout_seconds=self.settings.ocr_timeout_seconds,
            auth_root=self.settings.codex_auth_root,
            reasoning_effort=self.settings.ocr_reasoning_effort,
        )
        fallback = None
        api_key = self.settings.secret_text_from_file(self.settings.openrouter_api_key_file)
        if api_key and allow_daily_ocr_fallback(
            bulk=bool(claim.payload.get("bulk")),
            attempt_no=claim.attempt_no,
            max_attempts=claim.max_attempts,
            has_api_key=True,
        ):
            fallback = OpenRouterOCRBackend(
                api_key=api_key,
                model=self.settings.ocr_fallback_model,
                base_url=str(self.settings.openrouter_base_url),
                timeout_seconds=self.settings.ocr_timeout_seconds,
            )
        processor = OCRProcessor(chunk_pages=self.settings.ocr_chunk_pages)
        completed_pages: set[int] = set()
        total_pages = len(rendered.page_images)

        with self.database.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT unit_key, input_hash, output_hash, artifact_uri
                FROM stage_checkpoints
                WHERE job_id=%s AND attempt_no<%s
                ORDER BY attempt_no DESC, completed_at DESC
                """,
                (claim.id, claim.attempt_no),
            )
            checkpoint_rows = cursor.fetchall()
        resumable: dict[tuple[str, int, str], OCRResumeCheckpoint] = {}
        storage_root = self.settings.storage_root.resolve()
        for row in checkpoint_rows:
            unit_parts = str(row["unit_key"]).rsplit(":page:", 1)
            if len(unit_parts) != 2 or not unit_parts[1].isdigit():
                continue
            artifact_path = (self.settings.storage_root / str(row["artifact_uri"])).resolve()
            if not artifact_path.is_relative_to(storage_root) or not artifact_path.is_file():
                continue
            key = (unit_parts[0], int(unit_parts[1]), str(row["input_hash"]))
            resumable.setdefault(
                key,
                OCRResumeCheckpoint(
                    input_hash=str(row["input_hash"]),
                    output_hash=str(row["output_hash"]),
                    path=artifact_path,
                ),
            )

        def resume_page(
            provider: str,
            page: int,
            input_hash: str,
        ) -> OCRResumeCheckpoint | None:
            return resumable.get((provider, page, input_hash))

        def save_page_checkpoint(page: OCRPageCheckpoint) -> None:
            stored_page = self.objects.put_file(page.path)
            if stored_page.sha256 != page.output_hash:
                raise RuntimeError("OCR page checkpoint hash changed before storage")
            self.jobs.save_checkpoint(
                claim,
                unit_key=f"{page.provider}:page:{page.page}",
                input_hash=page.input_hash,
                output_hash=page.output_hash,
                artifact_uri=stored_page.relative_path.as_posix(),
            )
            completed_pages.add(page.page)
            self.observability.metrics.set_job_progress(
                issuer=claim.issuer,
                stage="ocr",
                completed=len(completed_pages),
                total=total_pages,
            )

        manifest = await processor.process(
            document_id=claim.document_id,
            rendered=rendered,
            output_dir=attempt_root / "result",
            primary=primary,
            fallback=fallback,
            bulk=bool(claim.payload.get("bulk")),
            checkpoint=save_page_checkpoint,
            resume=resume_page,
            durable_attempt=claim.attempt_no,
        )
        ocr_path = attempt_root / "result" / "ocr.md"
        stored_ocr = self.objects.put_file(ocr_path)
        pages = split_pages(ocr_path.read_text(encoding="utf-8"))
        source = SourceRecord.model_validate(claim.payload["source"])
        document = source.document_identity_for(str(claim.payload["pdf_sha256"]))
        generation_id = str(claim.payload.get("generation_id") or "")
        if not generation_id:
            raise PermanentStageError("OCR job has no candidate generation")
        ocr_artifact = ArtifactManifest(
            artifact_type=ArtifactType.OCR_MARKDOWN,
            content_sha256=stored_ocr.sha256,
            size_bytes=stored_ocr.size_bytes,
            media_type="text/markdown; charset=utf-8",
            created_at=datetime.now(UTC),
            lineage=Lineage(
                processor="page-addressed-ocr",
                processor_version=manifest.schema_version,
                config_sha256=canonical_sha256(
                    {
                        "bulk": bool(claim.payload.get("bulk")),
                        "chunk_pages": self.settings.ocr_chunk_pages,
                        "model": manifest.attempt.model,
                        "prompt_sha256": manifest.attempt.prompt_sha256,
                        "provider": manifest.attempt.provider,
                        "reasoning_effort": manifest.attempt.reasoning_effort,
                        "renderer": manifest.attempt.renderer,
                        "render_scale": self.settings.render_scale,
                    }
                ),
                input_sha256=(str(claim.payload["pdf_sha256"]),),
                source_snapshot_id=str(claim.payload.get("source_snapshot_id") or "unknown"),
                prompt_version=manifest.attempt.prompt_version,
                provider=manifest.attempt.provider,
                model=manifest.attempt.model,
                attempt=claim.attempt_no,
            ),
            document=document,
            page_count=len(pages),
            attributes=(
                ManifestAttribute(name="durable_job_attempt", value=claim.attempt_no),
                ManifestAttribute(name="ocr_chars", value=manifest.ocr_chars),
                ManifestAttribute(name="provider_attempt", value=manifest.successful_attempt),
                ManifestAttribute(name="renderer", value=manifest.attempt.renderer),
                ManifestAttribute(
                    name="reasoning_effort",
                    value=manifest.attempt.reasoning_effort or "none",
                ),
            ),
        )
        page_map_body = (json.dumps(pages, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        stored_page_map = self.objects.put_bytes(page_map_body)
        page_map_artifact = ArtifactManifest(
            artifact_type=ArtifactType.OCR_PAGE_MAP,
            content_sha256=stored_page_map.sha256,
            size_bytes=stored_page_map.size_bytes,
            media_type="application/json",
            created_at=datetime.now(UTC),
            lineage=Lineage(
                processor="ocr-page-map",
                processor_version="ocr-page-map.v1",
                config_sha256=canonical_sha256({"page_marker": "## Page N"}),
                input_sha256=(stored_ocr.sha256,),
                source_snapshot_id=str(claim.payload.get("source_snapshot_id") or "unknown"),
            ),
            document=document,
            page_count=len(pages),
        )
        with self.database.connection() as connection, connection.cursor() as cursor:
            self._assert_current(claim, cursor)
            cursor.execute(
                """
                UPDATE source_documents SET metadata = metadata || %s::jsonb WHERE document_id = %s
                """,
                (
                    json.dumps(
                        {
                            "ocr_object_key": stored_ocr.relative_path.as_posix(),
                            "ocr_sha256": stored_ocr.sha256,
                            "ocr_pages": pages,
                            "ocr_manifest": manifest.model_dump(mode="json"),
                        },
                        ensure_ascii=False,
                    ),
                    claim.document_id,
                ),
            )
            cursor.execute(
                """
                UPDATE generation_documents SET
                    ocr_sha256=%s, ocr_object_key=%s, ocr_pages=%s::jsonb,
                    ocr_manifest=%s::jsonb, updated_at=now()
                WHERE generation_id=%s AND document_id=%s
                RETURNING document_id
                """,
                (
                    stored_ocr.sha256,
                    stored_ocr.relative_path.as_posix(),
                    json.dumps(pages, ensure_ascii=False),
                    json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False),
                    generation_id,
                    claim.document_id,
                ),
            )
            if cursor.fetchone() is None:
                raise PermanentStageError("generation PDF provenance is missing before OCR")
            self._record_artifact(cursor, generation_id, claim.document_id, ocr_artifact)
            self._record_artifact(cursor, generation_id, claim.document_id, page_map_artifact)
            connection.commit()
        self.jobs.enqueue_child(
            claim,
            stage="structure",
            document_id=claim.document_id,
            idempotency_key=(
                f"structure:{claim.generation_id or claim.id}:{claim.document_id}:"
                f"{stored_ocr.sha256}:structured-document.v1"
            ),
            payload={
                **claim.payload,
                "ocr_object_key": stored_ocr.relative_path.as_posix(),
                "ocr_sha256": stored_ocr.sha256,
            },
            max_attempts=self.settings.max_job_attempts,
        )
        claim.payload["_successful_ocr_provenance"] = {
            "config_hash": ocr_artifact.lineage.config_sha256,
            "model": manifest.attempt.model,
            "provider": manifest.attempt.provider,
            "renderer": manifest.attempt.renderer,
        }

    async def structure(self, claim: ClaimedJob) -> None:
        if claim.document_id is None:
            raise PermanentStageError("structure job has no document ID")
        ocr_path = (self.settings.storage_root / str(claim.payload["ocr_object_key"])).resolve(strict=True)
        if not ocr_path.is_relative_to(self.settings.storage_root.resolve()):
            raise PermanentStageError("OCR object escaped storage root")
        text = ocr_path.read_text(encoding="utf-8")
        structured = extract_structure(claim.document_id, text)
        source = SourceRecord.model_validate(claim.payload["source"])
        chunks = build_chunks(
            structured,
            issuer=source.issuer,
            product_code=source.product_code,
            product_name=source.product_name,
            document_version=source.source_version,
            effective_date=source.effective_date.isoformat(),
            ocr_text=text,
        )
        body = (
            json.dumps(structured.model_dump(mode="json"), ensure_ascii=False, sort_keys=True) + "\n"
        ).encode()
        structured_object = self.objects.put_bytes(body)
        generation_id = str(claim.payload.get("generation_id") or "")
        if not generation_id:
            raise PermanentStageError("structure job has no candidate generation")
        document = source.document_identity_for(str(claim.payload["pdf_sha256"]))
        structure_artifact = ArtifactManifest(
            artifact_type=ArtifactType.STRUCTURED,
            content_sha256=structured_object.sha256,
            size_bytes=structured_object.size_bytes,
            media_type="application/json",
            created_at=datetime.now(UTC),
            lineage=Lineage(
                processor="evidence-bound-structure",
                processor_version=STRUCTURE_SCHEMA_VERSION,
                config_sha256=canonical_sha256(
                    {"chunk_policy": CHUNK_POLICY_VERSION, "extractor": "deterministic-rule.v1"}
                ),
                input_sha256=(str(claim.payload["ocr_sha256"]),),
                source_snapshot_id=str(claim.payload.get("source_snapshot_id") or "unknown"),
            ),
            document=document,
            item_count=len(structured.facts),
            attributes=(ManifestAttribute(name="validation_status", value=structured.validation_status),),
        )
        with self.database.connection() as connection, connection.cursor() as cursor:
            self._assert_current(claim, cursor)
            # A policy/model rebuild may produce stable evidence IDs. Replace
            # candidate rows atomically so ON CONFLICT cannot preserve stale vectors.
            cursor.execute(
                "DELETE FROM evidence WHERE generation_id=%s AND document_id=%s",
                (generation_id, claim.document_id),
            )
            cursor.execute(
                """
                DELETE FROM generation_artifacts WHERE generation_id=%s AND document_id=%s
                  AND artifact_type IN ('embedding','lexical_index','vector_index')
                """,
                (generation_id, claim.document_id),
            )
            cursor.execute(
                """
                UPDATE source_documents SET metadata=metadata || %s::jsonb WHERE document_id=%s
                """,
                (
                    json.dumps(
                        {
                            "structured_sha256": structured_object.sha256,
                            "structured_object_key": structured_object.relative_path.as_posix(),
                            "structure_schema_version": STRUCTURE_SCHEMA_VERSION,
                        }
                    ),
                    claim.document_id,
                ),
            )
            cursor.execute(
                """
                UPDATE generation_documents SET
                    structured_sha256=%s, structured_object_key=%s,
                    structure_schema_version=%s, updated_at=now()
                WHERE generation_id=%s AND document_id=%s AND ocr_sha256=%s
                RETURNING document_id
                """,
                (
                    structured_object.sha256,
                    structured_object.relative_path.as_posix(),
                    STRUCTURE_SCHEMA_VERSION,
                    generation_id,
                    claim.document_id,
                    str(claim.payload["ocr_sha256"]),
                ),
            )
            if cursor.fetchone() is None:
                raise PermanentStageError("generation OCR provenance is missing before structure")
            self._record_artifact(cursor, generation_id, claim.document_id, structure_artifact)
            connection.commit()
        self.jobs.enqueue_child(
            claim,
            stage="index",
            document_id=claim.document_id,
            idempotency_key=(
                f"index:{claim.generation_id or claim.id}:{claim.document_id}:{structured_object.sha256}:"
                f"{self.settings.embedding_model}:{self.settings.embedding_dimension}"
            ),
            payload={
                **claim.payload,
                "structured_sha256": structured_object.sha256,
                "structured_object_key": structured_object.relative_path.as_posix(),
                "chunks": [chunk.model_dump(mode="json") for chunk in chunks],
            },
            max_attempts=self.settings.max_job_attempts,
        )

    async def index(self, claim: ClaimedJob) -> None:
        if claim.document_id is None:
            raise PermanentStageError("index job has no document ID")
        generation_id = str(claim.payload.get("generation_id") or "")
        if not generation_id:
            raise PermanentStageError("index job has no candidate generation")
        chunks = [EvidenceChunk.model_validate(item) for item in claim.payload["chunks"]]
        api_key = self.settings.secret_text_from_file(self.settings.openrouter_api_key_file)
        if not api_key:
            raise RuntimeError("OpenRouter embedding secret is unavailable")
        embedder = OpenRouterEmbeddingProvider(
            api_key=api_key,
            model=self.settings.embedding_model,
            dimension=self.settings.embedding_dimension,
            base_url=str(self.settings.openrouter_base_url),
            timeout_seconds=self.settings.embedding_timeout_seconds,
        )
        vectors = await embedder.embed_documents([chunk.text for chunk in chunks])
        structured_sha256 = str(claim.payload["structured_sha256"])
        source = SourceRecord.model_validate(claim.payload["source"])
        document = source.document_identity_for(str(claim.payload["pdf_sha256"]))
        embedding_body = (
            json.dumps(
                {
                    "schema_version": "cardrag-embedding-catalog.v1",
                    "document_id": claim.document_id,
                    "evidence_ids": [chunk.evidence_id for chunk in chunks],
                    "embedding_provider": "openrouter",
                    "embedding_model": self.settings.embedding_model,
                    "embedding_dimension": self.settings.embedding_dimension,
                    "chunk_policy": CHUNK_POLICY_VERSION,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode()
        stored_embedding_catalog = self.objects.put_bytes(embedding_body)
        embedding_artifact = ArtifactManifest(
            artifact_type=ArtifactType.EMBEDDING,
            content_sha256=stored_embedding_catalog.sha256,
            size_bytes=stored_embedding_catalog.size_bytes,
            media_type="application/json",
            created_at=datetime.now(UTC),
            lineage=Lineage(
                processor="openrouter-document-embedding",
                processor_version="openrouter-embeddings.v1",
                config_sha256=canonical_sha256(
                    {
                        "provider": "openrouter",
                        "model": self.settings.embedding_model,
                        "dimension": self.settings.embedding_dimension,
                        "chunk_policy": CHUNK_POLICY_VERSION,
                    }
                ),
                input_sha256=(structured_sha256,),
                source_snapshot_id=str(claim.payload.get("source_snapshot_id") or "unknown"),
                provider="openrouter",
                model=self.settings.embedding_model,
            ),
            document=document,
            item_count=len(vectors),
        )
        with self.database.connection() as connection, connection.cursor() as cursor:
            self._assert_current(claim, cursor)
            # A retry may use the same stable evidence IDs with newly produced
            # vectors. Replace candidate rows atomically before inserting.
            cursor.execute(
                "DELETE FROM evidence WHERE generation_id=%s AND document_id=%s",
                (generation_id, claim.document_id),
            )
            cursor.execute(
                """
                DELETE FROM generation_artifacts WHERE generation_id=%s AND document_id=%s
                  AND artifact_type IN ('embedding','lexical_index','vector_index')
                """,
                (generation_id, claim.document_id),
            )
            # download persisted the adapter's authoritative ``is_current``
            # decision on this generation row.  Never infer it from history
            # dates/version ordering at a later stage.
            cursor.execute(
                """
                SELECT is_latest FROM generation_documents
                WHERE generation_id=%s AND document_id=%s
                FOR UPDATE
                """,
                (generation_id, claim.document_id),
            )
            row = cursor.fetchone()
            if row is None:
                raise PermanentStageError("index document is absent")
            is_latest = bool(row["is_latest"])
            if is_latest:
                cursor.execute(
                    """
                    UPDATE evidence SET is_latest=false
                    WHERE generation_id=%s AND issuer=%s AND product_code=%s AND document_type=%s
                      AND document_id<>%s AND is_latest
                    """,
                    (
                        generation_id,
                        source.issuer.value,
                        source.product_code,
                        source.document_type,
                        claim.document_id,
                    ),
                )
            for chunk, vector in zip(chunks, vectors, strict=True):
                cursor.execute(
                    """
                    INSERT INTO evidence(generation_id, evidence_id, document_id, issuer, product_code,
                                         product_name, document_type, effective_date, source_version,
                                         section_type, page_start, page_end, span_start, span_end,
                                         source_spans, text, text_sha256, confidence, is_latest, embedding)
                    VALUES (%s,%s,%s,%s,%s,%s,'product_description',%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,1.0,%s,%s::vector)
                    ON CONFLICT (generation_id, evidence_id) DO NOTHING
                    """,
                    (
                        generation_id,
                        chunk.evidence_id,
                        chunk.document_id,
                        chunk.issuer.value,
                        chunk.product_code,
                        chunk.product_name,
                        chunk.effective_date,
                        chunk.document_version,
                        chunk.section_type,
                        chunk.page_start,
                        chunk.page_end,
                        chunk.span_start,
                        chunk.span_end,
                        json.dumps(
                            [span.model_dump(mode="json") for span in chunk.source_spans],
                            sort_keys=True,
                        ),
                        chunk.text,
                        chunk.text_sha256,
                        is_latest,
                        "[" + ",".join(format(value, ".17g") for value in vector) + "]",
                    ),
                )
            cursor.execute(
                "SELECT count(*)::int AS n FROM evidence WHERE generation_id=%s AND document_id=%s AND embedding IS NOT NULL",
                (generation_id, claim.document_id),
            )
            count_row = cursor.fetchone()
            indexed_count = int(count_row["n"]) if count_row is not None else -1
            if indexed_count != len(chunks):
                raise RuntimeError("indexed evidence count differs from generated chunk count")
            cursor.execute(
                """
                UPDATE generation_documents SET
                    embedding_provider='openrouter', embedding_model=%s, embedding_dimension=%s,
                    chunk_policy=%s, chunk_count=%s, embedding_count=%s, index_count=%s,
                    is_latest=%s, updated_at=now()
                WHERE generation_id=%s AND document_id=%s AND structured_sha256=%s
                RETURNING document_id
                """,
                (
                    self.settings.embedding_model,
                    self.settings.embedding_dimension,
                    CHUNK_POLICY_VERSION,
                    len(chunks),
                    len(vectors),
                    indexed_count,
                    is_latest,
                    generation_id,
                    claim.document_id,
                    structured_sha256,
                ),
            )
            if cursor.fetchone() is None:
                raise PermanentStageError("generation structure provenance is missing before index")
            self._record_artifact(cursor, generation_id, claim.document_id, embedding_artifact)
            for artifact_type, processor in (
                (ArtifactType.LEXICAL_INDEX, "postgres-tsvector"),
                (ArtifactType.VECTOR_INDEX, "pgvector-hnsw"),
            ):
                self._record_artifact(
                    cursor,
                    generation_id,
                    claim.document_id,
                    embedding_artifact.model_copy(
                        update={
                            "artifact_type": artifact_type,
                            "lineage": embedding_artifact.lineage.model_copy(update={"processor": processor}),
                        }
                    ),
                )
            connection.commit()

    async def materialize(self, claim: ClaimedJob) -> None:
        """Copy immutable evidence into a new generation without re-running OCR/embedding."""
        if claim.document_id is None or claim.generation_id is None:
            raise PermanentStageError("materialize job has no document or target generation")
        with self.database.connection() as connection, connection.cursor() as cursor:
            self._assert_current(claim, cursor)
            cursor.execute(
                """
                SELECT source.generation_id
                FROM active_generation source
                JOIN generations old ON old.generation_id=source.generation_id
                JOIN generations target ON target.generation_id=%s
                JOIN generation_documents d
                  ON d.generation_id=source.generation_id AND d.document_id=%s
                WHERE old.embedding_provider=target.embedding_provider
                  AND old.embedding_model=target.embedding_model
                  AND old.embedding_dimension=target.embedding_dimension
                  AND d.structure_schema_version=%s AND d.chunk_policy=%s
                  AND cardrag_ocr_manifest_reusable(
                      d.ocr_manifest, d.pdf_sha256, d.ocr_sha256,
                      %s, %s, %s, %s, %s, %s, %s
                  )
                  AND (
                      d.ocr_manifest->>'schema_version' IS DISTINCT FROM 'cardrag.legacy-ocr-adoption.v1'
                      OR cardrag_legacy_adoption_bound(
                          d.ocr_manifest, d.document_id, d.pdf_sha256, d.ocr_sha256,
                          ARRAY['processing','finalizing','succeeded']::text[]
                      )
                  )
                FOR SHARE OF source, old, target, d
                """,
                (
                    claim.generation_id,
                    claim.document_id,
                    STRUCTURE_SCHEMA_VERSION,
                    CHUNK_POLICY_VERSION,
                    OCR_PROMPT_VERSION,
                    PDF_RENDERER_ID,
                    self.settings.ocr_reasoning_effort,
                    self.settings.render_scale,
                    self.settings.ocr_chunk_pages,
                    self.settings.ocr_model,
                    self.settings.ocr_fallback_model,
                ),
            )
            compatible = cursor.fetchone()
            if compatible is None:
                connection.rollback()
                raise PermanentStageError("published evidence processing contract is incompatible")
            source_generation_id = str(compatible["generation_id"])
            cursor.execute(
                """
                INSERT INTO generation_documents(
                    generation_id, document_id, issuer, product_code, product_name, document_type,
                    effective_date, source_version, version_sort_key, source_snapshot_id, source_url,
                    discovered_at, pdf_sha256, raw_object_key, pdf_size_bytes, pdf_page_count,
                    ocr_sha256, ocr_object_key, ocr_pages, ocr_manifest,
                    structured_sha256, structured_object_key, structure_schema_version,
                    embedding_provider, embedding_model, embedding_dimension, chunk_policy,
                    chunk_count, embedding_count, index_count, is_latest,
                    materialized_from_generation_id
                )
                SELECT %s, d.document_id, d.issuer, d.product_code, d.product_name, d.document_type,
                       d.effective_date, d.source_version, d.version_sort_key, %s,
                       d.source_url, d.discovered_at, d.pdf_sha256, d.raw_object_key,
                       d.pdf_size_bytes, d.pdf_page_count, d.ocr_sha256, d.ocr_object_key,
                       d.ocr_pages, d.ocr_manifest, d.structured_sha256, d.structured_object_key,
                       d.structure_schema_version, d.embedding_provider, d.embedding_model,
                       d.embedding_dimension, d.chunk_policy, d.chunk_count, d.embedding_count,
                       d.index_count, d.is_latest, %s
                FROM generation_documents d
                WHERE d.generation_id=%s AND d.document_id=%s
                ON CONFLICT (generation_id, document_id) DO UPDATE SET
                    issuer=EXCLUDED.issuer,
                    product_code=EXCLUDED.product_code,
                    product_name=EXCLUDED.product_name,
                    document_type=EXCLUDED.document_type,
                    effective_date=EXCLUDED.effective_date,
                    source_version=EXCLUDED.source_version,
                    version_sort_key=EXCLUDED.version_sort_key,
                    source_url=EXCLUDED.source_url,
                    discovered_at=EXCLUDED.discovered_at,
                    ocr_sha256=EXCLUDED.ocr_sha256,
                    ocr_object_key=EXCLUDED.ocr_object_key,
                    ocr_pages=EXCLUDED.ocr_pages,
                    ocr_manifest=EXCLUDED.ocr_manifest,
                    structured_sha256=EXCLUDED.structured_sha256,
                    structured_object_key=EXCLUDED.structured_object_key,
                    structure_schema_version=EXCLUDED.structure_schema_version,
                    embedding_provider=EXCLUDED.embedding_provider,
                    embedding_model=EXCLUDED.embedding_model,
                    embedding_dimension=EXCLUDED.embedding_dimension,
                    chunk_policy=EXCLUDED.chunk_policy,
                    chunk_count=EXCLUDED.chunk_count,
                    embedding_count=EXCLUDED.embedding_count,
                    index_count=EXCLUDED.index_count,
                    -- The target row was written by this run's authoritative
                    -- issuer discovery/download.  Preserve that flag instead
                    -- of importing the older published generation's value.
                    is_latest=generation_documents.is_latest,
                    materialized_from_generation_id=EXCLUDED.materialized_from_generation_id,
                    updated_at=now()
                WHERE generation_documents.pdf_sha256=EXCLUDED.pdf_sha256
                  AND generation_documents.raw_object_key=EXCLUDED.raw_object_key
                  AND generation_documents.source_snapshot_id=%s
                RETURNING document_id
                """,
                (
                    claim.generation_id,
                    str(claim.payload.get("source_snapshot_id") or "unknown"),
                    source_generation_id,
                    source_generation_id,
                    claim.document_id,
                    str(claim.payload.get("source_snapshot_id") or "unknown"),
                ),
            )
            if cursor.fetchone() is None:
                connection.rollback()
                raise PermanentStageError("downloaded PDF or fresh snapshot differs from reusable source")
            cursor.execute(
                """
                INSERT INTO generation_artifacts(
                    generation_id, manifest_id, artifact_id, document_id, artifact_type,
                    content_sha256, size_bytes, media_type, manifest_object_key, manifest, created_at
                )
                SELECT %s, x.manifest_id, x.artifact_id, x.document_id, x.artifact_type,
                       x.content_sha256, x.size_bytes, x.media_type, x.manifest_object_key,
                       x.manifest, x.created_at
                FROM generation_artifacts x
                WHERE x.generation_id=%s AND x.document_id=%s
                ON CONFLICT (generation_id, manifest_id) DO NOTHING
                """,
                (claim.generation_id, source_generation_id, claim.document_id),
            )
            cursor.execute(
                """
                WITH copied AS (
                    INSERT INTO evidence(
                        generation_id, evidence_id, document_id, issuer, product_code, product_name,
                        document_type, effective_date, source_version, section_type, page_start,
                        page_end, span_start, span_end, source_spans, text, text_sha256,
                        confidence, is_latest, embedding
                    )
                    SELECT %s, e.evidence_id, e.document_id, e.issuer, e.product_code, e.product_name,
                           e.document_type, e.effective_date, e.source_version, e.section_type,
                           e.page_start, e.page_end, e.span_start, e.span_end, e.source_spans,
                           e.text, e.text_sha256,
                           e.confidence, target.is_latest,
                           e.embedding
                    FROM evidence e
                    JOIN generation_documents target
                      ON target.generation_id=%s AND target.document_id=e.document_id
                    WHERE e.generation_id=%s AND e.document_id=%s
                    ON CONFLICT (generation_id, evidence_id) DO NOTHING
                    RETURNING evidence_id
                ) SELECT count(*)::int AS copied FROM copied
                """,
                (
                    claim.generation_id,
                    claim.generation_id,
                    source_generation_id,
                    claim.document_id,
                ),
            )
            row = cursor.fetchone()
            copied = int(row["copied"]) if row is not None else 0
            # create_run normally pre-materializes unchanged rows. Treat an
            # already complete snapshot as idempotent success, not an error.
            cursor.execute(
                """
                SELECT d.*, (
                    SELECT count(*)::int FROM evidence e
                    WHERE e.generation_id=d.generation_id AND e.document_id=d.document_id
                      AND e.embedding IS NOT NULL
                ) AS actual_index_count
                FROM generation_documents d
                WHERE d.generation_id=%s AND d.document_id=%s
                """,
                (claim.generation_id, claim.document_id),
            )
            document_row = cursor.fetchone()
            if copied == 0 and (
                document_row is None
                or document_row["embedding_count"] is None
                or int(document_row["actual_index_count"]) != int(document_row["index_count"])
            ):
                connection.rollback()
                raise PermanentStageError("no compatible published evidence to materialize")
            connection.commit()

    def _record_artifact(
        self,
        cursor: Any,
        generation_id: str,
        document_id: str | None,
        manifest: ArtifactManifest,
    ) -> None:
        manifest_object = self.objects.put_bytes(manifest.canonical_bytes())
        cursor.execute(
            """
            INSERT INTO generation_artifacts(
                generation_id, manifest_id, artifact_id, document_id, artifact_type,
                content_sha256, size_bytes, media_type, manifest_object_key, manifest, created_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)
            ON CONFLICT (generation_id, manifest_id) DO NOTHING
            """,
            (
                generation_id,
                manifest.manifest_id,
                manifest.artifact_id,
                document_id,
                manifest.artifact_type.value,
                manifest.content_sha256,
                manifest.size_bytes,
                manifest.media_type,
                manifest_object.relative_path.as_posix(),
                manifest.canonical_bytes().decode(),
                manifest.created_at,
            ),
        )

    @staticmethod
    def _assert_current(claim: ClaimedJob, cursor: Any) -> None:
        cursor.execute(
            """
            SELECT 1 FROM jobs
            WHERE id=%s AND state='running' AND lease_owner=%s
              AND fencing_token=%s AND lease_until > now() AND cancel_requested=false
            FOR UPDATE
            """,
            (claim.id, claim.lease_owner, claim.fencing_token),
        )
        if cursor.fetchone() is None:
            raise LostLeaseError("job lease was lost before a stage side effect")


class WorkerLoop:
    def __init__(
        self,
        jobs: JobRepository,
        pipeline: OfflinePipeline,
        *,
        worker_id: str,
        lease_seconds: int,
        poll_seconds: float = 1.0,
        observability: Observability | None = None,
        rollups: PostgresMetricRollupWriter | None = None,
        maintenance: WorkerMaintenance | None = None,
    ) -> None:
        self.jobs = jobs
        self.pipeline = pipeline
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.poll_seconds = poll_seconds
        self.stopping = False
        self.observability = observability or get_observability(
            service="worker",
            environment=getattr(pipeline.settings, "environment", "unknown"),
        )
        # Page callbacks and the worker lifecycle must update the same bounded
        # registry, including in tests that inject an isolated collector.
        self.pipeline.observability = self.observability
        self.rollups = rollups
        self.maintenance = maintenance
        self.logger = logging.getLogger("cardrag.worker")

    async def run(self, *, once: bool = False) -> None:
        while not self.stopping:
            if self.maintenance is not None:
                try:
                    self.maintenance.tick()
                except Exception as exc:
                    log_event(
                        self.logger,
                        "worker.maintenance.failed",
                        level=logging.ERROR,
                        error_code=type(exc).__name__,
                        outcome="error",
                        worker_id=self.worker_id,
                    )
            reclaimed = self.jobs.reclaim_expired()
            self.observability.metrics.lease_reclaimed(reclaimed)
            if reclaimed:
                log_event(
                    self.logger,
                    "worker.lease.reclaimed",
                    outcome="lease_reclaimed",
                    worker_id=self.worker_id,
                )
            claim = self.jobs.claim(worker_id=self.worker_id, lease_seconds=self.lease_seconds)
            if claim is None:
                if once:
                    return
                await asyncio.sleep(self.poll_seconds)
                continue
            run_id = str(claim.payload.get("run_id")) if claim.payload.get("run_id") else None
            generation_id = claim.generation_id or (
                str(claim.payload.get("generation_id")) if claim.payload.get("generation_id") else None
            )
            with bind_context(run_id=run_id, job_id=str(claim.id), generation_id=generation_id):
                await self._run_claim(claim)
            if once:
                return

    async def _run_claim(self, claim: ClaimedJob) -> None:
        started = time.perf_counter()
        outcome = "cancelled"
        retryable = False
        error_code: str | None = None
        self.observability.metrics.job_started(issuer=claim.issuer, stage=claim.stage)
        log_event(
            self.logger,
            "worker.job.started",
            worker_id=self.worker_id,
            issuer=claim.issuer,
            stage=claim.stage,
            attempt=claim.attempt_no,
            fencing_token=claim.fencing_token,
            document_id_hash=hash_identifier(claim.document_id),
            outcome="started",
        )
        heartbeat = asyncio.create_task(self._heartbeat(claim))
        work = asyncio.create_task(self.pipeline.handle(claim))
        try:
            done, _ = await asyncio.wait({work, heartbeat}, return_when=asyncio.FIRST_COMPLETED)
            if heartbeat in done:
                error = heartbeat.exception()
                if error is not None:
                    work.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await work
                    raise error
            await work
        except asyncio.CancelledError:
            self.stopping = True
            raise
        except LostLeaseError as exc:
            # The new owner is authoritative; the stale worker must not mutate
            # job state with its obsolete fencing token.
            error_code = type(exc).__name__
            outcome = "lost_lease"
        except PermanentStageError as exc:
            error_code = type(exc).__name__
            outcome = self._fail_claim(
                claim,
                error_code=error_code,
                retryable=False,
            )
            if outcome == "lost_lease":
                error_code = "LostLeaseError"
        except PERMANENT_PIPELINE_ERRORS as exc:
            error_code = type(exc).__name__
            outcome = self._fail_claim(
                claim,
                error_code=error_code,
                retryable=False,
            )
            if outcome == "lost_lease":
                error_code = "LostLeaseError"
        except httpx.HTTPError as exc:
            error_code = type(exc).__name__
            retryable, base_delay, minimum_delay = http_retry_policy(exc, Issuer(claim.issuer))
            outcome = self._fail_claim(
                claim,
                error_code=error_code,
                retryable=retryable,
                base_delay_seconds=base_delay,
                minimum_delay_seconds=minimum_delay,
            )
            if outcome == "lost_lease":
                error_code = "LostLeaseError"
        except Exception as exc:
            error_code = type(exc).__name__
            retryable = True
            outcome = self._fail_claim(
                claim,
                error_code=error_code,
                retryable=True,
            )
            if outcome == "lost_lease":
                error_code = "LostLeaseError"
        else:
            provider, model, config_hash = attempt_provenance(claim, self.pipeline)
            try:
                self.jobs.finish(
                    claim,
                    worker_id=self.worker_id,
                    provider=provider,
                    model=model,
                    config_hash=config_hash,
                )
                outcome = "succeeded"
            except LostLeaseError:
                error_code = "LostLeaseError"
                outcome = "lost_lease"
        finally:
            heartbeat.cancel()
            # A heartbeat failure is consumed above and translated into the
            # durable ``lost_lease`` outcome.  Awaiting the already-failed task
            # again here must not leak the same exception out of the worker
            # loop after the in-flight stage has been cancelled safely.
            with contextlib.suppress(asyncio.CancelledError, LostLeaseError):
                await heartbeat
            duration = time.perf_counter() - started
            self.observability.metrics.job_finished(
                issuer=claim.issuer,
                stage=claim.stage,
                outcome=outcome,
                duration=duration,
            )
            self._record_rollup(claim, outcome=outcome, duration=duration)
            log_event(
                self.logger,
                "worker.job.completed",
                level=logging.INFO if outcome == "succeeded" else logging.WARNING,
                worker_id=self.worker_id,
                issuer=claim.issuer,
                stage=claim.stage,
                attempt=claim.attempt_no,
                fencing_token=claim.fencing_token,
                document_id_hash=hash_identifier(claim.document_id),
                duration_seconds=round(duration, 6),
                error_code=error_code,
                retryable=retryable and outcome == "retry_wait",
                dead_letter=outcome == "dead_letter",
                outcome=outcome,
            )

    def _fail_claim(
        self,
        claim: ClaimedJob,
        *,
        error_code: str,
        retryable: bool,
        base_delay_seconds: float = 2.0,
        minimum_delay_seconds: float = 0.0,
    ) -> str:
        try:
            return self.jobs.fail(
                claim,
                worker_id=self.worker_id,
                error_code=error_code,
                retryable=retryable,
                base_delay_seconds=base_delay_seconds,
                minimum_delay_seconds=minimum_delay_seconds,
            ).value
        except LostLeaseError:
            # A concurrent cancellation/reclaim won the fencing race.  Its
            # durable transition is authoritative; keep the long-lived worker
            # alive and do not attempt another stale write.
            return "lost_lease"

    def _record_rollup(self, claim: ClaimedJob, *, outcome: str, duration: float) -> None:
        if self.rollups is None:
            return
        try:
            self.rollups.record_job(
                issuer=claim.issuer,
                stage=claim.stage,
                outcome=outcome,
                duration=duration,
            )
        except Exception as exc:
            # Telemetry loss must not cause a completed durable job to retry.
            log_event(
                self.logger,
                "worker.metric_rollup.failed",
                level=logging.ERROR,
                error_code=type(exc).__name__,
                outcome="error",
                worker_id=self.worker_id,
                issuer=claim.issuer,
                stage=claim.stage,
            )

    async def _heartbeat(self, claim: ClaimedJob) -> None:
        interval = max(5.0, self.lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            self.jobs.heartbeat(claim, worker_id=self.worker_id, lease_seconds=self.lease_seconds)
