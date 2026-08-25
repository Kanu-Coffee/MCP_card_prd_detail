"""Deterministic exporter for the only artifact the MCP service consumes."""

from __future__ import annotations

import hashlib
import math
import os
import re
import sqlite3
import struct
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from cardrag_core import (
    DOCUMENT_EMBEDDING_PREFIX,
    EMBEDDING_DIMENSION,
    EMBEDDING_POLICY_VERSION,
    QUERY_EMBEDDING_PREFIX,
)

from .contracts import (
    SERVING_SCHEMA_ID,
    DocumentRecord,
    EvidenceRecord,
    IssuerSpec,
    PageRecord,
)


class ServingDatabaseError(RuntimeError):
    pass


DDL = """
PRAGMA page_size=4096;
PRAGMA auto_vacuum=NONE;
PRAGMA foreign_keys=ON;

CREATE TABLE metadata (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
) STRICT, WITHOUT ROWID;

CREATE TABLE issuers (
  code TEXT PRIMARY KEY,
  display_name TEXT NOT NULL,
  sort_order INTEGER NOT NULL
) STRICT, WITHOUT ROWID;

CREATE TABLE documents (
  document_id TEXT PRIMARY KEY,
  issuer TEXT NOT NULL REFERENCES issuers(code),
  product_code TEXT NOT NULL,
  title TEXT NOT NULL,
  pdf_sha256 TEXT NOT NULL CHECK(length(pdf_sha256)=64),
  pdf_size_bytes INTEGER NOT NULL CHECK(pdf_size_bytes > 0),
  page_count INTEGER NOT NULL CHECK(page_count > 0),
  UNIQUE(issuer, product_code, document_id)
) STRICT;

CREATE TABLE products (
  issuer TEXT NOT NULL,
  product_code TEXT NOT NULL,
  name TEXT NOT NULL,
  document_id TEXT NOT NULL REFERENCES documents(document_id),
  PRIMARY KEY (issuer, product_code),
  FOREIGN KEY (issuer) REFERENCES issuers(code)
) STRICT, WITHOUT ROWID;

CREATE TABLE pages (
  document_id TEXT NOT NULL REFERENCES documents(document_id),
  page INTEGER NOT NULL CHECK(page > 0),
  text TEXT NOT NULL,
  text_sha256 TEXT NOT NULL CHECK(length(text_sha256)=64),
  PRIMARY KEY (document_id, page)
) STRICT, WITHOUT ROWID;

CREATE TABLE evidence (
  evidence_pk INTEGER PRIMARY KEY,
  evidence_id TEXT NOT NULL UNIQUE,
  document_id TEXT NOT NULL REFERENCES documents(document_id),
  page_start INTEGER NOT NULL CHECK(page_start > 0),
  page_end INTEGER NOT NULL CHECK(page_end >= page_start),
  section_type TEXT NOT NULL,
  text TEXT NOT NULL,
  source_start INTEGER NOT NULL CHECK(source_start >= 0),
  source_end INTEGER NOT NULL CHECK(source_end > source_start),
  embedding BLOB NOT NULL CHECK(length(embedding)=6144)
) STRICT;
CREATE INDEX evidence_document_idx ON evidence(document_id, page_start, evidence_id);

CREATE VIRTUAL TABLE evidence_fts USING fts5(
  evidence_id UNINDEXED,
  section_type,
  text,
  content='evidence',
  content_rowid='evidence_pk',
  tokenize='unicode61 remove_diacritics 2'
);
"""


@dataclass(frozen=True, slots=True)
class ServingExport:
    path: Path
    sha256: str
    size_bytes: int
    issuer_count: int
    document_count: int
    page_count: int
    evidence_count: int


def encode_embedding(values: Sequence[float]) -> bytes:
    if len(values) != EMBEDDING_DIMENSION:
        raise ServingDatabaseError(f"embedding dimension {len(values)} does not equal {EMBEDDING_DIMENSION}")
    raw = tuple(float(value) for value in values)
    if not all(math.isfinite(value) for value in raw):
        raise ServingDatabaseError("embedding contains a non-finite value")
    norm = math.sqrt(sum(value * value for value in raw))
    if not math.isfinite(norm) or norm <= 1e-12:
        raise ServingDatabaseError("embedding must be a non-zero finite vector")
    normalized = tuple(value / norm for value in raw)
    return struct.pack(f"<{EMBEDDING_DIMENSION}f", *normalized)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _validate_inputs(
    issuers: Sequence[IssuerSpec],
    documents: Sequence[DocumentRecord],
    evidence: Sequence[EvidenceRecord],
) -> None:
    issuer_codes = [row.code for row in issuers]
    if len(set(issuer_codes)) != len(issuer_codes):
        raise ServingDatabaseError("duplicate issuer code")
    document_ids = [row.document_id for row in documents]
    if len(set(document_ids)) != len(document_ids):
        raise ServingDatabaseError("duplicate document_id")
    products = [(row.issuer, row.product_code) for row in documents]
    if len(set(products)) != len(products):
        raise ServingDatabaseError("latest corpus contains multiple documents for one product")
    evidence_ids = [row.evidence_id for row in evidence]
    if len(set(evidence_ids)) != len(evidence_ids):
        raise ServingDatabaseError("duplicate evidence_id")
    known = set(document_ids)
    pages_by_identity: dict[tuple[str, int], PageRecord] = {}
    for document in documents:
        if document.issuer not in issuer_codes or len(document.pages) != document.page_count:
            raise ServingDatabaseError(f"document {document.document_id} has invalid issuer/page coverage")
        if tuple(page.page for page in document.pages) != tuple(range(1, document.page_count + 1)):
            raise ServingDatabaseError(f"document {document.document_id} pages are not contiguous")
        for page in document.pages:
            if page.document_id != document.document_id:
                raise ServingDatabaseError(
                    f"page {page.page} is bound to {page.document_id}, not {document.document_id}"
                )
            pages_by_identity[(document.document_id, page.page)] = page
    for item in evidence:
        if item.document_id not in known or not item.text:
            raise ServingDatabaseError(f"evidence {item.evidence_id} references an invalid document/text")
        if item.page_start != item.page_end:
            raise ServingDatabaseError(
                f"evidence {item.evidence_id} spans pages but the current chunk contract is page-local"
            )
        source_page = pages_by_identity.get((item.document_id, item.page_start))
        if source_page is None:
            raise ServingDatabaseError(f"evidence {item.evidence_id} references a missing page")
        if item.source_end > len(source_page.text):
            raise ServingDatabaseError(f"evidence {item.evidence_id} source span exceeds its page")
        if source_page.text[item.source_start : item.source_end] != item.text:
            raise ServingDatabaseError(
                f"evidence {item.evidence_id} text does not match its exact source span"
            )


def _verify_database(
    connection: sqlite3.Connection,
    *,
    issuer_count: int,
    document_count: int,
    page_count: int,
    evidence_count: int,
) -> None:
    integrity = connection.execute("PRAGMA integrity_check").fetchone()
    if integrity is None or integrity[0] != "ok":
        raise ServingDatabaseError(f"SQLite integrity check failed: {integrity}")
    foreign = connection.execute("PRAGMA foreign_key_check").fetchall()
    if foreign:
        raise ServingDatabaseError(f"SQLite foreign key check failed: {foreign[:3]}")
    expected = (
        ("issuers", "SELECT count(*) FROM issuers", issuer_count),
        ("documents", "SELECT count(*) FROM documents", document_count),
        ("products", "SELECT count(*) FROM products", document_count),
        ("pages", "SELECT count(*) FROM pages", page_count),
        ("evidence", "SELECT count(*) FROM evidence", evidence_count),
        ("evidence_fts", "SELECT count(*) FROM evidence_fts", evidence_count),
    )
    for table, query, count in expected:
        actual = int(connection.execute(query).fetchone()[0])
        if actual != count:
            raise ServingDatabaseError(f"{table} row count {actual} != {count}")
    invalid_vectors = int(
        connection.execute(
            "SELECT count(*) FROM evidence WHERE typeof(embedding)!='blob' OR length(embedding)!=?",
            (EMBEDDING_DIMENSION * 4,),
        ).fetchone()[0]
    )
    if invalid_vectors:
        raise ServingDatabaseError("serving database contains an invalid embedding blob")
    for document_id, page, page_text, declared_sha256 in connection.execute(
        "SELECT document_id,page,text,text_sha256 FROM pages ORDER BY document_id,page"
    ):
        actual_sha256 = hashlib.sha256(str(page_text).encode("utf-8")).hexdigest()
        if actual_sha256 != declared_sha256:
            raise ServingDatabaseError(f"stored page hash mismatch for {document_id}/{page}")
    previous_id: str | None = None
    previous_pk = 0
    for evidence_pk, evidence_id, blob in connection.execute(
        "SELECT evidence_pk,evidence_id,embedding FROM evidence ORDER BY evidence_pk"
    ):
        if int(evidence_pk) != previous_pk + 1:
            raise ServingDatabaseError("evidence primary keys are not contiguous and deterministic")
        previous_pk = int(evidence_pk)
        if previous_id is not None and str(evidence_id) <= previous_id:
            raise ServingDatabaseError("evidence rows are not strictly ordered by evidence_id")
        previous_id = str(evidence_id)
        vector = struct.unpack(f"<{EMBEDDING_DIMENSION}f", bytes(blob))
        norm_squared = sum(value * value for value in vector)
        if not math.isclose(norm_squared, 1.0, rel_tol=2e-5, abs_tol=2e-5):
            raise ServingDatabaseError("stored float32 embedding is not normalized")
        # Decode + dot(self,self) exercises the exact retrieval representation.
        cosine_self = sum(left * right for left, right in zip(vector, vector, strict=True))
        if not math.isclose(cosine_self, 1.0, rel_tol=2e-5, abs_tol=2e-5):
            raise ServingDatabaseError("embedding self-cosine smoke test failed")
    if evidence_count:
        sample_id, sample = connection.execute(
            "SELECT evidence_id,text FROM evidence ORDER BY evidence_id LIMIT 1"
        ).fetchone()
        sample = str(sample)
        match = re.search(r"[0-9A-Za-z가-힣]{2,}", sample)
        if match:
            hits = {
                str(row[0])
                for row in connection.execute(
                    """SELECT e.evidence_id FROM evidence_fts f
                       JOIN evidence e ON e.evidence_pk=f.rowid
                       WHERE evidence_fts MATCH ?""",
                    ('"' + match.group(0).replace('"', '""') + '"',),
                )
            }
            if str(sample_id) not in hits:
                raise ServingDatabaseError("FTS5 smoke query did not find its source evidence")


class ServingDatabaseExporter:
    def export(
        self,
        target: Path,
        *,
        generation_id: str,
        corpus_sha256: str,
        embedding_provider: str,
        embedding_model: str,
        issuers: Sequence[IssuerSpec],
        documents: Sequence[DocumentRecord],
        evidence: Sequence[EvidenceRecord],
        extra_metadata: Mapping[str, str] | None = None,
    ) -> ServingExport:
        _validate_inputs(issuers, documents, evidence)
        target.parent.mkdir(parents=True, exist_ok=True)
        token = uuid.uuid4().hex
        working = target.parent / f".{target.name}.{token}.build"
        vacuumed = target.parent / f".{target.name}.{token}.vacuum"
        for candidate in (working, vacuumed):
            if candidate.exists():
                candidate.unlink()
        ordered_issuers = tuple(sorted(issuers, key=lambda row: (row.sort_order, row.code)))
        ordered_documents = tuple(sorted(documents, key=lambda row: row.document_id))
        ordered_evidence = tuple(sorted(evidence, key=lambda row: row.evidence_id))
        page_count = sum(row.page_count for row in ordered_documents)
        metadata = {
            "schema_id": SERVING_SCHEMA_ID,
            "generation_id": generation_id,
            "corpus_sha256": corpus_sha256,
            "embedding_provider": embedding_provider,
            "embedding_model": embedding_model,
            "embedding_dimension": str(EMBEDDING_DIMENSION),
            "embedding_count": str(len(ordered_evidence)),
            "embedding_input_policy_version": EMBEDDING_POLICY_VERSION,
            "embedding_document_prefix": DOCUMENT_EMBEDDING_PREFIX,
            "embedding_query_prefix": QUERY_EMBEDDING_PREFIX,
        }
        if extra_metadata:
            overlap = set(metadata).intersection(extra_metadata)
            if overlap:
                raise ServingDatabaseError(
                    "reserved metadata keys cannot be overridden: " + ",".join(sorted(overlap))
                )
            metadata.update(extra_metadata)
        connection = sqlite3.connect(working)
        try:
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("PRAGMA synchronous=FULL")
            connection.executescript(DDL)
            connection.executemany("INSERT INTO metadata(key,value) VALUES(?,?)", sorted(metadata.items()))
            connection.executemany(
                "INSERT INTO issuers(code,display_name,sort_order) VALUES(?,?,?)",
                ((row.code, row.display_name, row.sort_order) for row in ordered_issuers),
            )
            connection.executemany(
                """INSERT INTO documents
                   (document_id,issuer,product_code,title,pdf_sha256,pdf_size_bytes,page_count)
                   VALUES(?,?,?,?,?,?,?)""",
                (
                    (
                        row.document_id,
                        row.issuer,
                        row.product_code,
                        row.title,
                        row.pdf_sha256,
                        row.pdf_size_bytes,
                        row.page_count,
                    )
                    for row in ordered_documents
                ),
            )
            connection.executemany(
                "INSERT INTO products(issuer,product_code,name,document_id) VALUES(?,?,?,?)",
                (
                    (row.issuer, row.product_code, row.product_name, row.document_id)
                    for row in ordered_documents
                ),
            )
            connection.executemany(
                "INSERT INTO pages(document_id,page,text,text_sha256) VALUES(?,?,?,?)",
                (
                    (page.document_id, page.page, page.text, page.text_sha256)
                    for document in ordered_documents
                    for page in document.pages
                ),
            )
            connection.executemany(
                """INSERT INTO evidence
                   (evidence_id,document_id,page_start,page_end,section_type,text,source_start,source_end,embedding)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    (
                        row.evidence_id,
                        row.document_id,
                        row.page_start,
                        row.page_end,
                        row.section_type,
                        row.text,
                        row.source_start,
                        row.source_end,
                        encode_embedding(row.embedding),
                    )
                    for row in ordered_evidence
                ),
            )
            connection.execute("INSERT INTO evidence_fts(evidence_fts) VALUES('rebuild')")
            connection.commit()
            connection.execute("INSERT INTO evidence_fts(evidence_fts) VALUES('integrity-check')")
            connection.commit()
            _verify_database(
                connection,
                issuer_count=len(ordered_issuers),
                document_count=len(ordered_documents),
                page_count=page_count,
                evidence_count=len(ordered_evidence),
            )
            connection.execute("VACUUM INTO ?", (str(vacuumed),))
        finally:
            connection.close()
        verify = sqlite3.connect(f"file:{vacuumed}?mode=ro&immutable=1", uri=True)
        try:
            _verify_database(
                verify,
                issuer_count=len(ordered_issuers),
                document_count=len(ordered_documents),
                page_count=page_count,
                evidence_count=len(ordered_evidence),
            )
        finally:
            verify.close()
        with vacuumed.open("rb") as stream:
            os.fsync(stream.fileno())
        vacuumed.replace(target)
        descriptor = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        working.unlink(missing_ok=True)
        sha256 = _file_sha256(target)
        return ServingExport(
            path=target,
            sha256=sha256,
            size_bytes=target.stat().st_size,
            issuer_count=len(ordered_issuers),
            document_count=len(ordered_documents),
            page_count=page_count,
            evidence_count=len(ordered_evidence),
        )
