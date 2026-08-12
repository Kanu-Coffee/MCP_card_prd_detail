from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from cardrag.db import Postgres
from cardrag.domain import Issuer
from cardrag.generation import GenerationManifest, GenerationStore
from cardrag.search.embeddings import FakeEmbeddingProvider
from cardrag.search.generation_store import ActiveGenerationMismatch, GenerationPinnedPostgresStore
from cardrag.search.hybrid import HybridSearchEngine, SearchFilters
from cardrag.search.postgres_store import PostgresSearchStore
from cardrag.service.models import SearchRequest
from cardrag.service.postgres_repository import PostgresCardRAGRepository

pytestmark = pytest.mark.integration

GENERATION_ID = "gen-20260812T000000Z-aaaaaaaaaaaa"


def _vector_literal(values: list[float]) -> str:
    return "[" + ",".join(str(value) for value in values) + "]"


@pytest.fixture()
def populated_search(clean_database: Postgres) -> tuple[Postgres, FakeEmbeddingProvider]:
    embedder = FakeEmbeddingProvider(model="fake-v1", dimension=1536)
    vector = embedder._vector("query:airport lounge")
    distant = embedder._vector("unrelated")
    with clean_database.connection() as connection, connection.cursor() as cursor:
        for issuer in ("woori", "kb"):
            cursor.execute(
                """
                INSERT INTO source_snapshots(
                    snapshot_id, issuer, discovery_mode, parser_version, source_url,
                    observed_count, payload_sha256, created_at, completed_at
                ) VALUES (%s,%s,'history','fixture-v1',%s,1,%s,now(),now())
                """,
                (f"snapshot-{issuer}", issuer, f"https://example.test/{issuer}", "f" * 64),
            )
        cursor.execute(
            """
            INSERT INTO generations(generation_id, state, manifest_sha256, root_uri, schema_version,
                                    embedding_provider, embedding_model, embedding_dimension,
                                    latest_document_count, latest_covered_count)
            VALUES (%s, 'building', %s, '/fixture', 'cardrag-generation.v1', 'fake', 'fake-v1', 1536, 2, 2)
            """,
            (GENERATION_ID, "a" * 64),
        )
        rows = [
            (
                "evidence-woori-lounge",
                "doc-woori-v10",
                "woori",
                "same-code",
                "우리 공항 카드",
                date(2026, 1, 1),
                "v10",
                "benefit",
                "airport lounge benefit twice monthly",
                hashlib.sha256(b"airport lounge benefit twice monthly").hexdigest(),
                True,
                vector,
            ),
            (
                "evidence-kb-lounge",
                "doc-kb-v1",
                "kb",
                "same-code",
                "KB 공항 카드",
                date(2026, 1, 2),
                "v1",
                "benefit",
                "airport lounge benefit once monthly",
                hashlib.sha256(b"airport lounge benefit once monthly").hexdigest(),
                True,
                distant,
            ),
            (
                "evidence-woori-history",
                "doc-woori-v9",
                "woori",
                "same-code",
                "우리 공항 카드",
                date(2025, 1, 1),
                "v9",
                "benefit",
                "old airport lounge condition",
                hashlib.sha256(b"old airport lounge condition").hexdigest(),
                False,
                vector,
            ),
            (
                "evidence-woori-older-history",
                "doc-woori-v8",
                "woori",
                "same-code",
                "우리 공항 카드",
                date(2024, 1, 1),
                "v8",
                "benefit",
                "older airport lounge condition",
                hashlib.sha256(b"older airport lounge condition").hexdigest(),
                False,
                vector,
            ),
        ]
        for row in rows:
            pdf_sha256 = {
                "doc-woori-v10": "a",
                "doc-kb-v1": "b",
                "doc-woori-v9": "c",
                "doc-woori-v8": "d",
            }[row[1]] * 64
            cursor.execute(
                """
                INSERT INTO source_documents(
                    document_id, discovery_id, issuer, product_code, product_name, document_type,
                    effective_date, source_version, version_sort_key, source_snapshot_id,
                    source_url, pdf_sha256, raw_object_key, last_seen_at
                ) VALUES (%s,%s,%s,%s,%s,'product_description',%s,%s,'[]'::jsonb,%s,%s,%s,%s,now())
                """,
                (
                    row[1],
                    "discovery-" + row[1],
                    row[2],
                    row[3],
                    row[4],
                    row[5],
                    row[6],
                    f"snapshot-{row[2]}",
                    f"https://example.test/{row[2]}/{row[1]}.pdf",
                    pdf_sha256,
                    f"sha256/{pdf_sha256}",
                ),
            )
            cursor.execute(
                """
                INSERT INTO generation_documents(
                    generation_id, document_id, issuer, product_code, product_name, document_type,
                    effective_date, source_version, version_sort_key, source_snapshot_id, source_url,
                    discovered_at, pdf_sha256, raw_object_key, pdf_size_bytes, pdf_page_count,
                    ocr_sha256, ocr_object_key, ocr_pages, ocr_manifest,
                    structured_sha256, structured_object_key, structure_schema_version,
                    embedding_provider, embedding_model, embedding_dimension, chunk_policy,
                    chunk_count, embedding_count, index_count, is_latest
                ) VALUES (
                    %s,%s,%s,%s,%s,'product_description',%s,%s,'[]'::jsonb,%s,%s,
                    now(),%s,%s,123,1,%s,%s,%s::jsonb,'{}'::jsonb,%s,%s,'fixture-structure-v1',
                    'fake','fake-v1',1536,'fixture-chunk-v1',1,1,1,%s
                )
                """,
                (
                    GENERATION_ID,
                    row[1],
                    row[2],
                    row[3],
                    row[4],
                    row[5],
                    row[6],
                    f"snapshot-{row[2]}",
                    f"https://example.test/{row[2]}/{row[1]}.pdf",
                    pdf_sha256,
                    f"sha256/{pdf_sha256}",
                    "d" * 64,
                    f"sha256/{'d' * 64}",
                    '["fixture OCR page"]',
                    "e" * 64,
                    f"sha256/{'e' * 64}",
                    row[10],
                ),
            )
            cursor.execute(
                """
                INSERT INTO evidence(generation_id, evidence_id, document_id, issuer, product_code,
                                     product_name, document_type, effective_date, source_version,
                                     section_type, page_start, page_end, span_start, span_end,
                                     source_spans, text, text_sha256, confidence, is_latest, embedding)
                VALUES (%s,%s,%s,%s,%s,%s,'product_description',%s,%s,%s,1,1,0,100,
                        %s::jsonb,%s,%s,1.0,%s,%s::vector)
                """,
                (
                    GENERATION_ID,
                    *row[:8],
                    json.dumps(
                        [{"page": 1, "start": 0, "end": 100, "quote_sha256": row[9]}]
                    ),
                    *row[8:-1],
                    _vector_literal(row[-1]),
                ),
            )
        cursor.execute("UPDATE generations SET state='published', published_at=now() WHERE generation_id=%s", (GENERATION_ID,))
        cursor.execute(
            "INSERT INTO active_generation(singleton, generation_id) VALUES (true, %s)",
            (GENERATION_ID,),
        )
        connection.commit()
    embedder.query_calls = 0
    return clean_database, embedder


def _published_store(tmp_path: Path, database: Postgres) -> GenerationStore:
    store = GenerationStore(tmp_path / "published", tmp_path / "build")
    candidate = store.candidate_path(GENERATION_ID)
    (candidate / "catalog.json").write_text('{"fixture":true}\n', encoding="utf-8")
    manifest = GenerationManifest(
        generation_id=GENERATION_ID,
        created_at=datetime(2026, 8, 12, tzinfo=UTC),
        source_snapshot_ids=("snapshot-kb", "snapshot-woori"),
        document_count=4,
        latest_document_count=2,
        latest_pdf_count=2,
        latest_ocr_count=2,
        latest_structure_count=2,
        latest_embedding_count=2,
        latest_index_count=2,
        historical_quarantine_count=0,
        embedding_provider="fake",
        embedding_model="fake-v1",
        embedding_dimension=1536,
        chunk_policy="fixture-chunk-v1",
        taxonomy_version="fixture-taxonomy-v1",
        files=store.build_file_inventory(candidate),
        quality_report_sha256=hashlib.sha256(b"quality").hexdigest(),
        retrieval_report_sha256=hashlib.sha256(b"retrieval").hexdigest(),
    )
    sealed = store.seal(candidate, manifest)
    store.publish(GENERATION_ID)
    with database.connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE generations SET manifest_sha256=%s, root_uri=%s,
                embedding_provider=%s, embedding_model=%s, embedding_dimension=%s,
                latest_document_count=%s, latest_covered_count=%s
            WHERE generation_id=%s
            """,
            (
                manifest.sha256,
                sealed.as_posix(),
                manifest.embedding_provider,
                manifest.embedding_model,
                manifest.embedding_dimension,
                manifest.latest_document_count,
                manifest.latest_index_count,
                GENERATION_ID,
            ),
        )
        connection.commit()
    return store


@pytest.mark.asyncio
async def test_pgvector_hybrid_uses_one_query_embedding_and_issuer_prefilter(
    populated_search: tuple[Postgres, FakeEmbeddingProvider],
) -> None:
    database, embedder = populated_search
    engine = HybridSearchEngine(PostgresSearchStore(database), embedder)
    result = await engine.search(
        "airport lounge",
        filters=SearchFilters(issuer=Issuer.WOORI),
        limit=10,
    )
    assert embedder.query_calls == 1
    assert result.generation_id == GENERATION_ID
    assert result.retrieval_mode == "hybrid"
    assert {hit.issuer for hit in result.hits} == {Issuer.WOORI}
    assert {hit.source_version for hit in result.hits} == {"v10"}
    fused = next(hit for hit in result.hits if hit.evidence_id == "evidence-woori-lounge")
    assert fused.lexical_rank is not None
    assert fused.vector_rank is not None
    assert fused.pdf_sha256 == "a" * 64
    assert fused.text_sha256 == hashlib.sha256(
        b"airport lounge benefit twice monthly"
    ).hexdigest()


@pytest.mark.asyncio
async def test_explicit_version_and_as_of_select_history(
    populated_search: tuple[Postgres, FakeEmbeddingProvider],
) -> None:
    database, embedder = populated_search
    engine = HybridSearchEngine(PostgresSearchStore(database), embedder)
    versioned = await engine.search(
        "airport lounge",
        filters=SearchFilters(issuer=Issuer.WOORI, version="v9"),
        limit=10,
    )
    assert {hit.source_version for hit in versioned.hits} == {"v9"}
    historical = await engine.search(
        "airport lounge",
        filters=SearchFilters(issuer=Issuer.WOORI, as_of=date(2025, 12, 31)),
        limit=10,
    )
    assert {hit.source_version for hit in historical.hits} == {"v9"}


@pytest.mark.asyncio
async def test_generation_pinned_repository_pages_and_reports_ready(
    populated_search: tuple[Postgres, FakeEmbeddingProvider],
    tmp_path: Path,
) -> None:
    database, embedder = populated_search
    generation_store = _published_store(tmp_path, database)
    search_store = GenerationPinnedPostgresStore(
        database,
        generation_store=generation_store,
        embedding_provider=embedder.provider,
        embedding_model=embedder.model,
        embedding_dimension=embedder.dimension,
    )
    repository = PostgresCardRAGRepository(
        database,
        generation_store,
        HybridSearchEngine(search_store, embedder),
        tmp_path,
    )

    first = await repository.search_evidence(SearchRequest(query="airport lounge", limit=1))
    assert first.next_cursor is not None
    second = await repository.search_evidence(
        SearchRequest(query="airport lounge", limit=1, cursor=first.next_cursor)
    )

    assert first.items[0].evidence_id != second.items[0].evidence_id
    assert embedder.query_calls == 2  # once per stateless page request
    assert first.items[0].text_sha256 != first.items[0].pdf_sha256
    readiness = await repository.readiness()
    assert readiness.ready is True
    assert readiness.generation_id == GENERATION_ID
    assert all(readiness.checks.values())

    source = await repository.get_source_pdf(first.items[0].document_id)
    page = await repository.get_source_page(first.items[0].document_id, 1)
    assert source is not None and source.sha256 == first.items[0].pdf_sha256
    assert source.size_bytes == 123
    assert page is not None and page.ocr_text == "fixture OCR page"
    assert page.pdf_sha256 == first.items[0].pdf_sha256


@pytest.mark.asyncio
async def test_evidence_lookup_begins_at_the_requested_stable_anchor(
    populated_search: tuple[Postgres, FakeEmbeddingProvider],
    tmp_path: Path,
) -> None:
    database, embedder = populated_search
    anchor_id = "late-anchor"
    with database.connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            "UPDATE generations SET state='building' WHERE generation_id=%s",
            (GENERATION_ID,),
        )
        for index in range(25):
            text = f"earlier evidence {index}"
            digest = hashlib.sha256(text.encode()).hexdigest()
            cursor.execute(
                """
                INSERT INTO evidence(
                    generation_id,evidence_id,document_id,issuer,product_code,product_name,
                    document_type,effective_date,source_version,section_type,page_start,page_end,
                    span_start,span_end,source_spans,text,text_sha256,confidence,is_latest
                ) VALUES (
                    %s,%s,'doc-woori-v10','woori','same-code','우리 공항 카드',
                    'product_description','2026-01-01','v10','benefit',1,1,
                    %s,%s,%s::jsonb,%s,%s,1,true
                )
                """,
                (
                    GENERATION_ID,
                    f"earlier-{index:02d}",
                    index,
                    index + 1,
                    json.dumps(
                        [{"page": 1, "start": index, "end": index + 1, "quote_sha256": digest}]
                    ),
                    text,
                    digest,
                ),
            )
        anchor_text = "late stable anchor"
        anchor_digest = hashlib.sha256(anchor_text.encode()).hexdigest()
        cursor.execute(
            """
            INSERT INTO evidence(
                generation_id,evidence_id,document_id,issuer,product_code,product_name,
                document_type,effective_date,source_version,section_type,page_start,page_end,
                span_start,span_end,source_spans,text,text_sha256,confidence,is_latest
            ) VALUES (
                %s,%s,'doc-woori-v10','woori','same-code','우리 공항 카드',
                'product_description','2026-01-01','v10','benefit',2,2,
                0,18,%s::jsonb,%s,%s,1,true
            )
            """,
            (
                GENERATION_ID,
                anchor_id,
                json.dumps(
                    [{"page": 2, "start": 0, "end": 18, "quote_sha256": anchor_digest}]
                ),
                anchor_text,
                anchor_digest,
            ),
        )
        cursor.execute(
            "UPDATE generations SET state='published' WHERE generation_id=%s",
            (GENERATION_ID,),
        )
        connection.commit()

    generation_store = _published_store(tmp_path, database)
    repository = PostgresCardRAGRepository(
        database,
        generation_store,
        HybridSearchEngine(
            GenerationPinnedPostgresStore(
                database,
                generation_store=generation_store,
                embedding_provider=embedder.provider,
                embedding_model=embedder.model,
                embedding_dimension=embedder.dimension,
            ),
            embedder,
        ),
        tmp_path,
    )

    page = await repository.get_evidence(anchor_id, cursor=None, limit=20)

    assert page is not None
    assert page.items[0].evidence_id == anchor_id


@pytest.mark.asyncio
async def test_repository_search_and_readiness_fail_closed_on_generation_drift(
    populated_search: tuple[Postgres, FakeEmbeddingProvider],
    tmp_path: Path,
) -> None:
    database, embedder = populated_search
    generation_store = _published_store(tmp_path, database)
    search_store = GenerationPinnedPostgresStore(
        database,
        generation_store=generation_store,
        embedding_provider=embedder.provider,
        embedding_model=embedder.model,
        embedding_dimension=embedder.dimension,
    )
    repository = PostgresCardRAGRepository(
        database,
        generation_store,
        HybridSearchEngine(search_store, embedder),
        tmp_path,
    )
    with database.connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            "UPDATE generations SET manifest_sha256=%s WHERE generation_id=%s",
            ("0" * 64, GENERATION_ID),
        )
        connection.commit()

    with pytest.raises(ActiveGenerationMismatch, match="manifest hashes differ"):
        await repository.search_evidence(SearchRequest(query="airport lounge"))
    readiness = await repository.readiness()
    assert readiness.ready is False
    assert readiness.checks["generation"] is False
    assert not any(readiness.checks.values())


def test_published_generation_rejects_all_evidence_mutation(populated_search: tuple[Postgres, FakeEmbeddingProvider]) -> None:
    database, _ = populated_search
    with database.connection() as connection, connection.cursor() as cursor:
        with pytest.raises(Exception, match="immutable"):
            cursor.execute(
                "UPDATE evidence SET text='tampered' WHERE generation_id=%s",
                (GENERATION_ID,),
            )
        connection.rollback()
        with pytest.raises(Exception, match="immutable"):
            cursor.execute(
                """
                INSERT INTO evidence(generation_id,evidence_id,document_id,issuer,product_code,product_name,
                                     document_type,effective_date,source_version,section_type,page_start,page_end,
                                     span_start,span_end,source_spans,text,text_sha256,confidence,is_latest)
                VALUES (%s,'late','doc','woori','code','name','product_description','2026-01-01','v1',
                        'benefit',1,1,0,1,%s::jsonb,'x',%s,1,true)
                """,
                (
                    GENERATION_ID,
                    json.dumps([{"page": 1, "start": 0, "end": 1, "quote_sha256": "4" * 64}]),
                    "4" * 64,
                ),
            )
        connection.rollback()
