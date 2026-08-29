from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytest
from cardrag_core import DOCUMENT_EMBEDDING_PREFIX, QUERY_EMBEDDING_PREFIX

from cardrag_mcp.embeddings import OpenRouterEmbedder
from cardrag_mcp.repository import ServingRepository
from cardrag_mcp.store import GenerationStore, cas_path, load_generation_handle

DDL = """
PRAGMA foreign_keys=ON;
CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL) STRICT, WITHOUT ROWID;
CREATE TABLE issuers (
  code TEXT PRIMARY KEY, display_name TEXT NOT NULL, sort_order INTEGER NOT NULL
) STRICT, WITHOUT ROWID;
CREATE TABLE documents (
  document_id TEXT PRIMARY KEY,
  issuer TEXT NOT NULL REFERENCES issuers(code),
  product_code TEXT NOT NULL,
  title TEXT NOT NULL,
  pdf_sha256 TEXT NOT NULL,
  pdf_size_bytes INTEGER NOT NULL,
  page_count INTEGER NOT NULL
) STRICT;
CREATE TABLE products (
  issuer TEXT NOT NULL,
  product_code TEXT NOT NULL,
  name TEXT NOT NULL,
  document_id TEXT NOT NULL REFERENCES documents(document_id),
  PRIMARY KEY (issuer, product_code),
  FOREIGN KEY (issuer) REFERENCES issuers(code)
) STRICT, WITHOUT ROWID;
CREATE TABLE unsupported_products (
  issuer TEXT NOT NULL,
  product_code TEXT NOT NULL,
  name TEXT NOT NULL,
  disposition TEXT NOT NULL CHECK(disposition='unsupported_drm'),
  source_id TEXT NOT NULL,
  source_version TEXT NOT NULL,
  source_url TEXT NOT NULL,
  protected_magic TEXT NOT NULL
    CHECK(protected_magic IN ('SCDSA002','SCDSA004','FASOO_DRMONE')),
  protected_sha256 TEXT NOT NULL CHECK(length(protected_sha256)=64),
  protected_size_bytes INTEGER NOT NULL CHECK(protected_size_bytes > 0),
  source_payload_json TEXT NOT NULL,
  PRIMARY KEY (issuer, product_code),
  FOREIGN KEY (issuer) REFERENCES issuers(code)
) STRICT, WITHOUT ROWID;
CREATE TABLE pages (
  document_id TEXT NOT NULL REFERENCES documents(document_id),
  page INTEGER NOT NULL,
  text TEXT NOT NULL,
  text_sha256 TEXT NOT NULL,
  PRIMARY KEY (document_id, page)
) STRICT, WITHOUT ROWID;
CREATE TABLE evidence (
  evidence_pk INTEGER PRIMARY KEY,
  evidence_id TEXT NOT NULL UNIQUE,
  document_id TEXT NOT NULL REFERENCES documents(document_id),
  page_start INTEGER NOT NULL,
  page_end INTEGER NOT NULL,
  section_type TEXT NOT NULL,
  text TEXT NOT NULL,
  source_start INTEGER NOT NULL,
  source_end INTEGER NOT NULL,
  embedding BLOB NOT NULL
) STRICT;
CREATE VIRTUAL TABLE evidence_fts USING fts5(
  evidence_id UNINDEXED,
  section_type,
  text,
  content='evidence',
  content_rowid='evidence_pk',
  tokenize='unicode61 remove_diacritics 2'
);
"""

OCR_FAILED_DDL = """
CREATE TABLE ocr_failed_products (
  issuer TEXT NOT NULL,
  product_code TEXT NOT NULL,
  name TEXT NOT NULL,
  document_id TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL,
  pdf_sha256 TEXT NOT NULL CHECK(length(pdf_sha256)=64),
  pdf_size_bytes INTEGER NOT NULL CHECK(pdf_size_bytes > 0),
  page_count INTEGER NOT NULL CHECK(page_count > 0),
  reason_code TEXT NOT NULL CHECK(length(reason_code) BETWEEN 1 AND 64),
  reason TEXT NOT NULL CHECK(length(reason) BETWEEN 1 AND 256),
  attempts INTEGER NOT NULL CHECK(attempts > 0),
  PRIMARY KEY (issuer, product_code),
  FOREIGN KEY (issuer) REFERENCES issuers(code)
) STRICT, WITHOUT ROWID;
"""


class FakeEmbedder(OpenRouterEmbedder):
    def __init__(self, vector: np.ndarray | None = None, *, fail: bool = False) -> None:
        self.vector = vector if vector is not None else unit_vector(0)
        self.fail = fail
        self.calls: list[tuple[str, str, str]] = []

    async def embed(self, query: str, *, provider: str, model: str) -> np.ndarray:
        from cardrag_mcp.embeddings import EmbeddingUnavailable

        self.calls.append((query, provider, model))
        if self.fail:
            raise EmbeddingUnavailable("injected failure")
        return self.vector.copy()

    async def close(self) -> None:
        return None


def unit_vector(index: int) -> np.ndarray:
    value = np.zeros(1536, dtype=np.float32)
    value[index] = 1.0
    return value


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


@dataclass(frozen=True)
class GenerationFixture:
    generation_id: str
    serving_schema: str
    corpus_sha256: str
    contract_sha256: str
    database: Path
    documents: tuple[tuple[str, str, int, bytes], ...]
    document_contracts: tuple[tuple[str, str, int], ...]
    issuer_codes: tuple[str, ...]


def create_database(
    target: Path,
    generation_id: str,
    *,
    suffix: str = "",
    two_documents: bool = True,
    schema_id: str = "cardrag.serving-db.v3",
    protected_magic: str = "SCDSA002",
) -> GenerationFixture:
    target.parent.mkdir(parents=True, exist_ok=True)
    pdf_one = b"%PDF-1.4\n" + f"first-{generation_id}".encode()
    pdf_two = b"%PDF-1.4\n" + f"second-{generation_id}".encode()
    sha_one = hashlib.sha256(pdf_one).hexdigest()
    sha_two = hashlib.sha256(pdf_two).hexdigest()
    doc_one = f"doc-a{suffix}"
    doc_two = f"doc-b{suffix}"
    documents = [
        (doc_one, "woori", "P1", "First Card", sha_one, len(pdf_one), pdf_one),
    ]
    if two_documents:
        documents.append((doc_two, "kb", "P2", "Second Card", sha_two, len(pdf_two), pdf_two))
    evidence = [
        (f"ev-a{suffix}", doc_one, "benefit", "airport lounge benefit", unit_vector(0)),
        (f"ev-b{suffix}", doc_one, "condition", "airport lounge condition", unit_vector(1)),
    ]
    if two_documents:
        evidence.append((f"ev-c{suffix}", doc_two, "benefit", "mileage reward", unit_vector(2)))
    page_text_by_document = {
        doc_one: "airport lounge benefit\nairport lounge condition",
        doc_two: "mileage reward",
    }
    protected_prefixes = {
        "SCDSA002": b"SCDSA002",
        "SCDSA004": b"SCDSA004",
        "FASOO_DRMONE": b"\x9b DRMONE",
    }
    protected_bytes = protected_prefixes[protected_magic] + b"fixture-protected-source"
    protected_sha256 = hashlib.sha256(protected_bytes).hexdigest()
    unsupported_source = {
        "category": "credit",
        "document_type": "product_description",
        "effective_date": "2026-08-26",
        "file_name": "protected.pdf",
        "issuer": "woori",
        "metadata": {"fixture": True},
        "product_code": "P-DRM",
        "product_name": "Protected Card",
        "source_post_id": "post-drm",
        "source_url": "https://example.com/protected.pdf",
        "source_version": "20260826",
    }
    unsupported_source_bytes = canonical_json_bytes(unsupported_source)
    unsupported_source_id = "source_" + hashlib.sha256(unsupported_source_bytes).hexdigest()
    unsupported_payload = {
        "disposition": "unsupported_drm",
        "protected_magic": protected_magic,
        "protected_sha256": protected_sha256,
        "protected_size_bytes": len(protected_bytes),
        "source": unsupported_source,
        "source_id": unsupported_source_id,
    }
    unsupported_sha256 = hashlib.sha256(
        canonical_json_bytes(
            {
                "documents": [unsupported_payload],
                "schema_version": "cardrag.unsupported-documents.v1",
            }
        )
    ).hexdigest()
    corpus_sha256 = hashlib.sha256(generation_id.encode()).hexdigest()
    contract_sha256 = hashlib.sha256(f"contract:{generation_id}".encode()).hexdigest()
    metadata = {
        "schema_id": schema_id,
        "generation_id": generation_id,
        "corpus_sha256": corpus_sha256,
        "contract_sha256": contract_sha256,
        "embedding_provider": "openrouter",
        "embedding_model": "openai/text-embedding-3-small",
        "embedding_input_policy_version": "cardrag.embedding-input.v1",
        "embedding_document_prefix": DOCUMENT_EMBEDDING_PREFIX,
        "embedding_query_prefix": QUERY_EMBEDDING_PREFIX,
        "embedding_dimension": "1536",
        "embedding_count": str(len(evidence)),
        "unsupported_document_count": "1",
        "unsupported_documents_sha256": unsupported_sha256,
    }
    if schema_id == "cardrag.serving-db.v4":
        metadata.update(
            {
                "ocr_failed_document_count": "0",
                "ocr_failed_documents_sha256": hashlib.sha256(
                    canonical_json_bytes(
                        {
                            "documents": [],
                            "schema_version": "cardrag.ocr-failed-products.v1",
                        }
                    )
                ).hexdigest(),
            }
        )
    connection = sqlite3.connect(target)
    try:
        connection.executescript(DDL)
        if schema_id == "cardrag.serving-db.v4":
            connection.executescript(OCR_FAILED_DDL)
        connection.executemany(
            "INSERT INTO metadata(key,value) VALUES(?,?)", sorted(metadata.items())
        )
        connection.executemany(
            "INSERT INTO issuers(code,display_name,sort_order) VALUES(?,?,?)",
            (("woori", "우리카드", 1), ("kb", "KB국민카드", 2))
            if two_documents
            else (("woori", "우리카드", 1),),
        )
        for document_id, issuer, product_code, title, pdf_sha, size, _ in documents:
            connection.execute(
                "INSERT INTO documents VALUES(?,?,?,?,?,?,?)",
                (document_id, issuer, product_code, title, pdf_sha, size, 1),
            )
            connection.execute(
                "INSERT INTO products VALUES(?,?,?,?)",
                (issuer, product_code, title, document_id),
            )
            page_text = page_text_by_document[document_id]
            connection.execute(
                "INSERT INTO pages VALUES(?,?,?,?)",
                (document_id, 1, page_text, hashlib.sha256(page_text.encode()).hexdigest()),
            )
        connection.execute(
            "INSERT INTO unsupported_products VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                "woori",
                "P-DRM",
                "Protected Card",
                "unsupported_drm",
                unsupported_source_id,
                "20260826",
                "https://example.com/protected.pdf",
                protected_magic,
                protected_sha256,
                len(protected_bytes),
                unsupported_source_bytes.decode("utf-8"),
            ),
        )
        for evidence_id, document_id, section, text, vector in evidence:
            source_start = page_text_by_document[document_id].index(text)
            connection.execute(
                """INSERT INTO evidence
                   (evidence_id,document_id,page_start,page_end,section_type,text,
                    source_start,source_end,embedding)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    evidence_id,
                    document_id,
                    1,
                    1,
                    section,
                    text,
                    source_start,
                    source_start + len(text),
                    vector.tobytes(),
                ),
            )
        connection.execute("INSERT INTO evidence_fts(evidence_fts) VALUES('rebuild')")
        connection.commit()
    finally:
        connection.close()
    return GenerationFixture(
        generation_id=generation_id,
        serving_schema=schema_id,
        corpus_sha256=corpus_sha256,
        contract_sha256=contract_sha256,
        database=target,
        documents=tuple((row[0], row[4], row[5], row[6]) for row in documents),
        document_contracts=tuple((row[0], row[1], 1) for row in documents),
        issuer_codes=tuple(sorted({row[1] for row in documents})),
    )


def install_generation(
    store: GenerationStore,
    generation_id: str,
    *,
    suffix: str = "",
    two_documents: bool = True,
    activate: bool = True,
) -> GenerationFixture:
    directory = store.generations / generation_id
    fixture = create_database(
        directory / "index.sqlite3",
        generation_id,
        suffix=suffix,
        two_documents=two_documents,
    )
    for _, digest, _, body in fixture.documents:
        target = cas_path(store.objects, digest)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
    handle = load_generation_handle(
        directory,
        store.objects,
        maximum_vector_bytes=store.maximum_vector_bytes,
        expected_generation_id=generation_id,
    )
    store.verify_handle_pdfs(handle)
    if activate:
        store.activate(handle)
    return fixture


@pytest.fixture
def active_runtime(tmp_path: Path):
    store = GenerationStore(
        tmp_path / "state",
        maximum_vector_bytes=1024 * 1024,
        retention=3,
    )
    fixture = install_generation(store, "gen-001")
    embedder = FakeEmbedder(unit_vector(0))
    repository = ServingRepository(
        store,
        embedder,
        cursor_secret=b"test-cursor-secret-value",
        maximum_candidates=20,
    )
    return store, repository, embedder, fixture
