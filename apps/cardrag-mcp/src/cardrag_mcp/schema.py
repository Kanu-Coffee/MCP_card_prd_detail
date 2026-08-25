"""Validation and immutable loading for ``cardrag.serving-db.v1``."""

from __future__ import annotations

import hashlib
import math
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from urllib.parse import quote

import numpy as np
from cardrag_core import (
    DOCUMENT_EMBEDDING_PREFIX,
    EMBEDDING_DIMENSION,
    EMBEDDING_POLICY_VERSION,
    QUERY_EMBEDDING_PREFIX,
)
from numpy.typing import NDArray

from cardrag_mcp.models import ServingMetadata

SCHEMA_ID = "cardrag.serving-db.v1"
FLOAT32_BYTES = 4
EMBEDDING_BYTES = EMBEDDING_DIMENSION * FLOAT32_BYTES

REQUIRED_COLUMNS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "metadata": ("key", "value"),
        "issuers": ("code", "display_name", "sort_order"),
        "products": ("issuer", "product_code", "name", "document_id"),
        "documents": (
            "document_id",
            "issuer",
            "product_code",
            "title",
            "pdf_sha256",
            "pdf_size_bytes",
            "page_count",
        ),
        "pages": ("document_id", "page", "text", "text_sha256"),
        "evidence": (
            "evidence_pk",
            "evidence_id",
            "document_id",
            "page_start",
            "page_end",
            "section_type",
            "text",
            "source_start",
            "source_end",
            "embedding",
        ),
    }
)


class ServingDatabaseError(RuntimeError):
    """The candidate database cannot safely become the active generation."""


def readonly_connection(path: Path) -> sqlite3.Connection:
    """Open one SQLite connection that cannot mutate or create sidecars."""

    resolved = path.resolve(strict=True)
    uri = f"file:{quote(resolved.as_posix(), safe='/')}?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _columns(connection: sqlite3.Connection, table: str) -> tuple[str, ...]:
    return tuple(str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")'))


def _integer_metadata(values: Mapping[str, str], key: str) -> int:
    try:
        value = int(values[key])
    except (KeyError, ValueError) as exc:
        raise ServingDatabaseError(f"metadata {key} is missing or invalid") from exc
    if value < 0:
        raise ServingDatabaseError(f"metadata {key} must not be negative")
    return value


def validate_schema(
    connection: sqlite3.Connection,
    *,
    maximum_vector_bytes: int | None = None,
) -> ServingMetadata:
    """Reject schema drift, corrupt references, and inconsistent metadata."""

    integrity_check = connection.execute("PRAGMA integrity_check").fetchone()
    if integrity_check is None or integrity_check[0] != "ok":
        raise ServingDatabaseError("SQLite integrity_check failed")
    foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchone()
    if foreign_key_errors is not None:
        raise ServingDatabaseError("SQLite foreign key check failed")

    for table, expected in REQUIRED_COLUMNS.items():
        actual = _columns(connection, table)
        if actual != expected:
            raise ServingDatabaseError(f"unexpected {table} schema: {actual!r}")

    fts_row = connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type='table' AND name='evidence_fts'"
    ).fetchone()
    if fts_row is None or not isinstance(fts_row[0], str):
        raise ServingDatabaseError("evidence_fts is missing")
    normalized_fts = "".join(fts_row[0].lower().split()).replace('"', "'")
    if (
        "usingfts5(" not in normalized_fts
        or "content='evidence'" not in normalized_fts
        or "content_rowid='evidence_pk'" not in normalized_fts
        or "tokenize='unicode61remove_diacritics2'" not in normalized_fts
    ):
        raise ServingDatabaseError("evidence_fts is not the required external-content FTS5 index")

    values = {
        str(row["key"]): str(row["value"])
        for row in connection.execute("SELECT key, value FROM metadata")
    }
    required_text = (
        "schema_id",
        "generation_id",
        "corpus_sha256",
        "embedding_provider",
        "embedding_model",
        "embedding_input_policy_version",
        "embedding_document_prefix",
        "embedding_query_prefix",
    )
    if any(not values.get(key) for key in required_text):
        raise ServingDatabaseError("required serving metadata is missing")
    if (
        values["embedding_input_policy_version"] != EMBEDDING_POLICY_VERSION
        or values["embedding_document_prefix"] != DOCUMENT_EMBEDDING_PREFIX
        or values["embedding_query_prefix"] != QUERY_EMBEDDING_PREFIX
    ):
        raise ServingDatabaseError("serving database embedding input policy is incompatible")
    dimension = _integer_metadata(values, "embedding_dimension")
    count = _integer_metadata(values, "embedding_count")
    expected_vector_bytes = count * EMBEDDING_BYTES
    if maximum_vector_bytes is not None and expected_vector_bytes > maximum_vector_bytes:
        raise ServingDatabaseError(
            f"embedding matrix requires {expected_vector_bytes} bytes; "
            f"promotion limit is {maximum_vector_bytes}"
        )
    try:
        metadata = ServingMetadata.model_validate(
            {
                "schema_id": values["schema_id"],
                "generation_id": values["generation_id"],
                "corpus_sha256": values["corpus_sha256"],
                "embedding_provider": values["embedding_provider"],
                "embedding_model": values["embedding_model"],
                "embedding_input_policy_version": values["embedding_input_policy_version"],
                "embedding_dimension": dimension,
                "embedding_count": count,
            }
        )
    except Exception as exc:
        raise ServingDatabaseError("serving metadata does not satisfy v1") from exc

    evidence_count = int(connection.execute("SELECT count(*) FROM evidence").fetchone()[0])
    fts_count = int(connection.execute("SELECT count(*) FROM evidence_fts").fetchone()[0])
    if evidence_count != count or fts_count != count:
        raise ServingDatabaseError("embedding/evidence/FTS counts differ")
    key_mismatch = connection.execute(
        """
        SELECT 1
        FROM evidence AS e
        LEFT JOIN evidence_fts AS f ON f.rowid=e.evidence_pk
        WHERE f.rowid IS NULL OR f.evidence_id != e.evidence_id
        LIMIT 1
        """
    ).fetchone()
    key_rows = connection.execute(
        "SELECT evidence_pk,evidence_id FROM evidence ORDER BY evidence_pk"
    )
    if key_mismatch is not None or any(
        int(row["evidence_pk"]) != expected for expected, row in enumerate(key_rows, start=1)
    ):
        raise ServingDatabaseError("evidence/FTS stable row identifiers differ")
    page_mismatch = connection.execute(
        """
        SELECT 1
        FROM documents AS d
        WHERE d.page_count != (SELECT count(*) FROM pages AS p WHERE p.document_id=d.document_id)
           OR 1 != (SELECT min(page) FROM pages AS p WHERE p.document_id=d.document_id)
           OR d.page_count != (SELECT max(page) FROM pages AS p WHERE p.document_id=d.document_id)
        LIMIT 1
        """
    ).fetchone()
    if page_mismatch is not None:
        raise ServingDatabaseError("document page_count differs from stored pages")
    for row in connection.execute(
        "SELECT document_id,page,text,text_sha256 FROM pages ORDER BY document_id,page"
    ):
        text = str(row["text"])
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if digest != str(row["text_sha256"]):
            raise ServingDatabaseError(
                f"page text hash differs from metadata for {row['document_id']}/{row['page']}"
            )
    bad_spans = connection.execute(
        """
        SELECT 1
        FROM evidence AS e
        JOIN documents AS d ON d.document_id=e.document_id
        WHERE e.page_start < 1 OR e.page_end != e.page_start
           OR e.page_end > d.page_count
           OR e.source_start < 0 OR e.source_end <= e.source_start
        LIMIT 1
        """
    ).fetchone()
    if bad_spans is not None:
        raise ServingDatabaseError("evidence contains an invalid source span")
    span_rows = connection.execute(
        """
        SELECT e.evidence_id,e.text,e.source_start,e.source_end,p.text AS page_text
        FROM evidence AS e
        LEFT JOIN pages AS p
          ON p.document_id=e.document_id AND p.page=e.page_start
        ORDER BY e.evidence_id
        """
    )
    span_count = 0
    for row in span_rows:
        span_count += 1
        start = int(row["source_start"])
        end = int(row["source_end"])
        if row["page_text"] is None:
            raise ServingDatabaseError(f"evidence page is missing for {row['evidence_id']}")
        page_text = str(row["page_text"])
        if end > len(page_text) or page_text[start:end] != str(row["text"]):
            raise ServingDatabaseError(
                f"evidence text differs from its exact page span for {row['evidence_id']}"
            )
    if span_count != evidence_count:
        raise ServingDatabaseError("evidence/page relation count differs")
    return metadata


@dataclass(frozen=True, slots=True)
class LoadedVectors:
    evidence_ids: tuple[str, ...]
    index_by_id: Mapping[str, int]
    matrix: NDArray[np.float32]
    norms: NDArray[np.float32]


def load_vectors(
    connection: sqlite3.Connection,
    *,
    expected_count: int,
    maximum_bytes: int,
) -> LoadedVectors:
    """Load one deterministic, exact-search matrix ordered by evidence ID."""

    required_bytes = expected_count * EMBEDDING_BYTES
    if required_bytes > maximum_bytes:
        raise ServingDatabaseError(
            f"embedding matrix requires {required_bytes} bytes; promotion limit is {maximum_bytes}"
        )
    matrix = np.empty((expected_count, EMBEDDING_DIMENSION), dtype=np.float32)
    evidence_ids: list[str] = []
    for index, row in enumerate(
        connection.execute("SELECT evidence_id, embedding FROM evidence ORDER BY evidence_id")
    ):
        if index >= expected_count:
            raise ServingDatabaseError("evidence count changed while loading vectors")
        evidence_id = str(row[0])
        blob = row[1]
        if not isinstance(blob, bytes) or len(blob) != EMBEDDING_BYTES:
            raise ServingDatabaseError(f"invalid embedding bytes for {evidence_id}")
        vector = np.frombuffer(blob, dtype="<f4", count=EMBEDDING_DIMENSION)
        if not bool(np.isfinite(vector).all()):
            raise ServingDatabaseError(f"non-finite embedding for {evidence_id}")
        matrix[index, :] = vector
        evidence_ids.append(evidence_id)
    if len(evidence_ids) != expected_count:
        raise ServingDatabaseError("evidence count changed while loading vectors")
    norms = np.linalg.norm(matrix, axis=1).astype(np.float32, copy=False)
    if any(not math.isfinite(float(value)) or value <= 0 for value in norms):
        raise ServingDatabaseError("zero or non-finite evidence embedding norm")
    if expected_count and not bool(np.allclose(norms, 1.0, rtol=2e-5, atol=2e-5)):
        raise ServingDatabaseError("evidence embeddings are not L2 normalized")
    ids = tuple(evidence_ids)
    return LoadedVectors(
        evidence_ids=ids,
        index_by_id=MappingProxyType({value: index for index, value in enumerate(ids)}),
        matrix=matrix,
        norms=norms,
    )
