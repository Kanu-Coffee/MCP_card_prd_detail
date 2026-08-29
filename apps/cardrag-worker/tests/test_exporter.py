from __future__ import annotations

import math
import sqlite3
import struct
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from cardrag_core import (
    DOCUMENT_EMBEDDING_PREFIX,
    EMBEDDING_POLICY_VERSION,
    QUERY_EMBEDDING_PREFIX,
)

from cardrag_worker.contracts import (
    DocumentRecord,
    EvidenceRecord,
    IssuerSpec,
    PageRecord,
    SourceRecord,
    UnsupportedProductRecord,
    canonical_sha256,
)
from cardrag_worker.exporter import ServingDatabaseError, ServingDatabaseExporter


def fixture_rows() -> tuple[IssuerSpec, DocumentRecord, EvidenceRecord]:
    issuer = IssuerSpec(
        code="kb",
        display_name="KB국민카드",
        sort_order=20,
        allowed_hosts=frozenset({"kb.example"}),
        categories=("credit",),
    )
    page_text = "연회비 안내와 전월 이용실적 조건을 정확하게 설명하는 문장입니다."
    page = PageRecord(document_id="doc_kb", page=1, text=page_text)
    document = DocumentRecord(
        document_id="doc_kb",
        issuer="kb",
        product_code="p1",
        product_name="테스트 카드",
        title="테스트 카드 상품설명서",
        pdf_sha256="a" * 64,
        pdf_size_bytes=123,
        page_count=1,
        pages=(page,),
    )
    start = page_text.index("전월")
    end = len(page_text)
    evidence = EvidenceRecord(
        evidence_id="evidence_001",
        document_id=document.document_id,
        page_start=1,
        page_end=1,
        section_type="body",
        text=page_text[start:end],
        source_start=start,
        source_end=end,
        embedding=[2.0] + [0.0] * 1535,
    )
    return issuer, document, evidence


def test_exporter_builds_vacuumed_exact_schema_and_normalized_vectors(tmp_path: Path) -> None:
    issuer, document, evidence = fixture_rows()
    target = tmp_path / "index.sqlite3"
    result = ServingDatabaseExporter().export(
        target,
        generation_id="g-test",
        corpus_sha256="b" * 64,
        contract_sha256="c" * 64,
        embedding_provider="openrouter",
        embedding_model="openai/text-embedding-3-small",
        issuers=[issuer],
        documents=[document],
        evidence=[evidence],
    )
    assert result.path == target
    connection = sqlite3.connect(f"{target.as_uri()}?mode=ro&immutable=1", uri=True)
    try:
        metadata = dict(connection.execute("SELECT key,value FROM metadata"))
        assert metadata["schema_id"] == "cardrag.serving-db.v4"
        assert metadata["corpus_sha256"] == "b" * 64
        assert metadata["contract_sha256"] == "c" * 64
        assert metadata["unsupported_document_count"] == "0"
        assert metadata["embedding_input_policy_version"] == EMBEDDING_POLICY_VERSION
        assert metadata["embedding_document_prefix"] == DOCUMENT_EMBEDDING_PREFIX
        assert metadata["embedding_query_prefix"] == QUERY_EMBEDDING_PREFIX
        columns = [row[1] for row in connection.execute("PRAGMA table_info(evidence)")]
        assert columns[:2] == ["evidence_pk", "evidence_id"]
        primary_key, blob = connection.execute(
            "SELECT evidence_pk,embedding FROM evidence WHERE evidence_id='evidence_001'"
        ).fetchone()
        assert primary_key == 1
        vector = struct.unpack("<1536f", blob)
        assert math.isclose(sum(value * value for value in vector), 1.0, rel_tol=2e-5)
        hits = connection.execute(
            """SELECT e.evidence_id FROM evidence_fts f
               JOIN evidence e ON e.evidence_pk=f.rowid
               WHERE evidence_fts MATCH '전월'"""
        ).fetchall()
        assert hits == [("evidence_001",)]
    finally:
        connection.close()


def test_exporter_binds_explicit_unsupported_product_audit_payload(tmp_path: Path) -> None:
    issuer, document, evidence = fixture_rows()
    source = SourceRecord(
        issuer="kb",
        product_code="protected-1",
        product_name="보호 문서 카드",
        effective_date=date(2026, 2, 4),
        source_version="20260204",
        source_url="https://kb.example/protected.pdf",
        source_post_id="post-1",
        file_name="protected.pdf",
        category="credit",
        discovered_at=datetime.now(UTC),
    )
    unsupported = UnsupportedProductRecord(
        source=source,
        protected_sha256="d" * 64,
        protected_size_bytes=545_086,
        protected_magic="FASOO_DRMONE",
    )
    target = tmp_path / "index.sqlite3"
    result = ServingDatabaseExporter().export(
        target,
        generation_id="g-test",
        corpus_sha256="b" * 64,
        contract_sha256="c" * 64,
        embedding_provider="openrouter",
        embedding_model="openai/text-embedding-3-small",
        issuers=[issuer],
        documents=[document],
        evidence=[evidence],
        unsupported_products=[unsupported],
    )
    assert result.unsupported_product_count == 1
    connection = sqlite3.connect(f"{target.as_uri()}?mode=ro&immutable=1", uri=True)
    try:
        metadata = dict(connection.execute("SELECT key,value FROM metadata"))
        assert metadata["unsupported_document_count"] == "1"
        assert metadata["unsupported_documents_sha256"] == canonical_sha256(
            {
                "schema_version": "cardrag.unsupported-documents.v1",
                "documents": [unsupported.payload],
            }
        )
        row = connection.execute(
            """SELECT issuer,product_code,disposition,source_id,protected_magic,protected_sha256,
                      protected_size_bytes,source_payload_json
               FROM unsupported_products"""
        ).fetchone()
        assert row == (
            "kb",
            "protected-1",
            "unsupported_drm",
            source.source_id,
            "FASOO_DRMONE",
            "d" * 64,
            545_086,
            unsupported.source_payload_json,
        )
    finally:
        connection.close()


def test_exporter_rejects_product_served_and_unsupported_at_once(tmp_path: Path) -> None:
    issuer, document, evidence = fixture_rows()
    source = SourceRecord(
        issuer="kb",
        product_code=document.product_code,
        product_name=document.product_name,
        effective_date=date(2026, 2, 4),
        source_version="20260204",
        source_url="https://kb.example/protected.pdf",
        source_post_id="post-1",
        file_name="protected.pdf",
        category="credit",
        discovered_at=datetime.now(UTC),
    )
    unsupported = UnsupportedProductRecord(
        source=source,
        protected_sha256="d" * 64,
        protected_size_bytes=1,
        protected_magic="SCDSA002",
    )
    with pytest.raises(ServingDatabaseError, match="both served and unsupported"):
        ServingDatabaseExporter().export(
            tmp_path / "bad.sqlite3",
            generation_id="g-test",
            corpus_sha256="b" * 64,
            contract_sha256="c" * 64,
            embedding_provider="openrouter",
            embedding_model="model",
            issuers=[issuer],
            documents=[document],
            evidence=[evidence],
            unsupported_products=[unsupported],
        )


@pytest.mark.parametrize(
    "mutation,match",
    [
        ({"text": "변조된 텍스트"}, "exact source span"),
        ({"source_end": 10_000}, "exceeds"),
        ({"page_start": 2, "page_end": 2}, "missing page"),
        ({"page_end": 2}, "page-local"),
    ],
)
def test_exporter_rejects_tampered_evidence_spans(
    tmp_path: Path, mutation: dict[str, object], match: str
) -> None:
    issuer, document, evidence = fixture_rows()
    with pytest.raises(ServingDatabaseError, match=match):
        ServingDatabaseExporter().export(
            tmp_path / "bad.sqlite3",
            generation_id="g-test",
            corpus_sha256="b" * 64,
            contract_sha256="c" * 64,
            embedding_provider="openrouter",
            embedding_model="model",
            issuers=[issuer],
            documents=[document],
            evidence=[replace(evidence, **mutation)],
        )


def test_exporter_rejects_zero_and_nonfinite_embeddings(tmp_path: Path) -> None:
    issuer, document, evidence = fixture_rows()
    for vector in ([0.0] * 1536, [float("nan")] + [0.0] * 1535):
        with pytest.raises(ServingDatabaseError):
            ServingDatabaseExporter().export(
                tmp_path / ("bad-" + str(len(list(tmp_path.iterdir()))) + ".sqlite3"),
                generation_id="g-test",
                corpus_sha256="b" * 64,
                contract_sha256="c" * 64,
                embedding_provider="openrouter",
                embedding_model="model",
                issuers=[issuer],
                documents=[document],
                evidence=[replace(evidence, embedding=vector)],
            )
