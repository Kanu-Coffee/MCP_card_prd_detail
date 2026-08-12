from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from cardrag.db import Postgres
from cardrag.generation import GenerationStore, new_generation_id
from cardrag.generation_builder import GenerationBuilder
from cardrag.jobs import JobRepository
from cardrag.pipeline.chunks import CHUNK_POLICY_VERSION
from cardrag.pipeline.ocr import OCR_PROMPT_VERSION
from cardrag.pipeline.structure import STRUCTURE_SCHEMA_VERSION
from cardrag.scheduler import DailyScheduler
from cardrag.search.generation_store import GenerationPinnedPostgresStore

pytestmark = pytest.mark.integration


def _vector() -> str:
    return "[1," + ",".join("0" for _ in range(1535)) + "]"


def _snapshots(
    database: Postgres,
    generation_id: str,
    label: str,
    *,
    discovery_mode: str = "current",
) -> dict[str, str]:
    values = {issuer: f"snapshot-{label}-{issuer}" for issuer in ("woori", "kb", "shinhan")}
    with database.connection() as connection, connection.cursor() as cursor:
        for issuer, snapshot_id in values.items():
            cursor.execute(
                """
                INSERT INTO source_snapshots(
                    snapshot_id, issuer, discovery_mode, parser_version, source_url,
                    observed_count, payload_sha256, created_at, completed_at
                ) VALUES (%s,%s,%s,'fixture.v1',%s,0,%s,now(),now())
                """,
                (
                    snapshot_id,
                    issuer,
                    discovery_mode,
                    f"https://{issuer}.example.test",
                    hashlib.sha256(snapshot_id.encode()).hexdigest(),
                ),
            )
            cursor.execute(
                """
                INSERT INTO generation_snapshots(
                    generation_id, issuer, snapshot_id, discovery_mode, completed_at
                ) VALUES (%s,%s,%s,%s,now())
                """,
                (generation_id, issuer, snapshot_id, discovery_mode),
            )
        connection.commit()
    return values


def _insert_complete_document(
    database: Postgres,
    generation_id: str,
    *,
    document_id: str,
    version: str,
    pdf_seed: str,
    snapshot_id: str,
    latest: bool,
    discovery_mode: str = "current",
) -> None:
    pdf_sha = hashlib.sha256(f"pdf:{pdf_seed}".encode()).hexdigest()
    ocr = (
        "## Page 1\n\n"
        "연회비 10,000원\n"
        "대중교통 혜택 10% 할인\n"
        "전월 이용실적 30만원 이상\n"
        "세금은 실적 제외\n"
        "무이자할부는 혜택 제외\n"
    )
    ocr_sha = hashlib.sha256(ocr.encode()).hexdigest()
    structured_sha = hashlib.sha256(f"structured:{pdf_seed}".encode()).hexdigest()
    lines = (
        ("annual_fee", "연회비 10,000원"),
        ("benefit", "대중교통 혜택 10% 할인"),
        ("performance_requirement", "전월 이용실적 30만원 이상"),
        ("performance_exclusion", "세금은 실적 제외"),
        ("benefit_exclusion", "무이자할부는 혜택 제외"),
    )
    spans = {text: (ocr.index(text), ocr.index(text) + len(text)) for _, text in lines}
    with database.connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO source_documents(
                document_id, discovery_id, issuer, product_code, product_name, document_type,
                effective_date, source_version, version_sort_key, source_snapshot_id, source_url,
                pdf_sha256, raw_object_key, last_seen_at, metadata
            ) VALUES (%s,%s,'woori','fixture-card','합성 카드','product_description',%s,%s,
                      %s::jsonb,%s,%s,%s,%s,now(),'{}'::jsonb)
            ON CONFLICT (document_id) DO NOTHING
            """,
            (
                document_id,
                f"discovery-{document_id}",
                date(2026, int(version.removeprefix("v")), 1),
                version,
                json.dumps([[0, int(version.removeprefix("v"))]]),
                snapshot_id,
                f"https://woori.example.test/{document_id}.pdf",
                pdf_sha,
                f"sha256/{pdf_sha}",
            ),
        )
        if latest:
            cursor.execute(
                """
                INSERT INTO generation_expected_documents(
                    generation_id, discovery_id, issuer, source_snapshot_id, discovery_mode,
                    is_current, product_code, document_type, effective_date, source_version,
                    source_url, discovered_at
                ) VALUES (%s,%s,'woori',%s,%s,true,'fixture-card',
                          'product_description',%s,%s,%s,now())
                """,
                (
                    generation_id,
                    f"discovery-{document_id}",
                    snapshot_id,
                    discovery_mode,
                    date(2026, int(version.removeprefix("v")), 1),
                    version,
                    f"https://woori.example.test/{document_id}.pdf",
                ),
            )
            cursor.execute(
                "UPDATE source_snapshots SET observed_count=observed_count+1 WHERE snapshot_id=%s",
                (snapshot_id,),
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
                %s,%s,'woori','fixture-card','합성 카드','product_description',%s,%s,%s::jsonb,
                %s,%s,now(),%s,%s,1024,1,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s,
                'openrouter','openai/text-embedding-3-small',1536,%s,5,5,5,%s
            )
            """,
            (
                generation_id,
                document_id,
                date(2026, int(version.removeprefix("v")), 1),
                version,
                json.dumps([[0, int(version.removeprefix("v"))]]),
                snapshot_id,
                f"https://woori.example.test/{document_id}.pdf",
                pdf_sha,
                f"sha256/{pdf_sha}",
                ocr_sha,
                f"sha256/{ocr_sha}",
                json.dumps([ocr], ensure_ascii=False),
                json.dumps(
                    {
                        "attempt": {
                            "prompt_version": OCR_PROMPT_VERSION,
                            "provider": "codex-exec",
                            "model": "gpt-5.4",
                            "reasoning_effort": "high",
                            "render_scale": 3.0,
                            "chunk_pages": 2,
                        }
                    }
                ),
                structured_sha,
                f"sha256/{structured_sha}",
                STRUCTURE_SCHEMA_VERSION,
                CHUNK_POLICY_VERSION,
                latest,
            ),
        )
        for index, (section_type, text) in enumerate(lines):
            text_sha = hashlib.sha256(text.encode()).hexdigest()
            cursor.execute(
                """
                INSERT INTO evidence(
                    generation_id, evidence_id, document_id, issuer, product_code, product_name,
                    document_type, effective_date, source_version, section_type, page_start,
                    page_end, span_start, span_end, source_spans, text, text_sha256,
                    confidence, is_latest, embedding
                ) VALUES (%s,%s,%s,'woori','fixture-card','합성 카드','product_description',%s,%s,%s,
                          1,1,%s,%s,%s::jsonb,%s,%s,1.0,%s,%s::vector)
                """,
                (
                    generation_id,
                    f"evidence-{document_id}-{index}",
                    document_id,
                    date(2026, int(version.removeprefix("v")), 1),
                    version,
                    section_type,
                    spans[text][0],
                    spans[text][1],
                    json.dumps(
                        [
                            {
                                "page": 1,
                                "start": spans[text][0],
                                "end": spans[text][1],
                                "quote_sha256": text_sha,
                            }
                        ]
                    ),
                    text,
                    text_sha,
                    latest,
                    _vector(),
                ),
            )
        artifacts = (
            ("source_pdf", pdf_sha),
            ("ocr_markdown", ocr_sha),
            ("structured", structured_sha),
            ("embedding", hashlib.sha256(f"embedding:{pdf_seed}".encode()).hexdigest()),
            ("lexical_index", hashlib.sha256(f"lexical:{pdf_seed}".encode()).hexdigest()),
            ("vector_index", hashlib.sha256(f"vector:{pdf_seed}".encode()).hexdigest()),
        )
        for artifact_type, content_sha in artifacts:
            cursor.execute(
                """
                INSERT INTO generation_artifacts(
                    generation_id, manifest_id, artifact_id, document_id, artifact_type,
                    content_sha256, size_bytes, media_type, manifest_object_key, manifest, created_at
                ) VALUES (%s,%s,%s,%s,%s,%s,1,'application/octet-stream',%s,%s::jsonb,now())
                """,
                (
                    generation_id,
                    f"manifest-{generation_id}-{document_id}-{artifact_type}",
                    f"artifact-{document_id}-{artifact_type}-{content_sha}",
                    document_id,
                    artifact_type,
                    content_sha,
                    f"sha256/{content_sha}",
                    json.dumps({"schema_version": "cardrag.artifact-manifest.v1"}),
                ),
            )
        connection.commit()


def _insert_missing_expectation(
    database: Postgres,
    generation_id: str,
    *,
    issuer: str,
    snapshot_id: str,
    discovery_mode: str,
) -> None:
    discovery_id = f"discovery-missing-{issuer}"
    with database.connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO generation_expected_documents(
                generation_id, discovery_id, issuer, source_snapshot_id, discovery_mode,
                is_current, product_code, document_type, effective_date, source_version,
                source_url, discovered_at
            ) VALUES (%s,%s,%s,%s,%s,true,%s,'product_description','2026-08-12','v1',%s,now())
            """,
            (
                generation_id,
                discovery_id,
                issuer,
                snapshot_id,
                discovery_mode,
                f"{issuer}-fixture-card",
                f"https://{issuer}.example.test/missing.pdf",
            ),
        )
        cursor.execute(
            "UPDATE source_snapshots SET observed_count=observed_count+1 WHERE snapshot_id=%s",
            (snapshot_id,),
        )
        connection.commit()


def _new_candidate(database: Postgres, generation_id: str) -> None:
    with database.connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO generations(
                generation_id, state, manifest_sha256, root_uri, schema_version,
                embedding_provider, embedding_model, embedding_dimension
            ) VALUES (%s,'building',repeat('0',64),'','cardrag-generation.v1',
                      'openrouter','openai/text-embedding-3-small',1536)
            """,
            (generation_id,),
        )
        connection.commit()


def test_bulk_current_expectations_from_all_issuers_must_materialize(
    clean_database: Postgres,
    tmp_path: Path,
) -> None:
    generation_id = new_generation_id(datetime(2026, 8, 12, 0, 0, tzinfo=UTC), "0" * 12)
    _new_candidate(clean_database, generation_id)
    snapshots = _snapshots(clean_database, generation_id, "bulk-missing", discovery_mode="history")
    _insert_complete_document(
        clean_database,
        generation_id,
        document_id="doc-bulk-woori",
        version="v1",
        pdf_seed="bulk-woori",
        snapshot_id=snapshots["woori"],
        latest=True,
        discovery_mode="history",
    )
    for issuer in ("kb", "shinhan"):
        _insert_missing_expectation(
            clean_database,
            generation_id,
            issuer=issuer,
            snapshot_id=snapshots[issuer],
            discovery_mode="history",
        )
    builder = GenerationBuilder(
        clean_database,
        GenerationStore(tmp_path / "published", tmp_path / "build"),
    )
    quality, retrieval = builder.evaluate(generation_id)
    with pytest.raises(ValueError, match="current discovery records were not materialized"):
        builder.seal(
            generation_id,
            embedding_provider="openrouter",
            embedding_model="openai/text-embedding-3-small",
            dimension=1536,
            quality_report=quality,
            retrieval_report=retrieval,
        )


def test_quality_evaluation_rejects_fabricated_exact_source_span(
    clean_database: Postgres,
    tmp_path: Path,
) -> None:
    generation_id = new_generation_id(datetime(2026, 8, 12, 0, 0, tzinfo=UTC), "f" * 12)
    _new_candidate(clean_database, generation_id)
    snapshots = _snapshots(clean_database, generation_id, "bad-span")
    _insert_complete_document(
        clean_database,
        generation_id,
        document_id="doc-bad-span",
        version="v1",
        pdf_seed="bad-span",
        snapshot_id=snapshots["woori"],
        latest=True,
    )
    with clean_database.connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE evidence SET source_spans=jsonb_build_array(jsonb_build_object(
                'page', 1, 'start', 0, 'end', 1, 'quote_sha256', repeat('0', 64)
            )), page_start=1, page_end=1, span_start=0, span_end=1
            WHERE generation_id=%s AND evidence_id=(
                SELECT evidence_id FROM evidence WHERE generation_id=%s ORDER BY evidence_id LIMIT 1
            )
            """,
            (generation_id, generation_id),
        )
        connection.commit()
    builder = GenerationBuilder(
        clean_database,
        GenerationStore(tmp_path / "published", tmp_path / "build"),
    )
    quality, _ = builder.evaluate(generation_id)
    assert quality.status == "failed"
    assert quality.structure_span_accuracy < 1.0


def test_daily_contract_change_requires_bulk_to_preserve_history(
    clean_database: Postgres,
    tmp_path: Path,
) -> None:
    store = GenerationStore(tmp_path / "published", tmp_path / "build")
    builder = GenerationBuilder(clean_database, store)
    first = new_generation_id(datetime(2026, 8, 12, 0, 0, tzinfo=UTC), "c" * 12)
    _new_candidate(clean_database, first)
    snapshots = _snapshots(clean_database, first, "contract-history")
    _insert_complete_document(
        clean_database,
        first,
        document_id="doc-history-old",
        version="v1",
        pdf_seed="history-old",
        snapshot_id=snapshots["woori"],
        latest=False,
    )
    _insert_complete_document(
        clean_database,
        first,
        document_id="doc-history-current",
        version="v2",
        pdf_seed="history-current",
        snapshot_id=snapshots["woori"],
        latest=True,
    )
    quality, retrieval = builder.evaluate(first)
    builder.seal(
        first,
        embedding_provider="openrouter",
        embedding_model="openai/text-embedding-3-small",
        dimension=1536,
        quality_report=quality,
        retrieval_report=retrieval,
    )
    builder.publish(first)

    scheduler = DailyScheduler(clean_database, object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="BULK rebuild.*preserve all versions"):
        scheduler.create_run(
            run_type="daily",
            bulk=False,
            embedding_provider="openrouter",
            embedding_model="changed-embedding-model",
            embedding_dimension=1536,
        )
    with clean_database.connection() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT count(*)::int AS n FROM pipeline_runs")
        assert cursor.fetchone() == {"n": 0}

    _, bulk_generation = scheduler.create_run(
        run_type="bulk",
        bulk=True,
        embedding_provider="openrouter",
        embedding_model="changed-embedding-model",
        embedding_dimension=1536,
    )
    with clean_database.connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*)::int AS n FROM generation_documents WHERE generation_id=%s",
            (bulk_generation,),
        )
        assert cursor.fetchone() == {"n": 0}


def test_generation_materialization_no_change_publish_and_compensation(
    clean_database: Postgres,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = GenerationStore(tmp_path / "published", tmp_path / "build")
    builder = GenerationBuilder(clean_database, store)
    first = new_generation_id(datetime(2026, 8, 12, 0, 0, tzinfo=UTC), "1" * 12)
    _new_candidate(clean_database, first)
    first_snapshots = _snapshots(clean_database, first, "first")
    _insert_complete_document(
        clean_database,
        first,
        document_id="doc-fixture-v1",
        version="v1",
        pdf_seed="one",
        snapshot_id=first_snapshots["woori"],
        latest=True,
    )
    quality, retrieval = builder.evaluate(first)
    builder.seal(
        first,
        embedding_provider="openrouter",
        embedding_model="openai/text-embedding-3-small",
        dimension=1536,
        quality_report=quality,
        retrieval_report=retrieval,
    )
    builder.publish(first)

    scheduler = DailyScheduler(clean_database, object())  # type: ignore[arg-type]
    _, second = scheduler.create_run(
        run_type="daily",
        bulk=False,
        embedding_provider="openrouter",
        embedding_model="openai/text-embedding-3-small",
        embedding_dimension=1536,
    )
    _snapshots(clean_database, second, "second")
    with clean_database.connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT count(*)::int AS documents,
                   count(*) FILTER (WHERE materialized_from_generation_id=%s)::int AS materialized
            FROM generation_documents WHERE generation_id=%s
            """,
            (first, second),
        )
        copied = cursor.fetchone()
        cursor.execute("SELECT count(*)::int AS n FROM evidence WHERE generation_id=%s", (second,))
        copied_evidence = cursor.fetchone()
    assert copied == {"documents": 1, "materialized": 1}
    assert copied_evidence == {"n": 5}
    assert builder.skip_if_unchanged(
        second,
        embedding_provider="openrouter",
        embedding_model="openai/text-embedding-3-small",
        dimension=1536,
    )
    assert store.current().generation_id == first

    cancelled_run, cancelled_generation = scheduler.create_run(
        run_type="daily",
        bulk=False,
        embedding_provider="openrouter",
        embedding_model="openai/text-embedding-3-small",
        embedding_dimension=1536,
    )
    _snapshots(clean_database, cancelled_generation, "cancelled")
    jobs = JobRepository(clean_database)
    cancelled_job, _ = jobs.enqueue(
        issuer="woori",
        stage="materialize",
        document_id="doc-fixture-v1",
        idempotency_key=f"materialize:{cancelled_generation}:doc-fixture-v1",
        payload={
            "run_id": str(cancelled_run),
            "generation_id": cancelled_generation,
        },
    )
    assert jobs.cancel(cancelled_job).value == "cancelled"
    with pytest.raises(ValueError, match="terminal processing failures"):
        builder.skip_if_unchanged(
            cancelled_generation,
            embedding_provider="openrouter",
            embedding_model="openai/text-embedding-3-small",
            dimension=1536,
        )

    _, third = scheduler.create_run(
        run_type="daily",
        bulk=False,
        embedding_provider="openrouter",
        embedding_model="openai/text-embedding-3-small",
        embedding_dimension=1536,
    )
    third_snapshots = _snapshots(clean_database, third, "third")
    with clean_database.connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            "UPDATE generation_documents SET is_latest=false WHERE generation_id=%s",
            (third,),
        )
        cursor.execute("UPDATE evidence SET is_latest=false WHERE generation_id=%s", (third,))
        connection.commit()
    _insert_complete_document(
        clean_database,
        third,
        document_id="doc-fixture-v2",
        version="v2",
        pdf_seed="two",
        snapshot_id=third_snapshots["woori"],
        latest=True,
    )
    quality, retrieval = builder.evaluate(third)
    builder.seal(
        third,
        embedding_provider="openrouter",
        embedding_model="openai/text-embedding-3-small",
        dimension=1536,
        quality_report=quality,
        retrieval_report=retrieval,
    )
    builder.publish(third)
    online = GenerationPinnedPostgresStore(
        clean_database,
        generation_store=store,
        embedding_provider="openrouter",
        embedding_model="openai/text-embedding-3-small",
        embedding_dimension=1536,
    )
    assert online.validate_active_generation_sync().generation_id == third

    _, fourth = scheduler.create_run(
        run_type="daily",
        bulk=False,
        embedding_provider="openrouter",
        embedding_model="openai/text-embedding-3-small",
        embedding_dimension=1536,
    )
    fourth_snapshots = _snapshots(clean_database, fourth, "fourth")
    with clean_database.connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            "UPDATE generation_documents SET is_latest=false WHERE generation_id=%s",
            (fourth,),
        )
        cursor.execute("UPDATE evidence SET is_latest=false WHERE generation_id=%s", (fourth,))
        connection.commit()
    _insert_complete_document(
        clean_database,
        fourth,
        document_id="doc-fixture-v3",
        version="v3",
        pdf_seed="three",
        snapshot_id=fourth_snapshots["woori"],
        latest=True,
    )
    quality, retrieval = builder.evaluate(fourth)
    builder.seal(
        fourth,
        embedding_provider="openrouter",
        embedding_model="openai/text-embedding-3-small",
        dimension=1536,
        quality_report=quality,
        retrieval_report=retrieval,
    )

    observed_during_swap: list[str] = []

    def fail_pointer(_: str) -> None:
        # PostgreSQL is already atomically advanced here while current.json is
        # still old. A concurrent request remains available and pins the new
        # sealed generation rather than observing a mixed pair.
        observed_during_swap.append(online.validate_active_generation_sync().generation_id)
        raise OSError("injected pointer failure")

    original_publish_locked = store.publish_locked
    monkeypatch.setattr(store, "publish_locked", fail_pointer)
    with pytest.raises(RuntimeError, match="compensated"):
        builder.publish(fourth)
    assert observed_during_swap == [fourth]
    with clean_database.connection() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT generation_id FROM active_generation WHERE singleton=true")
        active = cursor.fetchone()
        cursor.execute("SELECT state FROM generations WHERE generation_id=%s", (fourth,))
        fourth_state = cursor.fetchone()
    assert active == {"generation_id": third}
    assert fourth_state == {"state": "ready"}
    assert store.current().generation_id == third

    # Rehearse the operator rollback path end-to-end: both serving authorities
    # must move to the previous retired generation.
    monkeypatch.setattr(store, "publish_locked", original_publish_locked)
    assert builder.rollback() == first
    with clean_database.connection() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT generation_id FROM active_generation WHERE singleton=true")
        rolled_back = cursor.fetchone()
        cursor.execute("SELECT state FROM generations WHERE generation_id=%s", (third,))
        retired = cursor.fetchone()
    assert rolled_back == {"generation_id": first}
    assert retired == {"state": "retired"}
    assert store.current().generation_id == first
