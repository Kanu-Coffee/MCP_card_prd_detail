"""Validation and immutable loading for supported serving database generations."""

from __future__ import annotations

import hashlib
import json
import math
import re
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

SCHEMA_ID = "cardrag.serving-db.v4"
SUPPORTED_SCHEMA_IDS = frozenset({"cardrag.serving-db.v2", "cardrag.serving-db.v3", SCHEMA_ID})
UNSUPPORTED_DOCUMENTS_SCHEMA = "cardrag.unsupported-documents.v1"
OCR_FAILED_DOCUMENTS_SCHEMA = "cardrag.ocr-failed-products.v1"
FLOAT32_BYTES = 4
EMBEDDING_BYTES = EMBEDDING_DIMENSION * FLOAT32_BYTES
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_ID = re.compile(r"^source_[0-9a-f]{64}$")
_DOCUMENT_ID = re.compile(r"^doc_[0-9a-f]{64}$")
_SOURCE_FIELDS = frozenset(
    {
        "category",
        "document_type",
        "effective_date",
        "file_name",
        "issuer",
        "metadata",
        "product_code",
        "product_name",
        "source_post_id",
        "source_url",
        "source_version",
    }
)

REQUIRED_COLUMNS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "metadata": ("key", "value"),
        "issuers": ("code", "display_name", "sort_order"),
        "products": ("issuer", "product_code", "name", "document_id"),
        "unsupported_products": (
            "issuer",
            "product_code",
            "name",
            "disposition",
            "source_id",
            "source_version",
            "source_url",
            "protected_magic",
            "protected_sha256",
            "protected_size_bytes",
            "source_payload_json",
        ),
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
OCR_FAILED_COLUMNS = (
    "issuer",
    "product_code",
    "name",
    "document_id",
    "title",
    "pdf_sha256",
    "pdf_size_bytes",
    "page_count",
    "reason_code",
    "reason",
    "attempts",
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


def _canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (RecursionError, TypeError, UnicodeError, ValueError) as exc:
        raise ServingDatabaseError(
            "unsupported product source payload is not canonical JSON"
        ) from exc


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _validate_unsupported_products(
    connection: sqlite3.Connection,
    values: Mapping[str, str],
    *,
    schema_id: str,
) -> tuple[int, str]:
    expected_count = _integer_metadata(values, "unsupported_document_count")
    actual_count = int(
        connection.execute("SELECT count(*) FROM unsupported_products").fetchone()[0]
    )
    if expected_count > 100 or actual_count > 100:
        raise ServingDatabaseError("unsupported product count exceeds the promotion limit")
    expected_sha256 = values.get("unsupported_documents_sha256", "")
    if _SHA256.fullmatch(expected_sha256) is None:
        raise ServingDatabaseError("metadata unsupported_documents_sha256 is missing or invalid")

    issuer_codes = {
        str(row[0]) for row in connection.execute("SELECT code FROM issuers ORDER BY code")
    }
    available_products = {
        (str(row[0]), str(row[1]))
        for row in connection.execute("SELECT issuer,product_code FROM products")
    }
    payloads: list[dict[str, object]] = []
    source_ids: set[str] = set()
    allowed_protected_magic = {"SCDSA002", "SCDSA004"}
    if schema_id in {"cardrag.serving-db.v3", "cardrag.serving-db.v4"}:
        allowed_protected_magic.add("FASOO_DRMONE")
    rows = connection.execute(
        """
        SELECT issuer,product_code,name,disposition,source_id,source_version,source_url,
               protected_magic,protected_sha256,protected_size_bytes,source_payload_json
        FROM unsupported_products
        ORDER BY issuer,product_code
        """
    )
    for row in rows:
        issuer = str(row["issuer"])
        product_code = str(row["product_code"])
        name = str(row["name"])
        disposition = str(row["disposition"])
        source_id = str(row["source_id"])
        source_version = str(row["source_version"])
        source_url = str(row["source_url"])
        protected_magic = str(row["protected_magic"])
        protected_sha256 = str(row["protected_sha256"])
        protected_size = row["protected_size_bytes"]
        source_payload_json = str(row["source_payload_json"])
        if (
            not issuer
            or len(issuer) > 512
            or not product_code
            or len(product_code) > 512
            or not name
            or len(name) > 1_000
            or disposition != "unsupported_drm"
            or _SOURCE_ID.fullmatch(source_id) is None
            or not source_version
            or len(source_version) > 512
            or not source_url.startswith("https://")
            or len(source_url) > 4_096
            or protected_magic not in allowed_protected_magic
            or _SHA256.fullmatch(protected_sha256) is None
            or isinstance(protected_size, bool)
            or not isinstance(protected_size, int)
            or protected_size < 1
        ):
            raise ServingDatabaseError("unsupported product contains an invalid bounded value")
        if issuer not in issuer_codes:
            raise ServingDatabaseError("unsupported product references an unknown issuer")
        if (issuer, product_code) in available_products:
            raise ServingDatabaseError("product cannot be both available and unsupported")
        if source_id in source_ids:
            raise ServingDatabaseError("unsupported products contain a duplicate source_id")
        source_ids.add(source_id)
        try:
            source_payload_bytes = source_payload_json.encode("utf-8")
        except UnicodeError as exc:
            raise ServingDatabaseError("unsupported product source payload is not UTF-8") from exc
        if len(source_payload_bytes) > 1024 * 1024:
            raise ServingDatabaseError("unsupported product source payload is too large")
        try:
            source = json.loads(source_payload_json, parse_constant=_reject_json_constant)
        except (json.JSONDecodeError, RecursionError, ValueError) as exc:
            raise ServingDatabaseError(
                "unsupported product source payload is invalid JSON"
            ) from exc
        if not isinstance(source, dict) or set(source) != _SOURCE_FIELDS:
            raise ServingDatabaseError("unsupported product source payload has unexpected fields")
        canonical_source = _canonical_json_bytes(source)
        if source_payload_bytes != canonical_source:
            raise ServingDatabaseError("unsupported product source payload is not canonical JSON")
        calculated_source_id = "source_" + hashlib.sha256(canonical_source).hexdigest()
        if calculated_source_id != source_id:
            raise ServingDatabaseError("unsupported product source_id does not bind its payload")
        string_fields = _SOURCE_FIELDS - {"metadata"}
        if any(not isinstance(source.get(key), str) for key in string_fields) or not isinstance(
            source.get("metadata"), dict
        ):
            raise ServingDatabaseError("unsupported product source payload has invalid field types")
        if (
            source["issuer"] != issuer
            or source["product_code"] != product_code
            or source["product_name"] != name
            or source["source_version"] != source_version
            or source["source_url"] != source_url
        ):
            raise ServingDatabaseError("unsupported product columns do not bind its source payload")
        payloads.append(
            {
                "disposition": disposition,
                "protected_magic": protected_magic,
                "protected_sha256": protected_sha256,
                "protected_size_bytes": protected_size,
                "source": source,
                "source_id": source_id,
            }
        )

    if len(payloads) != expected_count:
        raise ServingDatabaseError("unsupported product count differs from metadata")
    payloads.sort(key=_canonical_json_bytes)
    actual_sha256 = hashlib.sha256(
        _canonical_json_bytes(
            {
                "documents": payloads,
                "schema_version": UNSUPPORTED_DOCUMENTS_SCHEMA,
            }
        )
    ).hexdigest()
    if actual_sha256 != expected_sha256:
        raise ServingDatabaseError("unsupported product hash differs from metadata")
    return expected_count, expected_sha256


def _validate_ocr_failed_products(
    connection: sqlite3.Connection,
    values: Mapping[str, str],
    *,
    schema_id: str,
) -> tuple[int, str]:
    empty_sha256 = hashlib.sha256(
        _canonical_json_bytes({"documents": [], "schema_version": OCR_FAILED_DOCUMENTS_SCHEMA})
    ).hexdigest()
    table_exists = connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='ocr_failed_products'"
    ).fetchone()
    if schema_id != SCHEMA_ID:
        if table_exists is not None or any(
            key in values for key in ("ocr_failed_document_count", "ocr_failed_documents_sha256")
        ):
            raise ServingDatabaseError("legacy serving database contains v4 OCR failure fields")
        return 0, empty_sha256
    if table_exists is None or _columns(connection, "ocr_failed_products") != OCR_FAILED_COLUMNS:
        raise ServingDatabaseError("v4 OCR-failed product schema is missing or incompatible")

    expected_count = _integer_metadata(values, "ocr_failed_document_count")
    expected_sha256 = values.get("ocr_failed_documents_sha256", "")
    if _SHA256.fullmatch(expected_sha256) is None:
        raise ServingDatabaseError("metadata OCR-failed document hash is missing or invalid")
    issuer_codes = {
        str(row[0]) for row in connection.execute("SELECT code FROM issuers ORDER BY code")
    }
    available_products = {
        (str(row[0]), str(row[1]))
        for row in connection.execute("SELECT issuer,product_code FROM products")
    }
    unsupported_products = {
        (str(row[0]), str(row[1]))
        for row in connection.execute("SELECT issuer,product_code FROM unsupported_products")
    }
    available_document_ids = {
        str(row[0]) for row in connection.execute("SELECT document_id FROM documents")
    }
    payloads: list[dict[str, object]] = []
    failed_identities: set[tuple[str, str]] = set()
    failed_document_ids: set[str] = set()
    for row in connection.execute(
        """SELECT issuer,product_code,name,document_id,title,pdf_sha256,pdf_size_bytes,
                  page_count,reason_code,reason,attempts
             FROM ocr_failed_products ORDER BY issuer,product_code"""
    ):
        issuer = str(row["issuer"])
        product_code = str(row["product_code"])
        name = str(row["name"])
        document_id = str(row["document_id"])
        title = str(row["title"])
        pdf_sha256 = str(row["pdf_sha256"])
        pdf_size_bytes = row["pdf_size_bytes"]
        page_count = row["page_count"]
        reason_code = str(row["reason_code"])
        reason = str(row["reason"])
        attempts = row["attempts"]
        identity = (issuer, product_code)
        if (
            issuer not in issuer_codes
            or not product_code
            or len(product_code) > 512
            or not name
            or len(name) > 1_000
            or _DOCUMENT_ID.fullmatch(document_id) is None
            or not title
            or len(title) > 1_000
            or _SHA256.fullmatch(pdf_sha256) is None
            or isinstance(pdf_size_bytes, bool)
            or not isinstance(pdf_size_bytes, int)
            or pdf_size_bytes < 1
            or isinstance(page_count, bool)
            or not isinstance(page_count, int)
            or page_count < 1
            or re.fullmatch(r"[a-z0-9_]{1,64}", reason_code) is None
            or not reason
            or len(reason) > 256
            or "\n" in reason
            or "\r" in reason
            or isinstance(attempts, bool)
            or not isinstance(attempts, int)
            or attempts < 1
        ):
            raise ServingDatabaseError("OCR-failed product contains an invalid bounded value")
        if (
            identity in available_products
            or identity in unsupported_products
            or identity in failed_identities
            or document_id in available_document_ids
            or document_id in failed_document_ids
        ):
            raise ServingDatabaseError("OCR-failed product identity overlaps another disposition")
        failed_identities.add(identity)
        failed_document_ids.add(document_id)
        payloads.append(
            {
                "attempts": attempts,
                "document_id": document_id,
                "issuer": issuer,
                "page_count": page_count,
                "pdf_sha256": pdf_sha256,
                "pdf_size_bytes": pdf_size_bytes,
                "product_code": product_code,
                "product_name": name,
                "reason": reason,
                "reason_code": reason_code,
                "title": title,
            }
        )
    if len(payloads) != expected_count:
        raise ServingDatabaseError("OCR-failed product count differs from metadata")
    actual_sha256 = hashlib.sha256(
        _canonical_json_bytes(
            {"documents": payloads, "schema_version": OCR_FAILED_DOCUMENTS_SCHEMA}
        )
    ).hexdigest()
    if actual_sha256 != expected_sha256:
        raise ServingDatabaseError("OCR-failed product hash differs from metadata")

    for issuer in issuer_codes:
        succeeded = sum(identity[0] == issuer for identity in available_products)
        failed = sum(identity[0] == issuer for identity in failed_identities)
        if succeeded < 1 or succeeded * 100 < (succeeded + failed) * 95:
            raise ServingDatabaseError("issuer OCR success gate is not satisfied")
    return expected_count, expected_sha256


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
        "contract_sha256",
        "embedding_provider",
        "embedding_model",
        "embedding_input_policy_version",
        "embedding_document_prefix",
        "embedding_query_prefix",
    )
    if any(not values.get(key) for key in required_text):
        raise ServingDatabaseError("required serving metadata is missing")
    schema_id = values["schema_id"]
    if schema_id not in SUPPORTED_SCHEMA_IDS:
        raise ServingDatabaseError("serving database schema version is incompatible")
    if (
        values["embedding_input_policy_version"] != EMBEDDING_POLICY_VERSION
        or values["embedding_document_prefix"] != DOCUMENT_EMBEDDING_PREFIX
        or values["embedding_query_prefix"] != QUERY_EMBEDDING_PREFIX
    ):
        raise ServingDatabaseError("serving database embedding input policy is incompatible")
    dimension = _integer_metadata(values, "embedding_dimension")
    count = _integer_metadata(values, "embedding_count")
    unsupported_count, unsupported_sha256 = _validate_unsupported_products(
        connection,
        values,
        schema_id=schema_id,
    )
    ocr_failed_count, ocr_failed_sha256 = _validate_ocr_failed_products(
        connection,
        values,
        schema_id=schema_id,
    )
    expected_vector_bytes = count * EMBEDDING_BYTES
    if maximum_vector_bytes is not None and expected_vector_bytes > maximum_vector_bytes:
        raise ServingDatabaseError(
            f"embedding matrix requires {expected_vector_bytes} bytes; "
            f"promotion limit is {maximum_vector_bytes}"
        )
    try:
        metadata = ServingMetadata.model_validate(
            {
                "schema_id": schema_id,
                "generation_id": values["generation_id"],
                "corpus_sha256": values["corpus_sha256"],
                "contract_sha256": values["contract_sha256"],
                "embedding_provider": values["embedding_provider"],
                "embedding_model": values["embedding_model"],
                "embedding_input_policy_version": values["embedding_input_policy_version"],
                "embedding_dimension": dimension,
                "embedding_count": count,
                "unsupported_document_count": unsupported_count,
                "unsupported_documents_sha256": unsupported_sha256,
                "ocr_failed_document_count": ocr_failed_count,
                "ocr_failed_documents_sha256": ocr_failed_sha256,
            }
        )
    except Exception as exc:
        raise ServingDatabaseError("serving metadata does not satisfy a supported schema") from exc

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
