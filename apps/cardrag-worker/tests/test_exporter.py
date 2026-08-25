from __future__ import annotations

import math
import sqlite3
import struct
from dataclasses import replace
from pathlib import Path

import pytest
from cardrag_core import (
    DOCUMENT_EMBEDDING_PREFIX,
    EMBEDDING_POLICY_VERSION,
    QUERY_EMBEDDING_PREFIX,
)

from cardrag_worker.contracts import DocumentRecord, EvidenceRecord, IssuerSpec, PageRecord
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
        embedding_provider="openrouter",
        embedding_model="openai/text-embedding-3-small",
        issuers=[issuer],
        documents=[document],
        evidence=[evidence],
        extra_metadata={"contract_sha256": "c" * 64},
    )
    assert result.path == target
    connection = sqlite3.connect(f"{target.as_uri()}?mode=ro&immutable=1", uri=True)
    try:
        metadata = dict(connection.execute("SELECT key,value FROM metadata"))
        assert metadata["schema_id"] == "cardrag.serving-db.v1"
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
                embedding_provider="openrouter",
                embedding_model="model",
                issuers=[issuer],
                documents=[document],
                evidence=[replace(evidence, embedding=vector)],
            )
