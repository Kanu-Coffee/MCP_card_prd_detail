"""Production read-only repository backed by PostgreSQL and sealed generation files."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from datetime import date
from functools import partial
from pathlib import Path
from typing import Any

import anyio

from cardrag.db import Postgres
from cardrag.domain import Issuer as DomainIssuer
from cardrag.generation import GenerationStore
from cardrag.observability import record_mcp_rollup
from cardrag.search import HybridSearchEngine, SearchFilters
from cardrag.service.models import (
    AuditEvent,
    Evidence,
    EvidencePage,
    ExactSourceSpan,
    ProductVersion,
    ProductVersions,
    ReadinessStatus,
    SearchPage,
    SearchRequest,
    SearchWarning,
    SourcePage,
    SourcePdf,
    SourceSpan,
)

logger = logging.getLogger(__name__)


def _cursor_encode(generation_id: str, offset: int, *, binding: str) -> str:
    body = json.dumps(
        {"b": binding, "g": generation_id, "o": offset},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(body).decode().rstrip("=")


def _cursor_decode(value: str | None, generation_id: str, *, binding: str) -> int:
    cursor_generation, offset = _cursor_decode_unbound(value, binding=binding)
    if cursor_generation is None:
        return offset
    if cursor_generation != generation_id:
        raise ValueError("invalid or stale cursor")
    return offset


def _cursor_decode_unbound(value: str | None, *, binding: str) -> tuple[str | None, int]:
    if value is None:
        return None, 0
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        payload = json.loads(raw)
        if not isinstance(payload, dict) or set(payload) != {"b", "g", "o"}:
            raise ValueError
        if payload["b"] != binding:
            raise ValueError
        generation_id = payload["g"]
        if not isinstance(generation_id, str) or not generation_id:
            raise ValueError
        offset = payload["o"]
        if isinstance(offset, bool) or not isinstance(offset, int):
            raise ValueError
        if offset < 0 or offset > 1_000_000:
            raise ValueError
        return generation_id, offset
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise ValueError("invalid or stale cursor") from exc


def _search_cursor_binding(request: SearchRequest) -> str:
    payload = request.model_dump(
        mode="json",
        exclude={"cursor", "limit"},
    )
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()


class PostgresCardRAGRepository:
    def __init__(
        self,
        database: Postgres,
        generations: GenerationStore,
        search_engine: HybridSearchEngine,
        storage_root: Path,
    ) -> None:
        self.database = database
        self.generations = generations
        self.search_engine = search_engine
        self.storage_root = storage_root.resolve()

    def _active_generation_id(self) -> str:
        validator = getattr(self.search_engine.store, "validate_active_generation_sync", None)
        if validator is None:
            raise RuntimeError("active generation validator is unavailable")
        return str(validator().generation_id)

    async def search_evidence(self, request: SearchRequest) -> SearchPage:
        filters = SearchFilters(
            issuer=DomainIssuer(request.issuer) if request.issuer else None,
            product_code=request.product_code,
            section_type=request.section_type,
            version=request.version,
            as_of=request.as_of,
        )
        cursor_binding = _search_cursor_binding(request)
        expected_generation_id, offset = _cursor_decode_unbound(
            request.cursor,
            binding=cursor_binding,
        )
        result = await self.search_engine.search(
            request.query,
            filters=filters,
            limit=request.limit,
            offset=offset,
            expected_generation_id=expected_generation_id,
            allow_degraded=request.allow_degraded,
        )
        items = [self._evidence_from_hit(hit.model_dump(mode="python")) for hit in result.hits]
        next_cursor = (
            _cursor_encode(
                result.generation_id,
                offset + len(items),
                binding=cursor_binding,
            )
            if result.has_more
            else None
        )
        no_evidence = not items
        low_confidence = result.low_confidence
        conflicting_versions = result.conflicting_versions
        warning_items: list[SearchWarning] = []
        if no_evidence:
            warning_items.append("no_evidence")
        if low_confidence:
            warning_items.append("low_confidence")
        if conflicting_versions:
            warning_items.append("conflicting_versions")
        if result.degraded:
            warning_items.append("vector_degraded")
        warnings = tuple(warning_items)
        return SearchPage(
            generation_id=result.generation_id,
            items=items,
            next_cursor=next_cursor,
            retrieval_mode=result.retrieval_mode,  # type: ignore[arg-type]
            degraded=result.degraded,
            failed_branch=result.failed_branch,  # type: ignore[arg-type]
            no_evidence=no_evidence,
            low_confidence=low_confidence,
            conflicting_versions=conflicting_versions,
            warnings=warnings,
        )

    @staticmethod
    def _evidence_from_hit(row: dict[str, Any]) -> Evidence:
        return Evidence(
            evidence_id=row["evidence_id"],
            issuer=row["issuer"].value if isinstance(row["issuer"], DomainIssuer) else row["issuer"],
            product_code=row["product_code"],
            document_id=row["document_id"],
            document_version=row["source_version"],
            effective_date=row["effective_date"],
            generation_id=row["generation_id"],
            section_type=row["section_type"],
            title=row.get("product_name"),
            text=row["text"],
            source_span=SourceSpan(
                page_start=row["page_start"],
                page_end=row["page_end"],
                char_start=row["span_start"],
                char_end=row["span_end"],
            ),
            source_spans=tuple(ExactSourceSpan.model_validate(span) for span in row["source_spans"]),
            text_sha256=row["text_sha256"],
            pdf_sha256=row["pdf_sha256"],
            confidence=row["confidence"],
            score=row.get("score"),
        )

    async def get_evidence(
        self,
        evidence_id: str,
        *,
        cursor: str | None,
        limit: int,
    ) -> EvidencePage | None:
        return await anyio.to_thread.run_sync(self._get_evidence, evidence_id, cursor, limit)

    def _get_evidence(self, evidence_id: str, cursor: str | None, limit: int) -> EvidencePage | None:
        generation_id = self._active_generation_id()
        offset = _cursor_decode(cursor, generation_id, binding=evidence_id)
        with self.database.connection() as connection, connection.cursor() as cursor_db:
            cursor_db.execute(
                """
                WITH anchor_row AS (
                    SELECT generation_id, document_id, page_start, span_start, evidence_id
                    FROM evidence
                    WHERE generation_id = %s AND evidence_id = %s
                ), ordered AS (
                    SELECT e.*,
                           row_number() OVER (
                               ORDER BY e.page_start, e.span_start, e.evidence_id
                           ) - 1 AS anchor_offset
                    FROM evidence e
                    JOIN anchor_row a
                      ON a.generation_id=e.generation_id AND a.document_id=e.document_id
                    WHERE (e.page_start, e.span_start, e.evidence_id)
                          >= (a.page_start, a.span_start, a.evidence_id)
                )
                SELECT e.generation_id, e.evidence_id, e.document_id,
                       d.issuer, d.product_code, d.product_name, d.document_type,
                       d.effective_date, d.source_version,
                       e.section_type, e.page_start, e.page_end,
                       e.span_start, e.span_end, e.source_spans, e.text, e.text_sha256,
                       d.pdf_sha256, e.confidence
                FROM ordered e
                JOIN generation_documents d
                  ON d.generation_id=e.generation_id AND d.document_id=e.document_id
                ORDER BY e.page_start, e.span_start, e.evidence_id
                OFFSET %s LIMIT %s
                """,
                (generation_id, evidence_id, offset, limit + 1),
            )
            rows = list(cursor_db.fetchall())
        if not rows:
            return None
        page_rows = rows[:limit]
        items = [self._evidence_from_hit({**dict(row), "score": None}) for row in page_rows]
        next_value = (
            _cursor_encode(generation_id, offset + limit, binding=evidence_id) if len(rows) > limit else None
        )
        return EvidencePage(
            generation_id=generation_id,
            evidence_id=evidence_id,
            document_id=str(page_rows[0]["document_id"]),
            items=items,
            next_cursor=next_value,
        )

    async def get_product_versions(
        self,
        issuer: str,
        product_code: str,
        *,
        as_of: date | None,
    ) -> ProductVersions:
        return await anyio.to_thread.run_sync(self._versions, issuer, product_code, as_of)

    def _versions(self, issuer: str, product_code: str, as_of: date | None) -> ProductVersions:
        generation_id = self._active_generation_id()
        with self.database.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT d.*
                FROM generation_documents d
                WHERE d.generation_id = %s AND d.issuer = %s AND d.product_code = %s
                  AND (%s::date IS NULL OR d.effective_date <= %s::date)
                ORDER BY d.effective_date DESC, d.version_sort_key DESC, d.document_id
                """,
                (generation_id, issuer, product_code, as_of, as_of),
            )
            rows = list(cursor.fetchall())
        return ProductVersions(
            generation_id=generation_id,
            issuer=issuer,  # type: ignore[arg-type]
            product_code=product_code,
            items=[
                ProductVersion(
                    issuer=row["issuer"],
                    product_code=row["product_code"],
                    document_id=row["document_id"],
                    version=row["source_version"],
                    effective_date=row["effective_date"],
                    discovered_at=row["discovered_at"],
                    source_sha256=row["pdf_sha256"],
                    is_latest=row["is_latest"],
                )
                for row in rows
            ],
        )

    async def get_source_pdf(self, document_id: str) -> SourcePdf | None:
        return await anyio.to_thread.run_sync(self._source_pdf, document_id)

    def _source_pdf(self, document_id: str) -> SourcePdf | None:
        generation_id = self._active_generation_id()
        with self.database.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT d.* FROM generation_documents d
                WHERE d.generation_id = %s AND d.document_id = %s
                  AND d.pdf_sha256 IS NOT NULL AND d.raw_object_key IS NOT NULL
                """,
                (generation_id, document_id),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        # Keep the catalog-supplied lexical path intact until SourceFileService
        # performs its containment and symlink checks. Resolving it here would
        # erase evidence of a child symlink and make that later guard
        # ineffective.
        path = self.storage_root / row["raw_object_key"]
        return SourcePdf(
            document_id=row["document_id"],
            issuer=row["issuer"],
            product_code=row["product_code"],
            version=row["source_version"],
            path=path,
            sha256=row["pdf_sha256"],
            size_bytes=row["pdf_size_bytes"],
        )

    async def get_source_page(self, document_id: str, page: int) -> SourcePage | None:
        return await anyio.to_thread.run_sync(self._source_page, document_id, page)

    def _source_page(self, document_id: str, page: int) -> SourcePage | None:
        generation_id = self._active_generation_id()
        with self.database.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT d.*, d.ocr_pages->>(%s - 1) AS page_text
                FROM generation_documents d
                WHERE d.generation_id = %s AND d.document_id = %s
                  AND d.pdf_sha256 IS NOT NULL AND d.ocr_pages IS NOT NULL
                """,
                (page, generation_id, document_id),
            )
            row = cursor.fetchone()
        if row is None or row["page_text"] is None:
            return None
        return SourcePage(
            document_id=row["document_id"],
            issuer=row["issuer"],
            product_code=row["product_code"],
            version=row["source_version"],
            page=page,
            page_count=row["pdf_page_count"],
            ocr_text=row["page_text"],
            ocr_sha256=row["ocr_sha256"],
            pdf_sha256=row["pdf_sha256"],
        )

    async def readiness(self) -> ReadinessStatus:
        return await anyio.to_thread.run_sync(self._readiness)

    def _readiness(self) -> ReadinessStatus:
        checks = {
            "database": False,
            "generation": False,
            "schema": False,
            "model": False,
            "coverage": False,
            "lexical": False,
            "vector": False,
        }
        generation_id: str | None = None
        try:
            validator = getattr(self.search_engine.store, "validate_active_generation_sync", None)
            if validator is None:
                raise RuntimeError("active generation validator is unavailable")
            snapshot = validator()
            generation_id = snapshot.generation_id
            checks["generation"] = True
            checks["model"] = True
            with self.database.connection() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT to_regclass('public.evidence') IS NOT NULL
                       AND to_regclass('public.generation_documents') IS NOT NULL
                       AND to_regclass('public.active_generation') IS NOT NULL AS ok
                    """
                )
                schema_row = cursor.fetchone()
                checks["schema"] = bool(schema_row is not None and schema_row["ok"])
                cursor.execute(
                    """
                    SELECT count(*)::int AS documents,
                           count(*) FILTER (WHERE is_latest)::int AS latest_documents
                    FROM generation_documents WHERE generation_id = %s
                    """,
                    (generation_id,),
                )
                document_row = cursor.fetchone()
                document_count = int(document_row["documents"]) if document_row is not None else 0
                latest_documents = int(document_row["latest_documents"]) if document_row is not None else 0
                cursor.execute(
                    """
                    SELECT count(*)::int AS n,
                           count(DISTINCT e.document_id) FILTER (WHERE d.is_latest)::int AS latest_documents
                    FROM evidence e
                    JOIN generation_documents d
                      ON d.generation_id=e.generation_id AND d.document_id=e.document_id
                    WHERE e.generation_id = %s
                    """,
                    (generation_id,),
                )
                count_row = cursor.fetchone()
                count = int(count_row["n"]) if count_row is not None else 0
                checks["database"] = True
                latest_indexed = int(count_row["latest_documents"]) if count_row is not None else 0
                checks["coverage"] = (
                    document_count == snapshot.manifest.document_count
                    and latest_documents == snapshot.manifest.latest_document_count
                    and latest_indexed == snapshot.manifest.latest_index_count
                )
                cursor.execute(
                    "SELECT count(*)::int AS n FROM evidence WHERE generation_id = %s AND embedding IS NOT NULL",
                    (generation_id,),
                )
                vector_row = cursor.fetchone()
                vector_count = int(vector_row["n"]) if vector_row is not None else 0
                cursor.execute(
                    """
                    SELECT c.relname, i.indisvalid, i.indisready
                    FROM pg_class c JOIN pg_index i ON i.indexrelid=c.oid
                    WHERE c.relname IN ('evidence_fts_idx', 'evidence_vector_idx')
                    """
                )
                indexes = {
                    row["relname"]: bool(row["indisvalid"] and row["indisready"]) for row in cursor.fetchall()
                }
                checks["lexical"] = count > 0 and indexes.get("evidence_fts_idx", False)
                checks["vector"] = (
                    vector_count == count and count > 0 and indexes.get("evidence_vector_idx", False)
                )
        except Exception:
            logger.warning("readiness check failed", exc_info=False)
        return ReadinessStatus(ready=all(checks.values()), generation_id=generation_id, checks=checks)

    async def record_audit(self, event: AuditEvent) -> None:
        await anyio.to_thread.run_sync(self._audit, event)

    async def record_mcp_metric(
        self,
        *,
        operation: str,
        outcome: str,
        duration: float,
    ) -> None:
        """Persist only the anonymous operation/outcome hourly aggregate."""

        await anyio.to_thread.run_sync(
            partial(
                record_mcp_rollup,
                self.database,
                operation=operation,
                outcome=outcome,
                duration=duration,
            )
        )

    def _audit(self, event: AuditEvent) -> None:
        with self.database.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO audit_events(event_type, request_id, subject_hash, client_id, scopes,
                                         document_id, outcome, metadata, occurred_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                """,
                (
                    event.action,
                    event.request_id,
                    event.subject_hash,
                    event.client_id,
                    list(event.granted_scopes),
                    event.document_id,
                    event.outcome,
                    json.dumps(
                        {
                            "page": event.page,
                            "range": event.requested_range,
                            "source_sha256": event.source_sha256,
                        },
                        separators=(",", ":"),
                    ),
                    event.occurred_at,
                ),
            )
            connection.commit()

    def prune_retention(self) -> tuple[int, int]:
        with self.database.connection() as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM audit_events WHERE occurred_at < now() - interval '90 days'")
            audits = cursor.rowcount
            cursor.execute("DELETE FROM metric_rollups WHERE bucket_start < now() - interval '1 year'")
            metrics = cursor.rowcount
            connection.commit()
            return audits, metrics
