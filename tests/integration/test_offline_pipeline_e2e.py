from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.auth.provider import AccessToken, TokenVerifier
from pydantic import SecretStr

from cardrag.acquisition import DownloadedPDF
from cardrag.cli import _finalize_existing_run
from cardrag.config import Settings
from cardrag.db import Postgres
from cardrag.domain import Issuer
from cardrag.generation import GenerationStore
from cardrag.generation_builder import GenerationBuilder
from cardrag.issuers import DiscoveryMode, SourceRecord
from cardrag.issuers.common import canonical_snapshot
from cardrag.issuers.kb import parse_history as parse_kb_history
from cardrag.issuers.kb import parse_listing as parse_kb_listing
from cardrag.issuers.shinhan import parse_history as parse_shinhan_history
from cardrag.issuers.shinhan import parse_listing as parse_shinhan_listing
from cardrag.issuers.woori import parse_detail_records
from cardrag.jobs import JobRepository
from cardrag.mcp_server import build_app
from cardrag.pipeline.ocr import split_pages
from cardrag.pipeline.runtime import OfflinePipeline, WorkerLoop
from cardrag.scheduler import DailyScheduler
from cardrag.search.generation_store import GenerationPinnedPostgresStore
from cardrag.search.hybrid import HybridSearchEngine
from cardrag.service.auth import _make_access_token
from cardrag.service.models import SearchRequest
from cardrag.service.postgres_repository import PostgresCardRAGRepository
from tests.support_pdf import write_synthetic_pdf

pytestmark = pytest.mark.integration

ISSUER_FIXTURES = Path(__file__).parents[1] / "fixtures/issuers"
GOLD_FIXTURE = Path(__file__).parents[1] / "fixtures/gold/gold_set.v1.json"


def _page_bodies(text: str) -> dict[int, str]:
    return {
        page: "\n".join(value.splitlines()[1:]).strip()
        for page, value in enumerate(split_pages(text), 1)
    }


def _vector(text: str, dimension: int) -> list[float]:
    digest = hashlib.shake_256(text.encode()).digest(dimension * 4)
    raw = [
        int.from_bytes(digest[index : index + 4], "big") / (2**32) - 0.5
        for index in range(0, len(digest), 4)
    ]
    norm = sum(value * value for value in raw) ** 0.5 or 1.0
    return [value / norm for value in raw]


class _FixtureEmbeddingProvider:
    provider = "openrouter"

    def __init__(
        self,
        *,
        model: str,
        dimension: int,
        api_key: str = "fixture",
        base_url: str = "https://fixture.invalid",
        timeout_seconds: float = 1.0,
        **_: object,
    ) -> None:
        del api_key, base_url, timeout_seconds
        self.model = model
        self.dimension = dimension

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [_vector(text, self.dimension) for text in texts]

    async def embed_query(self, query: str) -> list[float]:
        return _vector(query, self.dimension)


class _FixtureAdapter:
    def __init__(self, issuer: Issuer, snapshot: Any) -> None:
        self.issuer = issuer
        self.snapshot = snapshot
        self.allowed_hosts = frozenset(
            {
                Issuer.WOORI: "pc.wooricard.com",
                Issuer.KB: "card.kbcard.com",
                Issuer.SHINHAN: "www.shinhancard.com",
            }[issuer]
            for _ in range(1)
        )
        self.parser_version = f"{issuer.value}.fixture-e2e.v1"

    async def discover(
        self,
        client: object,
        *,
        mode: DiscoveryMode,
        categories: frozenset[str] | None = None,
    ) -> Any:
        del client, categories
        assert mode is self.snapshot.mode
        return self.snapshot

    def download_request(self, source: SourceRecord) -> tuple[str, None]:
        return str(source.source_url), None

    def download_form(self, source: SourceRecord) -> dict[str, str]:
        return {"file_token": source.source_post_id}


class _TokenVerifier(TokenVerifier):
    async def verify_token(self, token: str) -> AccessToken | None:
        scopes = {"fixture-token": ["search", "source_pdf"]}.get(token)
        if scopes is None:
            return None
        return _make_access_token(
            token=token,
            client_id="fixture-client",
            scopes=scopes,
            expires_at=2_000_000_000,
            resource="cardrag-mcp",
            subject="fixture-user",
            claims={"sub": "fixture-user"},
        )


def _records_from_real_parsers() -> dict[Issuer, list[SourceRecord]]:
    discovered_at = datetime(2026, 8, 12, tzinfo=UTC)
    woori = parse_detail_records(
        json.loads((ISSUER_FIXTURES / "woori_detail.json").read_text(encoding="utf-8")),
        product_code="W-GOLD-001",
        product_name="우리 합성 교통카드",
        current_only=False,
        discovered_at=discovered_at,
    )
    kb_current = parse_kb_listing(
        (ISSUER_FIXTURES / "kb_listing.html").read_text(encoding="utf-8"),
        category_code="0",
        discovered_at=discovered_at,
    )[0]
    kb = parse_kb_history(
        (ISSUER_FIXTURES / "kb_history.html").read_text(encoding="utf-8"),
        current=kb_current,
    )
    shinhan_current = parse_shinhan_listing(
        (ISSUER_FIXTURES / "shinhan_page_1.html").read_text(encoding="utf-8"),
        category="credit",
        discovered_at=discovered_at,
    )[0][0]
    shinhan = parse_shinhan_history(
        (ISSUER_FIXTURES / "shinhan_history.html").read_text(encoding="utf-8"),
        current=shinhan_current,
    )
    records = {Issuer.WOORI: woori, Issuer.KB: kb, Issuer.SHINHAN: shinhan}
    assert all(len(values) == 2 and sum(record.is_current for record in values) == 1 for values in records.values())
    return records


def _ocr_by_record(records: dict[Issuer, list[SourceRecord]]) -> dict[str, str]:
    gold = json.loads(GOLD_FIXTURE.read_text(encoding="utf-8"))["documents"]
    values = {str(item["key"]): str(item["ocr"]) for item in gold}
    result: dict[str, str] = {}
    for issuer, issuer_records in records.items():
        for record in issuer_records:
            if issuer is Issuer.WOORI:
                key = "woori-current" if record.is_current else "kb-history"
            elif issuer is Issuer.KB:
                key = "kb-current" if record.is_current else "kb-history"
            else:
                key = "shinhan-current" if record.is_current else "kb-history"
            result[record.source_post_id] = values[key]
    return result


def _write_pdfs(
    root: Path,
    records: dict[Issuer, list[SourceRecord]],
    ocr_by_post: dict[str, str],
) -> dict[str, Path]:
    root.mkdir(parents=True, exist_ok=True)
    result: dict[str, Path] = {}
    for issuer_records in records.values():
        for record in issuer_records:
            target = root / f"{record.source_post_id}.pdf"
            write_synthetic_pdf(
                target,
                [
                    f"CardRAG fixture {record.source_post_id} page {page_number}"
                    for page_number in range(
                        1,
                        len(split_pages(ocr_by_post[record.source_post_id])) + 1,
                    )
                ],
            )
            result[record.source_post_id] = target
    return result


def _settings(tmp_path: Path, database_url: str, secret: Path) -> Settings:
    return Settings(
        environment="test",
        database_url=SecretStr(database_url),
        storage_root=tmp_path / "objects",
        generation_root=tmp_path / "published",
        build_root=tmp_path / "build",
        page_cache_root=tmp_path / "page-cache",
        mcp_server_url="http://test/mcp",
        oidc_issuer="https://id.example/realms/cardrag",
        openrouter_api_key_file=secret,
        embedding_model="fixture-embedding-v1",
        embedding_dimension=1536,
        render_scale=1.0,
        ocr_chunk_pages=2,
        max_job_attempts=3,
        woori_discovery_minimum=2,
        kb_discovery_minimum=2,
        shinhan_discovery_minimum=2,
    )


async def _run_one(worker: WorkerLoop) -> None:
    await worker.run(once=True)


def _active_jobs(database: Postgres) -> int:
    with database.connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*)::int AS n FROM jobs WHERE state IN ('queued','running','retry_wait')"
        )
        row = cursor.fetchone()
    return int(row["n"]) if row else -1


async def _drain(worker: WorkerLoop, database: Postgres) -> None:
    for _ in range(300):
        if _active_jobs(database) == 0:
            return
        with database.connection() as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE jobs SET available_at=now() WHERE state='retry_wait'")
            connection.commit()
        await _run_one(worker)
    raise AssertionError("fixture job graph did not terminate")


def _assert_latest_consistency(database: Postgres, generation_id: str, issuer: str) -> None:
    with database.connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT d.document_id, d.is_latest,
                   bool_and(e.is_latest=d.is_latest) AS evidence_matches
            FROM generation_documents d JOIN evidence e USING (generation_id, document_id)
            WHERE d.generation_id=%s AND d.issuer=%s
            GROUP BY d.document_id, d.is_latest
            ORDER BY d.is_latest DESC, d.document_id
            """,
            (generation_id, issuer),
        )
        rows = cursor.fetchall()
    assert len(rows) == 2
    assert sum(bool(row["is_latest"]) for row in rows) == 1
    assert all(bool(row["evidence_matches"]) for row in rows)


async def _reindex_order(
    database: Postgres,
    jobs: JobRepository,
    worker: WorkerLoop,
    *,
    run_id: str,
    generation_id: str,
    current_first: bool,
) -> None:
    with database.connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT DISTINCT ON (j.document_id) j.document_id, j.payload, d.is_latest
            FROM jobs j JOIN generation_documents d
              ON d.generation_id=%s AND d.document_id=j.document_id
            WHERE j.stage='index' AND j.state='succeeded' AND d.issuer='kb'
            ORDER BY j.document_id, j.created_at
            """,
            (generation_id,),
        )
        rows = list(cursor.fetchall())
    assert len(rows) == 2
    rows.sort(key=lambda row: bool(row["is_latest"]), reverse=current_first)
    label = "current-history" if current_first else "history-current"
    for position, row in enumerate(rows):
        payload = dict(row["payload"])
        payload["run_id"] = run_id
        jobs.enqueue(
            issuer="kb",
            stage="index",
            document_id=str(row["document_id"]),
            idempotency_key=f"fixture-reindex:{label}:{position}:{generation_id}",
            payload=payload,
        )
    await _drain(worker, database)
    _assert_latest_consistency(database, generation_id, "kb")


@pytest.mark.asyncio
async def test_three_issuer_bulk_pipeline_restart_publish_and_http_mcp(
    clean_database: Postgres,
    integration_database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = _records_from_real_parsers()
    ocr_by_post = _ocr_by_record(records)
    source_pdfs = _write_pdfs(tmp_path / "source-pdfs", records, ocr_by_post)
    snapshots = {
        issuer: canonical_snapshot(
            issuer=issuer,
            mode=DiscoveryMode.HISTORY,
            source_url=str(issuer_records[0].source_url),
            parser_version=f"{issuer.value}.fixture-e2e.v1",
            records=issuer_records,
            started_at=datetime(2026, 8, 12, tzinfo=UTC),
        )
        for issuer, issuer_records in records.items()
    }
    adapters = {issuer: _FixtureAdapter(issuer, snapshot) for issuer, snapshot in snapshots.items()}
    secret = tmp_path / "openrouter-key"
    secret.write_text("fixture-only", encoding="utf-8")
    settings = _settings(tmp_path, integration_database_url, secret)

    class FixtureDownloader:
        def __init__(self, policy: object) -> None:
            del policy

        async def download(
            self,
            client: object,
            source: SourceRecord,
            destination: Path,
            **_: object,
        ) -> DownloadedPDF:
            del client
            body = await anyio.Path(source_pdfs[source.source_post_id]).read_bytes()
            await anyio.Path(destination.parent).mkdir(parents=True, exist_ok=True)
            await anyio.Path(destination).write_bytes(body)
            return DownloadedPDF(
                path=destination,
                sha256=hashlib.sha256(body).hexdigest(),
                size=len(body),
                page_count=len(split_pages(ocr_by_post[source.source_post_id])),
                media_type="application/pdf",
                final_url=str(source.source_url),
            )

    pdf_sha_by_post = {
        post_id: hashlib.sha256(path.read_bytes()).hexdigest() for post_id, path in source_pdfs.items()
    }
    document_to_ocr = {
        record.document_identity_for(pdf_sha_by_post[record.source_post_id]).stable_id: ocr_by_post[
            record.source_post_id
        ]
        for issuer_records in records.values()
        for record in issuer_records
    }
    shinhan_current = next(record for record in records[Issuer.SHINHAN] if record.is_current)
    restart_document = shinhan_current.document_identity_for(
        pdf_sha_by_post[shinhan_current.source_post_id]
    ).stable_id

    class FixtureCodexBackend:
        provider = "codex-exec"
        model = "gpt-5.4"
        reasoning_effort: str | None = "high"
        calls: dict[str, list[tuple[int, int]]] = {}
        injected = False

        def __init__(self, **kwargs: object) -> None:
            self.model = str(kwargs.get("model") or self.model)
            self.reasoning_effort = str(kwargs.get("reasoning_effort") or "high")

        async def recognize(
            self,
            images: Sequence[Path],
            *,
            first_page: int,
            prompt: str,
        ) -> str:
            del prompt
            document_id = next(
                parent.name for parent in images[0].parents if parent.name in document_to_ocr
            )
            self.calls.setdefault(document_id, []).append((first_page, len(images)))
            if document_id == restart_document and first_page == 3 and not self.injected:
                type(self).injected = True
                raise RuntimeError("fixture worker interruption")
            pages = _page_bodies(document_to_ocr[document_id])
            return "\n\n".join(
                f"## Page {page}\n\n{pages[page]}"
                for page in range(first_page, first_page + len(images))
            )

    monkeypatch.setattr("cardrag.pipeline.runtime.adapter_for", lambda issuer, **_: adapters[issuer])
    monkeypatch.setattr("cardrag.pipeline.runtime.SecurePDFDownloader", FixtureDownloader)
    monkeypatch.setattr("cardrag.pipeline.runtime.CodexExecBackend", FixtureCodexBackend)
    monkeypatch.setattr(
        "cardrag.pipeline.runtime.OpenRouterEmbeddingProvider",
        _FixtureEmbeddingProvider,
    )

    jobs = JobRepository(clean_database)
    scheduler = DailyScheduler(clean_database, jobs)
    run_id, generation_id = scheduler.create_run(
        run_type="bulk",
        bulk=True,
        embedding_provider="openrouter",
        embedding_model=settings.embedding_model,
        embedding_dimension=settings.embedding_dimension,
        render_scale=settings.render_scale,
        ocr_chunk_pages=settings.ocr_chunk_pages,
    )

    async def no_wait(_: object, __: object) -> None:
        return None

    discover_jobs = await scheduler.enqueue_sequence(
        run_id,
        generation_id,
        bulk=True,
        inter_issuer_seconds=0,
        wait_for_completion=no_wait,
    )
    assert len(discover_jobs) == 3
    first_pipeline = OfflinePipeline(settings, clean_database, jobs)
    first_worker = WorkerLoop(
        jobs,
        first_pipeline,
        worker_id="fixture-worker-before-restart",
        lease_seconds=30,
    )
    for _ in range(200):
        await _run_one(first_worker)
        with clean_database.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT state::text FROM jobs WHERE stage='ocr' AND document_id=%s",
                (restart_document,),
            )
            interrupted = cursor.fetchone()
        if interrupted and interrupted["state"] == "retry_wait":
            break
    else:
        raise AssertionError("fixture OCR interruption was not reached")
    assert FixtureCodexBackend.calls[restart_document] == [(1, 2), (3, 1)]
    with clean_database.connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*)::int AS n FROM stage_checkpoints c JOIN jobs j ON j.id=c.job_id "
            "WHERE j.document_id=%s AND c.attempt_no=1",
            (restart_document,),
        )
        assert cursor.fetchone() == {"n": 2}
        cursor.execute("UPDATE jobs SET available_at=now() WHERE state='retry_wait'")
        connection.commit()

    # A new pipeline and worker reuse the content-addressed artifacts and the
    # page-addressed OCR checkpoint left by the interrupted process.
    second_pipeline = OfflinePipeline(settings, clean_database, jobs)
    second_worker = WorkerLoop(
        jobs,
        second_pipeline,
        worker_id="fixture-worker-after-restart",
        lease_seconds=30,
    )
    await _drain(second_worker, clean_database)
    assert FixtureCodexBackend.calls[restart_document] == [(1, 2), (3, 1), (3, 1)]
    with clean_database.connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*)::int AS n FROM stage_checkpoints c JOIN jobs j ON j.id=c.job_id "
            "WHERE j.document_id=%s AND c.attempt_no=2",
            (restart_document,),
        )
        assert cursor.fetchone() == {"n": 3}
        cursor.execute(
            "SELECT count(*)::int AS n FROM jobs WHERE state IN ('dead_letter','cancelled')"
        )
        assert cursor.fetchone() == {"n": 0}

    # Re-run the actual INDEX handler in both completion orders. A historical
    # document must never demote the adapter-declared current evidence.
    await _reindex_order(
        clean_database,
        jobs,
        second_worker,
        run_id=str(run_id),
        generation_id=generation_id,
        current_first=True,
    )
    await _reindex_order(
        clean_database,
        jobs,
        second_worker,
        run_id=str(run_id),
        generation_id=generation_id,
        current_first=False,
    )

    # Simulate a crashed no-wait supervisor being resumed by `cardrag run
    # finalize`: existing durable jobs are accounted, validated, sealed and
    # published without re-enqueueing.
    monkeypatch.setattr("cardrag.cli._settings", lambda: settings)
    finalized = await _finalize_existing_run(run_id)
    assert finalized["state"] == "succeeded"
    assert finalized["publication"] == "published"
    with clean_database.connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT issuer, discovered_count, succeeded_count, failed_count "
            "FROM run_issuer_status WHERE run_id=%s ORDER BY sequence_no",
            (run_id,),
        )
        accounting = cursor.fetchall()
    assert [(row["issuer"], row["discovered_count"], row["succeeded_count"], row["failed_count"]) for row in accounting] == [
        ("woori", 2, 2, 0),
        ("kb", 2, 2, 0),
        ("shinhan", 2, 2, 0),
    ]

    store = GenerationStore(settings.generation_root, settings.build_root / "generation-candidates")
    builder = GenerationBuilder(clean_database, store)
    search_store = GenerationPinnedPostgresStore(
        clean_database,
        generation_store=store,
        embedding_provider="openrouter",
        embedding_model=settings.embedding_model,
        embedding_dimension=settings.embedding_dimension,
    )
    repository = PostgresCardRAGRepository(
        clean_database,
        store,
        HybridSearchEngine(
            search_store,
            _FixtureEmbeddingProvider(model=settings.embedding_model, dimension=1536),
        ),
        settings.storage_root,
    )
    searched = await repository.search_evidence(
        SearchRequest(
            query="대중교통 할인",
            issuer="woori",
            section_type="benefit",
            limit=10,
        )
    )
    assert searched.items and searched.generation_id == generation_id
    anchor = searched.items[0]
    resolved = await repository.get_evidence(anchor.evidence_id, cursor=None, limit=20)
    assert resolved is not None and any(item.evidence_id == anchor.evidence_id for item in resolved.items)
    versions = await repository.get_product_versions("kb", records[Issuer.KB][0].product_code, as_of=None)
    assert len(versions.items) == 2 and sum(item.is_latest for item in versions.items) == 1
    source = await repository.get_source_pdf(anchor.document_id)
    page = await repository.get_source_page(anchor.document_id, 1)
    assert source is not None and source.path.is_file()
    assert page is not None and page.pdf_sha256 == source.sha256
    assert (await repository.readiness()).ready is True

    # Exercise the actual authenticated streamable-HTTP MCP protocol, not just
    # the repository facade used by the tools.
    import httpx2

    app = build_app(repository, settings, token_verifier=_TokenVerifier())
    http_client = httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer fixture-token"},
    )
    async with (
        app.router.lifespan_context(app),
        http_client,
        streamable_http_client(
            "http://test/mcp",
            http_client=http_client,
            terminate_on_close=False,
        ) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        tools = await session.list_tools()
        search_result = await session.call_tool(
            "search_evidence",
            {
                "query": "대중교통 할인",
                "issuer": "woori",
                "section_type": "benefit",
            },
        )
        evidence_result = await session.call_tool(
            "get_evidence",
            {"evidence_id": anchor.evidence_id},
        )
        page_result = await session.call_tool(
            "get_source_page",
            {"document_id": anchor.document_id, "page": 1},
        )
    assert {tool.name for tool in tools.tools} >= {
        "search_evidence",
        "get_evidence",
        "get_product_versions",
        "get_source_pdf",
        "get_source_page",
    }
    assert search_result.is_error is False
    assert evidence_result.is_error is False
    assert page_result.is_error is False

    # A subsequent unchanged DAILY run must reuse the published immutable
    # OCR/structure/embedding artifacts, execute MATERIALIZE rather than the
    # expensive stages, and retain the published generation after no-change
    # detection.
    for issuer, adapter in adapters.items():
        # Reuse the two-record synthetic catalog so the production minimum
        # discovery-volume guard remains active in this E2E.
        current_records = records[issuer]
        adapter.snapshot = canonical_snapshot(
            issuer=issuer,
            mode=DiscoveryMode.CURRENT,
            source_url=str(current_records[0].source_url),
            parser_version=f"{issuer.value}.fixture-e2e.v1",
            records=current_records,
            started_at=datetime.now(UTC),
        )
    calls_before_reuse = {
        document_id: list(calls) for document_id, calls in FixtureCodexBackend.calls.items()
    }
    reuse_run_id, reuse_generation_id = scheduler.create_run(
        run_type="daily",
        bulk=False,
        embedding_provider="openrouter",
        embedding_model=settings.embedding_model,
        embedding_dimension=settings.embedding_dimension,
        render_scale=settings.render_scale,
        ocr_chunk_pages=settings.ocr_chunk_pages,
    )
    reuse_discovery = await scheduler.enqueue_sequence(
        reuse_run_id,
        reuse_generation_id,
        bulk=False,
        inter_issuer_seconds=0,
        wait_for_completion=no_wait,
    )
    assert len(reuse_discovery) == 3
    reuse_pipeline = OfflinePipeline(settings, clean_database, jobs)
    reuse_worker = WorkerLoop(
        jobs,
        reuse_pipeline,
        worker_id="fixture-worker-reuse",
        lease_seconds=30,
    )
    await _drain(reuse_worker, clean_database)
    for issuer in Issuer:
        await scheduler._wait_issuer(reuse_run_id, issuer)
    assert scheduler.finish_run(reuse_run_id) == "succeeded"
    assert FixtureCodexBackend.calls == calls_before_reuse
    with clean_database.connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT stage::text, count(*)::int AS n
            FROM jobs WHERE payload->>'run_id'=%s
            GROUP BY stage ORDER BY stage
            """,
            (str(reuse_run_id),),
        )
        reuse_stages = {str(row["stage"]): int(row["n"]) for row in cursor.fetchall()}
        cursor.execute(
            """
            SELECT count(*)::int AS document_count,
                   count(*) FILTER (
                       WHERE materialized_from_generation_id=%s
                   )::int AS reused_count
            FROM generation_documents WHERE generation_id=%s
            """,
            (generation_id, reuse_generation_id),
        )
        reuse_documents = cursor.fetchone()
    assert reuse_stages == {"discover": 3, "download": 6, "materialize": 6}
    assert reuse_documents == {"document_count": 6, "reused_count": 6}
    assert builder.skip_if_unchanged(
        reuse_generation_id,
        embedding_provider="openrouter",
        embedding_model=settings.embedding_model,
        dimension=settings.embedding_dimension,
    )
    assert store.current().generation_id == generation_id
