from __future__ import annotations

import asyncio
import hashlib
import os
import threading
import time
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from psycopg.errors import QueryCanceled
from pydantic import ValidationError

from cardrag.domain import Issuer as DomainIssuer
from cardrag.search.hybrid import SearchHit, SearchResponse
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
    SourcePage,
    SourcePdf,
    SourceSpan,
)
from cardrag.service.postgres_repository import PostgresCardRAGRepository
from cardrag.service.query import QueryService, ServiceTimeoutError, ServiceUnavailableError
from cardrag.service.source_files import BoundedFileResponse, InvalidSourceError, SourceFileService
from tests.support_pdf import write_synthetic_pdf

SHA = "a" * 64
TEXT_SHA = hashlib.sha256(b"Evidence text").hexdigest()


def _evidence() -> Evidence:
    return Evidence(
        evidence_id="ev-1",
        issuer="woori",
        product_code="card-1",
        document_id="doc-1",
        document_version="2026-01",
        effective_date=date(2026, 1, 1),
        generation_id="gen-1",
        section_type="benefit",
        title="Benefit",
        text="Evidence text",
        source_span=SourceSpan(page_start=1, page_end=1, char_start=0, char_end=13),
        source_spans=(ExactSourceSpan(page=1, start=0, end=13, quote_sha256=TEXT_SHA),),
        text_sha256=TEXT_SHA,
        pdf_sha256="b" * 64,
        confidence=1.0,
        score=0.9,
    )


class FakeRepository:
    def __init__(self) -> None:
        self.search_result = SearchPage(generation_id="gen-1", items=[_evidence()])
        self.source_pdf: SourcePdf | None = None
        self.source_page: SourcePage | None = None
        self.audits: list[AuditEvent] = []
        self.fail_search = False

    async def search_evidence(self, request: SearchRequest) -> SearchPage:
        if self.fail_search:
            raise RuntimeError(f"backend leaked query: {request.query}")
        return self.search_result

    async def get_evidence(
        self,
        evidence_id: str,
        *,
        cursor: str | None,
        limit: int,
    ) -> EvidencePage | None:
        if evidence_id != "ev-1":
            return None
        return EvidencePage(
            generation_id="gen-1",
            evidence_id=evidence_id,
            document_id="doc-1",
            items=[_evidence()],
        )

    async def get_product_versions(
        self,
        issuer: str,
        product_code: str,
        *,
        as_of: date | None,
    ) -> ProductVersions:
        return ProductVersions(
            generation_id="gen-1",
            issuer=issuer,
            product_code=product_code,
            items=[
                ProductVersion(
                    issuer=issuer,
                    product_code=product_code,
                    document_id="doc-1",
                    version="2026-01",
                    effective_date=date(2026, 1, 1),
                    discovered_at=datetime(2026, 1, 2, tzinfo=UTC),
                    source_sha256=SHA,
                    is_latest=True,
                )
            ],
        )

    async def get_source_pdf(self, document_id: str) -> SourcePdf | None:
        if self.source_pdf is None or document_id != self.source_pdf.document_id:
            return None
        return self.source_pdf

    async def get_source_page(self, document_id: str, page: int) -> SourcePage | None:
        if self.source_page is None:
            return None
        if document_id != self.source_page.document_id or page != self.source_page.page:
            return None
        return self.source_page

    async def readiness(self) -> ReadinessStatus:
        return ReadinessStatus(
            ready=True,
            generation_id="gen-1",
            checks={"repository": True, "generation": True, "indexes": True},
        )

    async def record_audit(self, event: AuditEvent) -> None:
        self.audits.append(event)


def _query_service(repository: FakeRepository) -> QueryService:
    return QueryService(
        repository,
        max_concurrent_requests=5,
        request_timeout_seconds=2,
    )


def test_public_evidence_rejects_a_text_hash_that_does_not_match_the_payload() -> None:
    payload = _evidence().model_dump(mode="python")
    payload["text_sha256"] = "0" * 64
    with pytest.raises(ValidationError, match="text hash"):
        Evidence.model_validate(payload)


class _PagingEngine:
    def __init__(self, hits: list[SearchHit]) -> None:
        self.hits = hits
        self.generation_id = "gen-page"
        self.calls: list[tuple[int, int, str | None]] = []

    async def search(
        self,
        _: str,
        *,
        filters: object,
        limit: int,
        offset: int,
        expected_generation_id: str | None,
        allow_degraded: bool,
    ) -> SearchResponse:
        del filters, allow_degraded
        self.calls.append((offset, limit, expected_generation_id))
        if expected_generation_id is not None and expected_generation_id != self.generation_id:
            raise ValueError("search cursor belongs to a stale generation")
        page = self.hits[offset : offset + limit]
        return SearchResponse(
            generation_id=self.generation_id,
            hits=tuple(page),
            retrieval_mode="hybrid",
            degraded=False,
            has_more=len(self.hits) > offset + limit,
            low_confidence=any(hit.confidence < 0.7 for hit in self.hits),
            conflicting_versions=len({(hit.document_id, hit.source_version) for hit in self.hits}) > 1,
        )


def _search_hit(index: int, *, confidence: float = 1.0, version: str = "v1") -> SearchHit:
    text = f"근거 {index}"
    return SearchHit(
        generation_id="gen-page",
        evidence_id=f"evidence-{index}",
        issuer=DomainIssuer.WOORI,
        product_code="card-1",
        product_name="카드",
        document_id=f"document-{version}",
        document_type="product_description",
        effective_date=date(2026, 1, index + 1),
        source_version=version,
        section_type="benefit",
        page_start=1,
        page_end=1,
        span_start=index,
        span_end=index + 1,
        source_spans=(
            {
                "page": 1,
                "start": index,
                "end": index + 1,
                "quote_sha256": hashlib.sha256(text.encode()).hexdigest(),
            },
        ),
        text=text,
        text_sha256=hashlib.sha256(text.encode()).hexdigest(),
        pdf_sha256="f" * 64,
        confidence=confidence,
        score=1.0 / (index + 1),
    )


@pytest.mark.asyncio
async def test_postgres_repository_cursor_returns_later_results_and_rejects_stale_generation(
    tmp_path: Path,
) -> None:
    engine = _PagingEngine([_search_hit(index) for index in range(5)])
    repository = PostgresCardRAGRepository(
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        engine,  # type: ignore[arg-type]
        tmp_path,
    )

    first = await repository.search_evidence(SearchRequest(query="혜택", limit=2))
    second = await repository.search_evidence(SearchRequest(query="혜택", limit=2, cursor=first.next_cursor))

    assert [item.evidence_id for item in first.items] == ["evidence-0", "evidence-1"]
    assert [item.evidence_id for item in second.items] == ["evidence-2", "evidence-3"]
    assert second.next_cursor is not None
    assert engine.calls == [(0, 2, None), (2, 2, "gen-page")]
    assert "혜택" not in second.next_cursor

    with pytest.raises(ValueError, match="invalid or stale cursor"):
        await repository.search_evidence(SearchRequest(query="다른 질의", limit=2, cursor=second.next_cursor))

    engine.generation_id = "gen-new"
    with pytest.raises(ValueError, match="stale generation"):
        await repository.search_evidence(SearchRequest(query="혜택", limit=2, cursor=second.next_cursor))


@pytest.mark.asyncio
async def test_search_page_exposes_low_confidence_conflicting_versions_and_no_evidence(
    tmp_path: Path,
) -> None:
    engine = _PagingEngine(
        [
            _search_hit(0, confidence=0.4, version="v1"),
            _search_hit(1, confidence=0.6, version="v2"),
        ]
    )
    repository = PostgresCardRAGRepository(
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        engine,  # type: ignore[arg-type]
        tmp_path,
    )

    result = await repository.search_evidence(SearchRequest(query="조건", limit=10))
    assert result.low_confidence is True
    assert result.conflicting_versions is True
    assert result.warnings == ("low_confidence", "conflicting_versions")
    assert result.items[0].text_sha256 != result.items[0].pdf_sha256
    assert result.items[0].confidence == 0.4

    engine.hits = [
        _search_hit(0, confidence=1.0),
        _search_hit(1, confidence=0.6),
    ]
    partially_low = await repository.search_evidence(SearchRequest(query="혼합 신뢰도", limit=10))
    assert partially_low.low_confidence is True
    assert partially_low.warnings == ("low_confidence",)

    engine.hits = []
    empty = await repository.search_evidence(SearchRequest(query="없는 근거", limit=10))
    assert empty.no_evidence is True
    assert empty.warnings == ("no_evidence",)


@pytest.mark.asyncio
async def test_search_never_propagates_query_in_backend_error() -> None:
    repository = FakeRepository()
    repository.fail_search = True
    service = _query_service(repository)
    raw_query = "annual fee private phrase"

    with pytest.raises(ServiceUnavailableError) as raised:
        await service.search(SearchRequest(query=raw_query))

    assert raw_query not in str(raised.value)
    assert str(raised.value) == "search is temporarily unavailable"


@pytest.mark.asyncio
async def test_search_timeout_has_stable_non_query_error_classification() -> None:
    class SlowRepository(FakeRepository):
        async def search_evidence(self, request: SearchRequest) -> SearchPage:
            del request
            await asyncio.sleep(1)
            return self.search_result

    service = QueryService(
        SlowRepository(),
        max_concurrent_requests=1,
        request_timeout_seconds=0.01,
    )
    started = time.perf_counter()
    with pytest.raises(ServiceTimeoutError) as raised:
        await service.search(SearchRequest(query="private timeout query"))

    assert time.perf_counter() - started < 0.2
    assert str(raised.value) == "search timed out"
    assert "private timeout query" not in str(raised.value)


@pytest.mark.asyncio
async def test_postgres_statement_cancellation_is_a_stable_service_timeout() -> None:
    class CancelledRepository(FakeRepository):
        async def search_evidence(self, request: SearchRequest) -> SearchPage:
            del request
            raise QueryCanceled("statement timeout contained private SQL")

    service = QueryService(
        CancelledRepository(),
        max_concurrent_requests=1,
        request_timeout_seconds=1,
    )

    with pytest.raises(ServiceTimeoutError) as raised:
        await service.search(SearchRequest(query="private timeout query"))

    assert str(raised.value) == "search timed out"
    assert "private" not in str(raised.value)


@pytest.mark.asyncio
async def test_timed_out_thread_work_retains_capacity_until_sync_completion() -> None:
    service = QueryService(
        FakeRepository(),
        max_concurrent_requests=1,
        request_timeout_seconds=0.01,
    )
    lock = threading.Lock()
    running = 0
    peak = 0

    def slow_sync() -> None:
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        try:
            time.sleep(0.08)
        finally:
            with lock:
                running -= 1

    for _ in range(8):
        with pytest.raises(ServiceTimeoutError, match="sync fixture timed out"):
            await service.run_with_budget(
                lambda: asyncio.to_thread(slow_sync),
                label="sync fixture",
            )

    await asyncio.wait_for(
        asyncio.gather(*tuple(service._budget_tasks), return_exceptions=True),
        timeout=1,
    )
    assert running == 0
    assert peak == 1


@pytest.mark.asyncio
async def test_readiness_uses_the_shared_budget_and_retains_timed_out_sync_capacity() -> None:
    class SlowReadinessRepository(FakeRepository):
        def __init__(self) -> None:
            super().__init__()
            self.lock = threading.Lock()
            self.running = 0
            self.peak = 0

        async def readiness(self) -> ReadinessStatus:
            await asyncio.to_thread(self._slow_readiness)
            return await super().readiness()

        def _slow_readiness(self) -> None:
            with self.lock:
                self.running += 1
                self.peak = max(self.peak, self.running)
            try:
                time.sleep(0.06)
            finally:
                with self.lock:
                    self.running -= 1

    repository = SlowReadinessRepository()
    service = QueryService(
        repository,
        max_concurrent_requests=1,
        request_timeout_seconds=0.01,
    )

    results = await asyncio.gather(*(service.readiness() for _ in range(6)))
    await asyncio.wait_for(
        asyncio.gather(*tuple(service._budget_tasks), return_exceptions=True),
        timeout=1,
    )

    assert all(result.ready is False for result in results)
    assert repository.peak == 1


@pytest.mark.asyncio
async def test_source_audit_uses_bounded_independent_capacity(tmp_path: Path) -> None:
    repository = FakeRepository()
    query_service = QueryService(
        repository,
        max_concurrent_requests=1,
        request_timeout_seconds=0.01,
    )
    sources = SourceFileService(
        query_service,
        storage_root=tmp_path,
        page_cache_root=tmp_path / "cache",
        public_server_url="http://localhost:8000/mcp",
        max_pdf_bytes=100 * 1024 * 1024,
        page_cache_ttl_seconds=604_800,
        render_scale=1,
        subject_namespace="issuer",
    )

    async def slow_audit(event: AuditEvent) -> None:
        del event
        await asyncio.to_thread(time.sleep, 0.08)

    repository.record_audit = slow_audit  # type: ignore[method-assign]
    started = time.perf_counter()
    with pytest.raises(InvalidSourceError, match="audit repository"):
        await sources.audit_attempt(
            request_id="req-audit-timeout",
            action="source_pdf",
            access_token=None,
            source=None,
            document_id="not-found",
            page=None,
            requested_range=None,
            outcome="not_found",
        )

    assert time.perf_counter() - started < 0.2


@pytest.mark.asyncio
async def test_degraded_search_requires_explicit_opt_in() -> None:
    repository = FakeRepository()
    repository.search_result = SearchPage(
        generation_id="gen-1",
        items=[_evidence()],
        retrieval_mode="lexical_only",
        degraded=True,
        failed_branch="vector",
        warnings=("vector_degraded",),
    )
    service = _query_service(repository)

    with pytest.raises(ServiceUnavailableError):
        await service.search(SearchRequest(query="benefit"))
    result = await service.search(SearchRequest(query="benefit", allow_degraded=True))
    assert result.retrieval_mode == "lexical_only"
    assert result.failed_branch == "vector"


def test_search_rejects_blank_query_and_ambiguous_version_selector() -> None:
    with pytest.raises(ValidationError):
        SearchRequest(query="   ")
    with pytest.raises(ValidationError):
        SearchRequest(
            query="benefit",
            version="v2",
            as_of=date(2026, 1, 1),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, -1, 51, 1_000_000])
async def test_evidence_lookup_rejects_unbounded_page_sizes(limit: int) -> None:
    with pytest.raises(ValidationError):
        await _query_service(FakeRepository()).evidence("ev-1", limit=limit)


@pytest.mark.asyncio
async def test_evidence_lookup_rejects_oversized_cursor() -> None:
    with pytest.raises(ValidationError):
        await _query_service(FakeRepository()).evidence("ev-1", cursor="x" * 2_049)


def _make_pdf(path: Path) -> tuple[str, int]:
    write_synthetic_pdf(path, ["CardRAG source page"])
    raw = path.read_bytes()
    return hashlib.sha256(raw).hexdigest(), len(raw)


@pytest.mark.asyncio
async def test_source_service_verifies_pdf_and_renders_seven_day_page_cache(
    tmp_path: Path,
) -> None:
    storage = tmp_path / "storage"
    cache = tmp_path / "cache"
    storage.mkdir()
    pdf_path = storage / "source.pdf"
    digest, size = _make_pdf(pdf_path)
    repository = FakeRepository()
    repository.source_pdf = SourcePdf(
        document_id="woori:card-1:2026-01",
        issuer="woori",
        product_code="card-1",
        version="2026-01",
        path=pdf_path,
        sha256=digest,
        size_bytes=size,
    )
    repository.source_page = SourcePage(
        document_id=repository.source_pdf.document_id,
        issuer="woori",
        product_code="card-1",
        version="2026-01",
        page=1,
        page_count=1,
        ocr_text="CardRAG source page",
        ocr_sha256=hashlib.sha256(b"CardRAG source page").hexdigest(),
        pdf_sha256=digest,
    )
    sources = SourceFileService(
        _query_service(repository),
        storage_root=storage,
        page_cache_root=cache,
        public_server_url="http://localhost:8000/mcp",
        max_pdf_bytes=100 * 1024 * 1024,
        page_cache_ttl_seconds=7 * 24 * 3600,
        render_scale=1,
        subject_namespace="https://id.example/realms/cardrag",
    )

    pdf_descriptor = await sources.pdf_descriptor(repository.source_pdf.document_id)
    page_descriptor = await sources.page_descriptor(
        repository.source_pdf.document_id,
        1,
        include_png=True,
    )

    assert pdf_descriptor.url.endswith("/sources/woori%3Acard-1%3A2026-01/pdf")
    assert pdf_descriptor.sha256 == digest
    assert pdf_descriptor.size_bytes == size
    assert page_descriptor.png_url is not None
    assert page_descriptor.png_cache_ttl_seconds == 604_800
    cached = list(cache.glob("*/*.png"))
    assert len(cached) == 1
    assert cached[0].read_bytes().startswith(b"\x89PNG\r\n\x1a\n")

    expired = time.time() - 604_801
    os.utime(cached[0], (expired, expired))
    assert await sources.cleanup_expired() == 1
    assert not cached[0].exists()


@pytest.mark.asyncio
async def test_source_service_rejects_catalog_path_outside_storage(tmp_path: Path) -> None:
    storage = tmp_path / "storage"
    storage.mkdir()
    outside = tmp_path / "outside.pdf"
    digest, size = _make_pdf(outside)
    repository = FakeRepository()
    repository.source_pdf = SourcePdf(
        document_id="doc-outside",
        issuer="kb",
        product_code="card-2",
        version="v1",
        path=outside,
        sha256=digest,
        size_bytes=size,
    )
    sources = SourceFileService(
        _query_service(repository),
        storage_root=storage,
        page_cache_root=tmp_path / "cache",
        public_server_url="http://localhost:8000/mcp",
        max_pdf_bytes=100 * 1024 * 1024,
        page_cache_ttl_seconds=604_800,
        render_scale=1,
        subject_namespace="issuer",
    )

    with pytest.raises(InvalidSourceError):
        await sources.pdf_descriptor("doc-outside")


@pytest.mark.asyncio
async def test_source_service_rejects_catalog_child_symlink(tmp_path: Path) -> None:
    storage = tmp_path / "storage"
    storage.mkdir()
    real_directory = storage / "real"
    real_directory.mkdir()
    pdf_path = real_directory / "source.pdf"
    digest, size = _make_pdf(pdf_path)
    linked_directory = storage / "linked"
    linked_directory.symlink_to(real_directory, target_is_directory=True)
    repository = FakeRepository()
    repository.source_pdf = SourcePdf(
        document_id="doc-symlink",
        issuer="kb",
        product_code="card-2",
        version="v1",
        path=linked_directory / "source.pdf",
        sha256=digest,
        size_bytes=size,
    )
    sources = SourceFileService(
        _query_service(repository),
        storage_root=storage,
        page_cache_root=tmp_path / "cache",
        public_server_url="http://localhost:8000/mcp",
        max_pdf_bytes=100 * 1024 * 1024,
        page_cache_ttl_seconds=604_800,
        render_scale=1,
        subject_namespace="issuer",
    )

    with pytest.raises(InvalidSourceError):
        await sources.pdf_descriptor("doc-symlink")


@pytest.mark.asyncio
async def test_source_composite_budget_is_reentrant_at_concurrency_one(tmp_path: Path) -> None:
    storage = tmp_path / "storage"
    storage.mkdir()
    pdf_path = storage / "source.pdf"
    digest, size = _make_pdf(pdf_path)
    repository = FakeRepository()
    repository.source_pdf = SourcePdf(
        document_id="doc-reentrant",
        issuer="woori",
        product_code="card-1",
        version="v1",
        path=pdf_path,
        sha256=digest,
        size_bytes=size,
    )
    sources = SourceFileService(
        QueryService(repository, max_concurrent_requests=1, request_timeout_seconds=0.5),
        storage_root=storage,
        page_cache_root=tmp_path / "cache",
        public_server_url="http://localhost:8000/mcp",
        max_pdf_bytes=100 * 1024 * 1024,
        page_cache_ttl_seconds=604_800,
        render_scale=1,
        subject_namespace="issuer",
    )

    descriptor = await asyncio.wait_for(sources.pdf_descriptor("doc-reentrant"), timeout=0.25)

    assert descriptor.sha256 == digest


@pytest.mark.asyncio
async def test_source_verification_obeys_shared_concurrency_and_timeout_budget(
    tmp_path: Path,
) -> None:
    storage = tmp_path / "storage"
    storage.mkdir()
    pdf_path = storage / "source.pdf"
    digest, size = _make_pdf(pdf_path)
    repository = FakeRepository()
    repository.source_pdf = SourcePdf(
        document_id="doc-bounded",
        issuer="woori",
        product_code="card-1",
        version="v1",
        path=pdf_path,
        sha256=digest,
        size_bytes=size,
    )
    query_service = QueryService(
        repository,
        max_concurrent_requests=2,
        request_timeout_seconds=1,
    )
    sources = SourceFileService(
        query_service,
        storage_root=storage,
        page_cache_root=tmp_path / "cache",
        public_server_url="http://localhost:8000/mcp",
        max_pdf_bytes=100 * 1024 * 1024,
        page_cache_ttl_seconds=604_800,
        render_scale=1,
        subject_namespace="issuer",
    )
    lock = threading.Lock()
    concurrent = 0
    peak = 0

    def slow_verification(source: SourcePdf) -> Path:
        nonlocal concurrent, peak
        with lock:
            concurrent += 1
            peak = max(peak, concurrent)
        try:
            time.sleep(0.03)
            return source.path
        finally:
            with lock:
                concurrent -= 1

    sources._verify_pdf_sync = slow_verification  # type: ignore[method-assign]
    await asyncio.gather(*(sources.pdf_descriptor("doc-bounded") for _ in range(6)))
    assert peak == 2

    query_service._timeout = 0.01
    with pytest.raises(ServiceTimeoutError, match="source PDF descriptor timed out"):
        await sources.pdf_descriptor("doc-bounded")


@pytest.mark.asyncio
async def test_page_render_holds_the_shared_query_capacity(tmp_path: Path) -> None:
    storage = tmp_path / "storage"
    storage.mkdir()
    pdf_path = storage / "source.pdf"
    digest, size = _make_pdf(pdf_path)

    class TrackedRepository(FakeRepository):
        def __init__(self) -> None:
            super().__init__()
            self.search_started = asyncio.Event()

        async def search_evidence(self, request: SearchRequest) -> SearchPage:
            del request
            self.search_started.set()
            return self.search_result

    repository = TrackedRepository()
    repository.source_pdf = SourcePdf(
        document_id="doc-render-budget",
        issuer="woori",
        product_code="card-1",
        version="v1",
        path=pdf_path,
        sha256=digest,
        size_bytes=size,
    )
    repository.source_page = SourcePage(
        document_id="doc-render-budget",
        issuer="woori",
        product_code="card-1",
        version="v1",
        page=1,
        page_count=1,
        ocr_text="page",
        ocr_sha256=hashlib.sha256(b"page").hexdigest(),
        pdf_sha256=digest,
    )
    query_service = QueryService(
        repository,
        max_concurrent_requests=1,
        request_timeout_seconds=1,
    )
    sources = SourceFileService(
        query_service,
        storage_root=storage,
        page_cache_root=tmp_path / "cache",
        public_server_url="http://localhost:8000/mcp",
        max_pdf_bytes=100 * 1024 * 1024,
        page_cache_ttl_seconds=604_800,
        render_scale=1,
        subject_namespace="issuer",
    )
    render_started = threading.Event()
    release_render = threading.Event()

    def slow_render(_: Path, __: SourcePage, ___: Path) -> None:
        render_started.set()
        assert release_render.wait(timeout=1)

    sources._render_page_sync = slow_render  # type: ignore[method-assign]
    render_task = asyncio.create_task(sources.page_descriptor("doc-render-budget", 1, include_png=True))
    assert await asyncio.to_thread(render_started.wait, 0.5)
    search_task = asyncio.create_task(query_service.search(SearchRequest(query="queued")))
    await asyncio.sleep(0.02)
    assert not repository.search_started.is_set()

    release_render.set()
    await asyncio.gather(render_task, search_task)
    assert repository.search_started.is_set()


@pytest.mark.asyncio
async def test_file_stream_holds_capacity_for_body_lifetime_and_times_out(tmp_path: Path) -> None:
    repository = FakeRepository()
    query_service = QueryService(
        repository,
        max_concurrent_requests=1,
        request_timeout_seconds=1,
    )
    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"payload")
    response = BoundedFileResponse(
        payload,
        query_service=query_service,
        budget_label="source PDF stream",
    )
    body_started = asyncio.Event()
    release_body = asyncio.Event()

    async def receive() -> dict[str, object]:
        return {"type": "http.disconnect"}

    async def blocked_send(message: dict[str, object]) -> None:
        if message["type"] == "http.response.body":
            body_started.set()
            await release_body.wait()

    scope = {"type": "http", "method": "GET", "headers": []}
    stream_task = asyncio.create_task(response(scope, receive, blocked_send))  # type: ignore[arg-type]
    await asyncio.wait_for(body_started.wait(), timeout=0.5)
    queued = asyncio.create_task(query_service.search(SearchRequest(query="queued")))
    await asyncio.sleep(0.02)
    assert not queued.done()

    release_body.set()
    await asyncio.gather(stream_task, queued)

    query_service._timeout = 0.01

    async def slow_send(message: dict[str, object]) -> None:
        if message["type"] == "http.response.body":
            await asyncio.sleep(1)

    timed_response = BoundedFileResponse(
        payload,
        query_service=query_service,
        budget_label="source PDF stream",
    )
    started = time.perf_counter()
    with pytest.raises(ServiceTimeoutError, match="source PDF stream timed out"):
        await timed_response(scope, receive, slow_send)  # type: ignore[arg-type]
    assert time.perf_counter() - started < 0.2


@pytest.mark.asyncio
async def test_cancelled_file_stream_is_observed_after_body_task_ends(tmp_path: Path) -> None:
    query_service = QueryService(
        FakeRepository(),
        max_concurrent_requests=1,
        request_timeout_seconds=1,
    )
    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"payload")
    body_started = asyncio.Event()
    release_body = asyncio.Event()
    observations: list[tuple[str, float]] = []

    async def completed(outcome: str, duration: float) -> None:
        observations.append((outcome, duration))

    response = BoundedFileResponse(
        payload,
        query_service=query_service,
        budget_label="source PDF stream",
        on_complete=completed,
    )

    async def receive() -> dict[str, object]:
        return {"type": "http.disconnect"}

    async def blocked_send(message: dict[str, object]) -> None:
        if message["type"] == "http.response.body":
            body_started.set()
            await release_body.wait()

    scope = {"type": "http", "method": "GET", "headers": []}
    response_task = asyncio.create_task(response(scope, receive, blocked_send))  # type: ignore[arg-type]
    await asyncio.wait_for(body_started.wait(), timeout=0.5)
    response_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await response_task
    assert observations == []

    release_body.set()
    await asyncio.wait_for(
        asyncio.gather(*tuple(query_service._budget_tasks), return_exceptions=True),
        timeout=0.5,
    )
    assert len(observations) == 1
    assert observations[0][0] == "error"
