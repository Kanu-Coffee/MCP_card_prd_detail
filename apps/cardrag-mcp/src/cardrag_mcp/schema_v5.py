"""Fail-closed validation and read-only mmap loading for serving-db v5."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from types import MappingProxyType
from typing import Literal, cast

import numpy as np
from cardrag_core import (
    canonical_sha256,
    qwen3_embedding_profile_id,
    v5_exact_row_corpus_sha256,
)
from numpy.typing import NDArray

from cardrag_mcp.models import ServingMetadata

SCHEMA_ID_V5 = "cardrag.serving-db.v5"
V5_EMBEDDING_DIMENSION = 4096
FLOAT32_BYTES = 4
V5_VECTOR_ROW_BYTES = V5_EMBEDDING_DIMENSION * FLOAT32_BYTES
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_ID = re.compile(r"^source_[0-9a-f]{64}$")
_DOCUMENT_ID = re.compile(r"^doc_[0-9a-f]{64}$")
_PROFILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")
_ALLOWED_PROVIDER_IDS = frozenset({"deepinfra", "nebius"})
_NODE_TYPES = frozenset(
    {
        "ROOT",
        "MAJOR_SECTION",
        "ITEM",
        "PARAGRAPH",
        "LIST_ITEM",
        "TABLE",
        "TABLE_ROW",
        "FOOTNOTE",
        "BOILERPLATE",
        "UNCLASSIFIED",
    }
)
_MAJOR_CLASSES = frozenset({"BENEFIT", "NOTICE", "MIXED", "UNKNOWN"})
_VIEW_TYPES = frozenset(
    {"TITLE", "RAW_ITEM", "CONTEXTUAL_ITEM", "DETAIL", "MAJOR_SECTION", "CONTRACT"}
)
_CANONICAL_LEAF_TYPES = frozenset(
    {"PARAGRAPH", "LIST_ITEM", "TABLE_ROW", "FOOTNOTE", "BOILERPLATE", "UNCLASSIFIED"}
)
_CONTAINER_TYPES = _NODE_TYPES - _CANONICAL_LEAF_TYPES
_LINK_TYPES = frozenset({"CONTINUATION_OF", "FOOTNOTE_OF", "APPLIES_TO", "PREVIOUS", "NEXT"})
_UNSUPPORTED_DOCUMENTS_SCHEMA = "cardrag.unsupported-documents.v1"
_OCR_FAILED_DOCUMENTS_SCHEMA = "cardrag.ocr-failed-products.v1"
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

V5_REQUIRED_COLUMNS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "metadata": ("key", "value"),
        "issuers": ("code", "display_name", "sort_order"),
        "product_lineages": (
            "product_lineage_id",
            "issuer",
            "product_code",
            "document_type",
            "name",
        ),
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
        "ocr_failed_products": (
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
        ),
        "contract_revisions": (
            "contract_revision_id",
            "product_lineage_id",
            "document_id",
            "source_id",
            "source_version",
            "source_url",
            "effective_date",
            "pdf_sha256",
            "pdf_size_bytes",
            "page_count",
            "temporal_status",
            "supersedes_revision_id",
        ),
        "document_pages": ("contract_revision_id", "page", "text", "text_sha256"),
        "structure_nodes": (
            "node_id",
            "contract_revision_id",
            "parent_id",
            "parent_contract_revision_id",
            "node_type",
            "major_class",
            "raw_heading",
            "ordinal",
            "display_text",
            "table_headers_json",
            "table_cells_json",
            "table_role",
        ),
        "node_spans": (
            "node_id",
            "contract_revision_id",
            "page",
            "source_start",
            "source_end",
            "text_sha256",
            "span_ordinal",
            "is_canonical",
        ),
        "node_links": (
            "from_node_id",
            "from_contract_revision_id",
            "to_node_id",
            "to_contract_revision_id",
            "link_type",
        ),
        "embedding_profiles": (
            "profile_id",
            "provider",
            "model",
            "provider_id",
            "dimension",
            "dtype",
            "normalization",
            "document_policy",
            "query_policy",
            "maximum_tokens",
        ),
        "embedding_views": (
            "view_pk",
            "row_index",
            "node_id",
            "contract_revision_id",
            "view_type",
            "input_sha256",
            "profile_id",
            "display_text",
        ),
        "embedding_view_spans": (
            "row_index",
            "contract_revision_id",
            "page",
            "source_start",
            "source_end",
            "text_sha256",
            "span_ordinal",
        ),
        "revision_coverage": (
            "contract_revision_id",
            "source_sha256",
            "source_non_whitespace_count",
            "covered_non_whitespace_count",
            "coverage_sha256",
        ),
    }
)


class ServingDatabaseV5Error(RuntimeError):
    """The v5 candidate cannot safely become active."""


def _columns(connection: sqlite3.Connection, table: str) -> tuple[str, ...]:
    return tuple(str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")'))


def _metadata_int(values: Mapping[str, str], key: str) -> int:
    try:
        value = int(values[key])
    except (KeyError, ValueError) as exc:
        raise ServingDatabaseV5Error(f"metadata {key} is missing or invalid") from exc
    if value < 0:
        raise ServingDatabaseV5Error(f"metadata {key} must be non-negative")
    return value


def _sha_metadata(values: Mapping[str, str], key: str) -> str:
    value = values.get(key, "")
    if _SHA256.fullmatch(value) is None:
        raise ServingDatabaseV5Error(f"metadata {key} is missing or invalid")
    return value


def _required_metadata(values: Mapping[str, str], key: str) -> str:
    value = values.get(key, "")
    if not value:
        raise ServingDatabaseV5Error(f"metadata {key} is missing")
    return value


def _exact_row_corpus_identity(
    connection: sqlite3.Connection,
    values: Mapping[str, str],
) -> str:
    rows = tuple(
        (
            int(row[0]),
            str(row[1]),
            str(row[2]),
            str(row[3]),
            str(row[4]),
            str(row[5]),
        )
        for row in connection.execute(
            """SELECT row_index,contract_revision_id,node_id,view_type,input_sha256,profile_id
                 FROM embedding_views ORDER BY row_index"""
        )
    )
    revisions = tuple(
        (str(row[0]), str(row[1]), None if row[2] is None else str(row[2]), str(row[3]))
        for row in connection.execute(
            """SELECT contract_revision_id,product_lineage_id,effective_date,temporal_status
                 FROM contract_revisions ORDER BY contract_revision_id"""
        )
    )
    try:
        return v5_exact_row_corpus_sha256(
            embedding_profile_id=_required_metadata(values, "primary_embedding_profile_id"),
            vector_sidecar_sha256=_sha_metadata(values, "vector_sidecar_sha256"),
            rows=rows,
            revisions=revisions,
        )
    except ValueError as exc:
        raise ServingDatabaseV5Error("v5 exact-row corpus identity is invalid") from exc


def _document_aggregation_metadata(
    connection: sqlite3.Connection,
    values: Mapping[str, str],
    *,
    exact_row_corpus_sha256: str,
) -> tuple[str, str, str | None]:
    allowed = {
        "document_aggregation_status",
        "document_aggregation_policy",
        "sealed_profile_sha256",
    }
    unknown = {
        key for key in values if key.startswith("document_aggregation_") and key not in allowed
    }
    if unknown:
        raise ServingDatabaseV5Error("v5 database has unknown document aggregation metadata")
    status = values.get("document_aggregation_status")
    policy = values.get("document_aggregation_policy")
    profile_sha256 = values.get("sealed_profile_sha256")
    if status is None and policy is None and profile_sha256 is None:
        return "candidate_default", "max_child", None
    if status == "candidate_default" and policy == "max_child" and profile_sha256 is None:
        return status, policy, None
    if (
        status != "sealed"
        or policy not in {"max_child", "top3_mean", "contract_plus_child"}
        or profile_sha256 is None
        or _SHA256.fullmatch(profile_sha256) is None
        or values.get("exact_row_corpus_sha256") != exact_row_corpus_sha256
    ):
        raise ServingDatabaseV5Error("v5 document aggregation metadata is incomplete")
    invalid_contract_views = connection.execute(
        """SELECT contract_revision_id
             FROM embedding_views
             GROUP BY contract_revision_id
             HAVING sum(view_type='CONTRACT') != 1
                OR sum(view_type!='CONTRACT') < 1
             LIMIT 1"""
    ).fetchone()
    if invalid_contract_views is not None:
        raise ServingDatabaseV5Error(
            "sealed aggregation requires one CONTRACT and at least one child row per revision"
        )
    return status, policy, profile_sha256


def _file_sha256(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


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
        raise ServingDatabaseV5Error("disposition payload is not canonical JSON") from exc


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _parse_string_tuple_json(raw: object, *, label: str) -> tuple[str, ...]:
    value = str(raw)
    if len(value.encode("utf-8")) > 1024 * 1024:
        raise ServingDatabaseV5Error(f"{label} exceeds its size bound")
    try:
        parsed = json.loads(value, parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ServingDatabaseV5Error(f"{label} is invalid JSON") from exc
    if (
        not isinstance(parsed, list)
        or any(not isinstance(item, str) or "\x00" in item for item in parsed)
        or _canonical_json_bytes(parsed) != value.encode("utf-8")
    ):
        raise ServingDatabaseV5Error(f"{label} is not a canonical string array")
    return tuple(parsed)


def _validate_dispositions(
    connection: sqlite3.Connection,
    values: Mapping[str, str],
) -> tuple[int, str, int, str]:
    issuer_codes = {
        str(row[0]) for row in connection.execute("SELECT code FROM issuers ORDER BY code")
    }
    active_products = {
        (str(row[0]), str(row[1]))
        for row in connection.execute("SELECT issuer,product_code FROM product_lineages")
    }

    unsupported_expected = _metadata_int(values, "unsupported_document_count")
    unsupported_sha256 = _sha_metadata(values, "unsupported_documents_sha256")
    unsupported_rows = connection.execute(
        """SELECT issuer,product_code,name,disposition,source_id,source_version,source_url,
                  protected_magic,protected_sha256,protected_size_bytes,source_payload_json
             FROM unsupported_products ORDER BY issuer,product_code"""
    ).fetchall()
    if unsupported_expected > 100 or len(unsupported_rows) > 100:
        raise ServingDatabaseV5Error("unsupported product count exceeds the promotion limit")
    unsupported_payloads: list[dict[str, object]] = []
    unsupported_identities: set[tuple[str, str]] = set()
    source_ids: set[str] = set()
    for row in unsupported_rows:
        issuer, product_code, name = str(row[0]), str(row[1]), str(row[2])
        disposition, source_id = str(row[3]), str(row[4])
        source_version, source_url = str(row[5]), str(row[6])
        protected_magic, protected_sha256 = str(row[7]), str(row[8])
        protected_size, source_payload_json = row[9], str(row[10])
        identity = (issuer, product_code)
        if (
            issuer not in issuer_codes
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
            or protected_magic not in {"SCDSA002", "SCDSA004", "FASOO_DRMONE"}
            or _SHA256.fullmatch(protected_sha256) is None
            or isinstance(protected_size, bool)
            or not isinstance(protected_size, int)
            or protected_size < 1
            or identity in active_products
            or identity in unsupported_identities
            or source_id in source_ids
        ):
            raise ServingDatabaseV5Error("unsupported product contains an invalid bounded value")
        try:
            payload_bytes = source_payload_json.encode("utf-8")
        except UnicodeError as exc:
            raise ServingDatabaseV5Error("unsupported source payload is not UTF-8") from exc
        if len(payload_bytes) > 1024 * 1024:
            raise ServingDatabaseV5Error("unsupported source payload is too large")
        try:
            source = json.loads(source_payload_json, parse_constant=_reject_json_constant)
        except (json.JSONDecodeError, RecursionError, ValueError) as exc:
            raise ServingDatabaseV5Error("unsupported source payload is invalid JSON") from exc
        if (
            not isinstance(source, dict)
            or set(source) != _SOURCE_FIELDS
            or payload_bytes != _canonical_json_bytes(source)
            or "source_" + hashlib.sha256(payload_bytes).hexdigest() != source_id
            or any(not isinstance(source.get(key), str) for key in _SOURCE_FIELDS - {"metadata"})
            or not isinstance(source.get("metadata"), dict)
            or source.get("issuer") != issuer
            or source.get("product_code") != product_code
            or source.get("product_name") != name
            or source.get("source_version") != source_version
            or source.get("source_url") != source_url
        ):
            raise ServingDatabaseV5Error("unsupported source payload is not canonically bound")
        unsupported_identities.add(identity)
        source_ids.add(source_id)
        unsupported_payloads.append(
            {
                "disposition": disposition,
                "protected_magic": protected_magic,
                "protected_sha256": protected_sha256,
                "protected_size_bytes": protected_size,
                "source": source,
                "source_id": source_id,
            }
        )
    if len(unsupported_payloads) != unsupported_expected:
        raise ServingDatabaseV5Error("unsupported product count differs from metadata")
    # Early v5 Worker exporters hashed this array in the table's deterministic
    # (issuer, product_code) order, while the v4 contract and current exporter
    # use canonical payload order.  Accept only those two exact encodings so a
    # sealed early-v5 database remains readable without weakening any payload
    # binding or admitting an arbitrary metadata digest.
    worker_order_sha256 = canonical_sha256(
        {
            "documents": unsupported_payloads,
            "schema_version": _UNSUPPORTED_DOCUMENTS_SCHEMA,
        }
    )
    unsupported_payloads.sort(key=_canonical_json_bytes)
    canonical_order_sha256 = canonical_sha256(
        {
            "documents": unsupported_payloads,
            "schema_version": _UNSUPPORTED_DOCUMENTS_SCHEMA,
        }
    )
    if unsupported_sha256 not in {canonical_order_sha256, worker_order_sha256}:
        raise ServingDatabaseV5Error("unsupported product hash differs from metadata")

    ocr_expected = _metadata_int(values, "ocr_failed_document_count")
    ocr_sha256 = _sha_metadata(values, "ocr_failed_documents_sha256")
    ocr_payloads: list[dict[str, object]] = []
    ocr_identities: set[tuple[str, str]] = set()
    ocr_document_ids: set[str] = set()
    active_document_ids = {
        str(row[0]) for row in connection.execute("SELECT document_id FROM contract_revisions")
    }
    for row in connection.execute(
        """SELECT issuer,product_code,name,document_id,title,pdf_sha256,pdf_size_bytes,
                  page_count,reason_code,reason,attempts
             FROM ocr_failed_products ORDER BY issuer,product_code"""
    ):
        issuer, product_code, name = str(row[0]), str(row[1]), str(row[2])
        document_id, title, pdf_sha256 = str(row[3]), str(row[4]), str(row[5])
        pdf_size, page_count = row[6], row[7]
        reason_code, reason, attempts = str(row[8]), str(row[9]), row[10]
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
            or isinstance(pdf_size, bool)
            or not isinstance(pdf_size, int)
            or pdf_size < 1
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
            or identity in active_products
            or identity in unsupported_identities
            or identity in ocr_identities
            or document_id in active_document_ids
            or document_id in ocr_document_ids
        ):
            raise ServingDatabaseV5Error("OCR-failed product contains an invalid bounded value")
        ocr_identities.add(identity)
        ocr_document_ids.add(document_id)
        ocr_payloads.append(
            {
                "attempts": attempts,
                "document_id": document_id,
                "issuer": issuer,
                "page_count": page_count,
                "pdf_sha256": pdf_sha256,
                "pdf_size_bytes": pdf_size,
                "product_code": product_code,
                "product_name": name,
                "reason": reason,
                "reason_code": reason_code,
                "title": title,
            }
        )
    if len(ocr_payloads) != ocr_expected:
        raise ServingDatabaseV5Error("OCR-failed product count differs from metadata")
    if (
        canonical_sha256(
            {"documents": ocr_payloads, "schema_version": _OCR_FAILED_DOCUMENTS_SCHEMA}
        )
        != ocr_sha256
    ):
        raise ServingDatabaseV5Error("OCR-failed product hash differs from metadata")
    return unsupported_expected, unsupported_sha256, ocr_expected, ocr_sha256


def _validate_profiles(connection: sqlite3.Connection, values: Mapping[str, str]) -> None:
    primary = values.get("primary_embedding_profile_id", "")
    if _PROFILE.fullmatch(primary) is None:
        raise ServingDatabaseV5Error("primary embedding profile ID is invalid")
    profiles = connection.execute(
        """SELECT profile_id,provider,model,provider_id,dimension,dtype,normalization,
                  document_policy,query_policy,maximum_tokens
             FROM embedding_profiles ORDER BY profile_id"""
    ).fetchall()
    if len(profiles) != _metadata_int(values, "embedding_profile_count") or not profiles:
        raise ServingDatabaseV5Error("embedding profile count differs from metadata")
    seen_primary = False
    for row in profiles:
        profile_id = str(row[0])
        provider_id = str(row[3])
        if (
            _PROFILE.fullmatch(profile_id) is None
            or str(row[1]) != "openrouter"
            or str(row[2]) != "qwen/qwen3-embedding-8b"
            or provider_id not in _ALLOWED_PROVIDER_IDS
            or "fp8" in provider_id.casefold()
            or int(row[4]) != V5_EMBEDDING_DIMENSION
            or str(row[5]) != "float32"
            or str(row[6]) != "l2"
            or str(row[7]) != "cardrag.structure-views.v1"
            or str(row[8]) != "cardrag.qwen3-query.v1"
            or int(row[9]) < 1
            or int(row[9]) > 32768
        ):
            raise ServingDatabaseV5Error("embedding profile violates the sealed Qwen contract")
        canonical_provider_id = cast(Literal["deepinfra", "nebius"], provider_id)
        if profile_id != qwen3_embedding_profile_id(
            canonical_provider_id,
            maximum_tokens=int(row[9]),
        ):
            raise ServingDatabaseV5Error("embedding profile ID is not canonically derived")
        seen_primary |= profile_id == primary
    if not seen_primary:
        raise ServingDatabaseV5Error("primary embedding profile is absent")


def _validate_structure(connection: sqlite3.Connection, values: Mapping[str, str]) -> None:
    table_counts = {
        "issuer_count": ("issuers", "SELECT count(*) FROM issuers"),
        "product_lineage_count": ("product_lineages", "SELECT count(*) FROM product_lineages"),
        "contract_revision_count": (
            "contract_revisions",
            "SELECT count(*) FROM contract_revisions",
        ),
        "document_page_count": ("document_pages", "SELECT count(*) FROM document_pages"),
        "structure_node_count": ("structure_nodes", "SELECT count(*) FROM structure_nodes"),
        "node_span_count": ("node_spans", "SELECT count(*) FROM node_spans"),
        "node_link_count": ("node_links", "SELECT count(*) FROM node_links"),
    }
    for key, (table, query) in table_counts.items():
        actual = int(connection.execute(query).fetchone()[0])
        if actual != _metadata_int(values, key):
            raise ServingDatabaseV5Error(f"{table} count differs from metadata")

    lineage_rows = connection.execute(
        """SELECT product_lineage_id,issuer,product_code,document_type
             FROM product_lineages ORDER BY product_lineage_id"""
    ).fetchall()
    lineage_ids: set[str] = set()
    for row in lineage_rows:
        lineage_id, issuer, product_code, document_type = map(str, row)
        expected_lineage_id = "lineage_" + canonical_sha256(
            {
                "document_type": document_type,
                "issuer": issuer,
                "product_code": product_code,
            }
        )
        if (
            lineage_id != expected_lineage_id
            or not product_code
            or not document_type
            or product_code != product_code.strip()
            or document_type != document_type.strip()
        ):
            raise ServingDatabaseV5Error("product lineage identity is not canonically bound")
        lineage_ids.add(lineage_id)

    revision_rows = connection.execute(
        """SELECT contract_revision_id,product_lineage_id,source_id,source_version,
                  source_url,effective_date,pdf_sha256,pdf_size_bytes,page_count,
                  temporal_status,supersedes_revision_id
             FROM contract_revisions ORDER BY contract_revision_id"""
    ).fetchall()
    revisions: dict[str, tuple[str, date, str | None, str]] = {}
    statuses_by_lineage: defaultdict[str, list[str]] = defaultdict(list)
    for row in revision_rows:
        revision_id, lineage_id, source_id = str(row[0]), str(row[1]), str(row[2])
        source_version, source_url, effective_date_text = str(row[3]), str(row[4]), str(row[5])
        pdf_sha256, temporal_status = str(row[6]), str(row[9])
        supersedes = None if row[10] is None else str(row[10])
        try:
            effective_date = date.fromisoformat(effective_date_text)
        except ValueError as exc:
            raise ServingDatabaseV5Error("contract revision effective date is invalid") from exc
        expected_revision_id = "revision_" + canonical_sha256(
            {
                "pdf_sha256": pdf_sha256,
                "product_lineage_id": lineage_id,
                "source_id": source_id,
            }
        )
        if (
            lineage_id not in lineage_ids
            or _SOURCE_ID.fullmatch(source_id) is None
            or revision_id != expected_revision_id
            or not source_version
            or source_version != source_version.strip()
            or not source_url.startswith("https://")
            or effective_date.isoformat() != effective_date_text
            or _SHA256.fullmatch(pdf_sha256) is None
            or isinstance(row[7], bool)
            or not isinstance(row[7], int)
            or int(row[7]) < 1
            or isinstance(row[8], bool)
            or not isinstance(row[8], int)
            or int(row[8]) < 1
        ):
            raise ServingDatabaseV5Error("contract revision identity or source is invalid")
        revisions[revision_id] = (lineage_id, effective_date, supersedes, temporal_status)
        statuses_by_lineage[lineage_id].append(temporal_status)

    for revision_id, (lineage_id, effective_date, supersedes, _status) in revisions.items():
        if supersedes is None:
            continue
        predecessor = revisions.get(supersedes)
        if (
            predecessor is None
            or predecessor[0] != lineage_id
            or predecessor[1] > effective_date
            or supersedes == revision_id
        ):
            raise ServingDatabaseV5Error("revision supersedes relation is invalid")
        seen = {revision_id}
        current: str | None = supersedes
        while current is not None:
            if current in seen:
                raise ServingDatabaseV5Error("revision supersedes relation contains a cycle")
            seen.add(current)
            current_row = revisions.get(current)
            if current_row is None or current_row[0] != lineage_id:
                raise ServingDatabaseV5Error("revision supersedes relation crosses a lineage")
            current = current_row[2]
    for lineage_id in lineage_ids:
        statuses = statuses_by_lineage[lineage_id]
        has_current = "current" in statuses
        has_ambiguous = "ambiguous" in statuses
        if (not has_current and not has_ambiguous) or (has_current and has_ambiguous):
            raise ServingDatabaseV5Error(
                "lineage must have either one current revision or explicit ambiguity"
            )

    temporal = {
        str(row[0]): int(row[1])
        for row in connection.execute(
            "SELECT temporal_status,count(*) FROM contract_revisions GROUP BY temporal_status"
        )
    }
    if set(temporal) - {"current", "superseded", "ambiguous"}:
        raise ServingDatabaseV5Error("contract revision temporal status is invalid")
    for status, key in (
        ("current", "current_revision_count"),
        ("superseded", "superseded_revision_count"),
        ("ambiguous", "ambiguous_revision_count"),
    ):
        if temporal.get(status, 0) != _metadata_int(values, key):
            raise ServingDatabaseV5Error("contract revision status count differs from metadata")
    duplicate_current = connection.execute(
        """SELECT product_lineage_id FROM contract_revisions
             WHERE temporal_status='current'
             GROUP BY product_lineage_id HAVING count(*)>1 LIMIT 1"""
    ).fetchone()
    if duplicate_current is not None:
        raise ServingDatabaseV5Error("one lineage has multiple current revisions")

    invalid_nodes = connection.execute(
        """SELECT 1 FROM structure_nodes
             WHERE node_type NOT IN ('ROOT','MAJOR_SECTION','ITEM','PARAGRAPH','LIST_ITEM',
                                     'TABLE','TABLE_ROW','FOOTNOTE','BOILERPLATE','UNCLASSIFIED')
                OR major_class NOT IN ('BENEFIT','NOTICE','MIXED','UNKNOWN')
                OR (parent_id IS NULL) != (parent_contract_revision_id IS NULL)
                OR (parent_contract_revision_id IS NOT NULL
                    AND parent_contract_revision_id != contract_revision_id)
             LIMIT 1"""
    ).fetchone()
    if invalid_nodes is not None:
        raise ServingDatabaseV5Error("structure node type, class, or parent contract is invalid")

    nodes_by_revision: defaultdict[str, list[sqlite3.Row]] = defaultdict(list)
    original_row_factory = connection.row_factory
    connection.row_factory = sqlite3.Row
    try:
        node_rows = connection.execute(
            """SELECT node_id,contract_revision_id,parent_id,parent_contract_revision_id,
                      node_type,ordinal,table_headers_json,table_cells_json,table_role
                 FROM structure_nodes ORDER BY contract_revision_id,ordinal,node_id"""
        ).fetchall()
    finally:
        connection.row_factory = original_row_factory
    for row in node_rows:
        nodes_by_revision[str(row["contract_revision_id"])].append(row)
    if set(nodes_by_revision) != set(revisions):
        raise ServingDatabaseV5Error("every contract revision must have a structure tree")
    for revision_id, rows in nodes_by_revision.items():
        if [int(row["ordinal"]) for row in rows] != list(range(len(rows))):
            raise ServingDatabaseV5Error("structure node ordinals are not canonical")
        by_id = {str(row["node_id"]): row for row in rows}
        children: defaultdict[str, list[sqlite3.Row]] = defaultdict(list)
        roots = [row for row in rows if str(row["node_type"]) == "ROOT"]
        if (
            len(roots) != 1
            or int(roots[0]["ordinal"]) != 0
            or roots[0]["parent_id"] is not None
            or any(row["parent_id"] is None and str(row["node_type"]) != "ROOT" for row in rows)
        ):
            raise ServingDatabaseV5Error("contract structure must have one canonical ROOT")
        for row in rows:
            if row["parent_id"] is None:
                parent = None
            else:
                parent = by_id.get(str(row["parent_id"]))
                if (
                    parent is None
                    or str(row["parent_contract_revision_id"]) != revision_id
                    or int(parent["ordinal"]) >= int(row["ordinal"])
                ):
                    raise ServingDatabaseV5Error("structure parent order or identity is invalid")
                children[str(row["parent_id"])].append(row)
            node_type = str(row["node_type"])
            headers = _parse_string_tuple_json(
                row["table_headers_json"], label="table header metadata"
            )
            cells = _parse_string_tuple_json(row["table_cells_json"], label="table cell metadata")
            role = None if row["table_role"] is None else str(row["table_role"])
            if node_type == "TABLE_ROW":
                if (
                    parent is None
                    or str(parent["node_type"]) != "TABLE"
                    or role not in {"HEADER", "SEPARATOR", "BODY"}
                    or not cells
                    or headers
                    != _parse_string_tuple_json(
                        parent["table_headers_json"], label="table header metadata"
                    )
                ):
                    raise ServingDatabaseV5Error(
                        "table row lost its header, cells, role, or parent"
                    )
            elif node_type == "TABLE":
                if cells or role is not None:
                    raise ServingDatabaseV5Error("table container contains row-only metadata")
            elif headers or cells or role is not None:
                raise ServingDatabaseV5Error("non-table node contains table metadata")
        for row in rows:
            if str(row["node_type"]) != "TABLE":
                continue
            table_children = children.get(str(row["node_id"]), [])
            if not table_children or any(
                str(child["node_type"]) != "TABLE_ROW" for child in table_children
            ):
                raise ServingDatabaseV5Error("table must contain only one or more table rows")
    invalid_links = connection.execute(
        """SELECT 1 FROM node_links
             WHERE from_contract_revision_id != to_contract_revision_id
                OR link_type NOT IN ('CONTINUATION_OF','FOOTNOTE_OF','APPLIES_TO','PREVIOUS','NEXT')
             LIMIT 1"""
    ).fetchone()
    if invalid_links is not None:
        raise ServingDatabaseV5Error("cross-contract or invalid structure link exists")
    node_types = {
        str(row[0]): int(row[1])
        for row in connection.execute(
            "SELECT node_type,count(*) FROM structure_nodes GROUP BY node_type"
        )
    }
    if set(node_types) - _NODE_TYPES:
        raise ServingDatabaseV5Error("unknown structure node type exists")
    major_classes = {
        str(row[0]): int(row[1])
        for row in connection.execute(
            "SELECT major_class,count(*) FROM structure_nodes GROUP BY major_class"
        )
    }
    if set(major_classes) - _MAJOR_CLASSES:
        raise ServingDatabaseV5Error("unknown major class exists")
    for node_type in _NODE_TYPES:
        key = f"structure_node_count.{node_type}"
        if node_types.get(node_type, 0) != _metadata_int(values, key):
            raise ServingDatabaseV5Error("node type count differs from metadata")
    major_section_classes = {
        str(row[0]): int(row[1])
        for row in connection.execute(
            """SELECT major_class,count(*) FROM structure_nodes
                 WHERE node_type='MAJOR_SECTION' GROUP BY major_class"""
        )
    }
    for major_class in _MAJOR_CLASSES:
        key = f"structure_major_class_count.{major_class}"
        if major_section_classes.get(major_class, 0) != _metadata_int(values, key):
            raise ServingDatabaseV5Error("major section class count differs from metadata")


def _validate_source_coverage(connection: sqlite3.Connection, values: Mapping[str, str]) -> None:
    pages: dict[tuple[str, int], str] = {}
    pages_by_revision: defaultdict[str, list[tuple[int, str, str]]] = defaultdict(list)
    source_digest = hashlib.sha256()
    total_non_whitespace = 0
    for row in connection.execute(
        "SELECT contract_revision_id,page,text,text_sha256 FROM document_pages ORDER BY 1,2"
    ):
        revision_id, page, text, declared = str(row[0]), int(row[1]), str(row[2]), str(row[3])
        encoded = text.encode("utf-8")
        if hashlib.sha256(encoded).hexdigest() != declared:
            raise ServingDatabaseV5Error("document page text hash mismatch")
        pages[(revision_id, page)] = text
        pages_by_revision[revision_id].append((page, text, declared))
        for character in text:
            if not character.isspace():
                source_digest.update(character.encode("utf-8"))
                total_non_whitespace += 1

    node_contracts: dict[tuple[str, str], tuple[str, str]] = {
        (str(row[0]), str(row[1])): (str(row[2]), str(row[3]))
        for row in connection.execute(
            """SELECT node_id,contract_revision_id,node_type,display_text
                 FROM structure_nodes ORDER BY contract_revision_id,ordinal"""
        )
    }
    coverage = {identity: bytearray(len(text)) for identity, text in pages.items()}
    spans_by_node: defaultdict[tuple[str, str], list[int]] = defaultdict(list)
    span_text_by_node: defaultdict[tuple[str, str], list[str]] = defaultdict(list)
    canonical_flags_by_node: defaultdict[tuple[str, str], list[int]] = defaultdict(list)
    for row in connection.execute(
        """SELECT node_id,contract_revision_id,page,source_start,source_end,text_sha256,
                  span_ordinal,is_canonical
             FROM node_spans ORDER BY contract_revision_id,node_id,span_ordinal"""
    ):
        node_id, revision_id = str(row[0]), str(row[1])
        node_identity = (node_id, revision_id)
        page, start, end = int(row[2]), int(row[3]), int(row[4])
        page_text = pages.get((revision_id, page))
        if (
            node_identity not in node_contracts
            or page_text is None
            or start < 0
            or end <= start
            or end > len(page_text)
        ):
            raise ServingDatabaseV5Error("node span is outside its contract page")
        source_text = page_text[start:end]
        if hashlib.sha256(source_text.encode("utf-8")).hexdigest() != str(row[5]):
            raise ServingDatabaseV5Error("node span hash does not match source text")
        ordinal = int(row[6])
        expected_ordinal = len(spans_by_node[node_identity])
        if ordinal != expected_ordinal:
            raise ServingDatabaseV5Error("node span ordinals are not contiguous")
        canonical = int(row[7])
        if canonical not in {0, 1}:
            raise ServingDatabaseV5Error("node span canonical flag is invalid")
        spans_by_node[node_identity].append(ordinal)
        span_text_by_node[node_identity].append(source_text)
        canonical_flags_by_node[node_identity].append(canonical)
        if canonical == 1:
            marks = coverage[(revision_id, page)]
            for position in range(start, end):
                if marks[position]:
                    raise ServingDatabaseV5Error("canonical source spans overlap")
                marks[position] = 1

    for node_identity, (node_type, display_text) in node_contracts.items():
        flags = canonical_flags_by_node.get(node_identity, [])
        if "".join(span_text_by_node.get(node_identity, [])) != display_text:
            raise ServingDatabaseV5Error("structure node display text is not source-bound")
        if node_type in _CANONICAL_LEAF_TYPES:
            if not flags or any(flag != 1 for flag in flags):
                raise ServingDatabaseV5Error("canonical leaf spans are incomplete or non-canonical")
        elif node_type in _CONTAINER_TYPES and any(flag != 0 for flag in flags):
            raise ServingDatabaseV5Error("structure container claims canonical source coverage")

    covered_digest = hashlib.sha256()
    covered_non_whitespace = 0
    for identity, text in sorted(pages.items()):
        marks = coverage[identity]
        for position, character in enumerate(text):
            if character.isspace():
                continue
            if marks[position] != 1:
                raise ServingDatabaseV5Error("canonical source coverage is below 100 percent")
            covered_digest.update(character.encode("utf-8"))
            covered_non_whitespace += 1
    if (
        total_non_whitespace != _metadata_int(values, "source_non_whitespace_count")
        or covered_non_whitespace != _metadata_int(values, "covered_non_whitespace_count")
        or total_non_whitespace != covered_non_whitespace
        or source_digest.hexdigest() != _sha_metadata(values, "source_coverage_sha256")
        or covered_digest.hexdigest() != source_digest.hexdigest()
    ):
        raise ServingDatabaseV5Error("aggregate source coverage metadata is inconsistent")

    revision_rows = connection.execute(
        """SELECT contract_revision_id,source_sha256,source_non_whitespace_count,
                  covered_non_whitespace_count,coverage_sha256
             FROM revision_coverage ORDER BY contract_revision_id"""
    ).fetchall()
    revision_count = int(
        connection.execute("SELECT count(*) FROM contract_revisions").fetchone()[0]
    )
    if len(revision_rows) != revision_count:
        raise ServingDatabaseV5Error("revision coverage ledger is incomplete")
    declared_page_counts = {
        str(row[0]): int(row[1])
        for row in connection.execute(
            "SELECT contract_revision_id,page_count FROM contract_revisions"
        )
    }
    if set(pages_by_revision) != set(declared_page_counts):
        raise ServingDatabaseV5Error("every contract revision must have source pages")
    expected_coverage: dict[str, tuple[str, int, int, str]] = {}
    for revision_id, revision_pages in pages_by_revision.items():
        ordered = sorted(revision_pages, key=lambda item: item[0])
        if [page for page, _, _ in ordered] != list(
            range(1, declared_page_counts[revision_id] + 1)
        ):
            raise ServingDatabaseV5Error("contract revision source pages are not complete")
        source_non_whitespace = sum(
            not character.isspace() for _, text, _ in ordered for character in text
        )
        source_sha256 = canonical_sha256(
            {
                "pages": [
                    {"page": page, "text_sha256": text_sha256} for page, _, text_sha256 in ordered
                ],
                "schema_version": "cardrag.structure-source.v1",
            }
        )
        coverage_sha256 = canonical_sha256(
            {
                "pages": [
                    {
                        "non_whitespace_characters": sum(
                            not character.isspace() for character in text
                        ),
                        "non_whitespace_sha256": hashlib.sha256(
                            "".join(
                                character for character in text if not character.isspace()
                            ).encode("utf-8")
                        ).hexdigest(),
                        "page": page,
                        "text_sha256": text_sha256,
                    }
                    for page, text, text_sha256 in ordered
                ],
                "schema_version": "cardrag.structure-coverage.v1",
            }
        )
        expected_coverage[revision_id] = (
            source_sha256,
            source_non_whitespace,
            source_non_whitespace,
            coverage_sha256,
        )
    actual_coverage = {
        str(row[0]): (str(row[1]), int(row[2]), int(row[3]), str(row[4])) for row in revision_rows
    }
    if actual_coverage != expected_coverage:
        raise ServingDatabaseV5Error("revision coverage ledger is not source-bound")


def _validate_view_source_spans(
    connection: sqlite3.Connection,
    values: Mapping[str, str],
) -> None:
    views = {
        int(row[0]): (str(row[1]), str(row[2]))
        for row in connection.execute(
            """SELECT row_index,contract_revision_id,display_text
                 FROM embedding_views ORDER BY row_index"""
        )
    }
    pages = {
        (str(row[0]), int(row[1])): str(row[2])
        for row in connection.execute("SELECT contract_revision_id,page,text FROM document_pages")
    }
    span_text: defaultdict[int, list[str]] = defaultdict(list)
    for row in connection.execute(
        """SELECT row_index,contract_revision_id,page,source_start,source_end,
                  text_sha256,span_ordinal
             FROM embedding_view_spans ORDER BY row_index,span_ordinal"""
    ):
        row_index, revision_id = int(row[0]), str(row[1])
        page, start, end, text_sha256, ordinal = (
            int(row[2]),
            int(row[3]),
            int(row[4]),
            str(row[5]),
            int(row[6]),
        )
        view = views.get(row_index)
        page_text = pages.get((revision_id, page))
        if (
            view is None
            or view[0] != revision_id
            or page_text is None
            or start < 0
            or end <= start
            or end > len(page_text)
            or ordinal != len(span_text[row_index])
        ):
            raise ServingDatabaseV5Error("embedding view source span is unbound or invalid")
        text = page_text[start:end]
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != text_sha256:
            raise ServingDatabaseV5Error("embedding view source span hash is invalid")
        span_text[row_index].append(text)
    if _metadata_int(values, "embedding_view_span_count") != sum(
        len(items) for items in span_text.values()
    ):
        raise ServingDatabaseV5Error("embedding view span count differs from metadata")
    for row_index, (_revision_id, display_text) in views.items():
        if not span_text[row_index] or "".join(span_text[row_index]) != display_text:
            raise ServingDatabaseV5Error("embedding view display text is not source-bound")


def validate_schema_v5(
    connection: sqlite3.Connection,
    *,
    maximum_sidecar_bytes: int | None = None,
) -> ServingMetadata:
    """Validate the complete v5 relational contract without loading vector bytes."""

    integrity = connection.execute("PRAGMA integrity_check").fetchone()
    if integrity is None or integrity[0] != "ok":
        raise ServingDatabaseV5Error("SQLite integrity_check failed")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise ServingDatabaseV5Error("SQLite foreign_key_check failed")
    for table, expected in V5_REQUIRED_COLUMNS.items():
        actual = _columns(connection, table)
        if actual != expected:
            raise ServingDatabaseV5Error(f"unexpected {table} schema: {actual!r}")

    values = {
        str(row[0]): str(row[1]) for row in connection.execute("SELECT key,value FROM metadata")
    }
    if values.get("schema_id") != SCHEMA_ID_V5:
        raise ServingDatabaseV5Error("serving database schema version is not v5")
    for key in ("generation_id", "embedding_provider", "embedding_model"):
        if not values.get(key):
            raise ServingDatabaseV5Error(f"metadata {key} is missing")
    for key in (
        "corpus_sha256",
        "contract_sha256",
        "source_coverage_sha256",
        "vector_sidecar_sha256",
        "unsupported_documents_sha256",
        "ocr_failed_documents_sha256",
        "parser_policy_sha256",
        "embedding_policy_sha256",
        "retrieval_policy_sha256",
    ):
        _sha_metadata(values, key)
    for issuer_row in connection.execute("SELECT code FROM issuers ORDER BY code"):
        issuer = str(issuer_row[0])
        parser_profile_id = _required_metadata(values, f"parser_profile_id.{issuer}")
        if _PROFILE.fullmatch(parser_profile_id) is None:
            raise ServingDatabaseV5Error("issuer parser profile ID is invalid")
        _sha_metadata(values, f"parser_profile_sha256.{issuer}")
    dimension = _metadata_int(values, "embedding_dimension")
    count = _metadata_int(values, "embedding_count")
    sidecar_size = _metadata_int(values, "vector_sidecar_size_bytes")
    if (
        count < 1
        or dimension != V5_EMBEDDING_DIMENSION
        or values.get("embedding_provider") != "openrouter"
        or values.get("embedding_model") != "qwen/qwen3-embedding-8b"
        or values.get("embedding_input_policy_version") != "cardrag.structure-views.v1"
        or values.get("vector_sidecar_dtype") != "float32"
        or values.get("vector_sidecar_normalization") != "l2"
        or values.get("vector_sidecar_byte_order") != "little-endian"
        or values.get("vector_sidecar_layout") != "row-major"
        or values.get("vector_sidecar_profile_id") != values.get("primary_embedding_profile_id")
        or _metadata_int(values, "vector_sidecar_dimension") != dimension
        or _metadata_int(values, "vector_sidecar_row_count") != count
        or sidecar_size != count * V5_VECTOR_ROW_BYTES
    ):
        raise ServingDatabaseV5Error("v5 embedding/sidecar metadata is inconsistent")
    if maximum_sidecar_bytes is not None and sidecar_size > maximum_sidecar_bytes:
        raise ServingDatabaseV5Error("v5 vector sidecar exceeds the promotion limit")
    unsupported_count, unsupported_sha256, ocr_failed_count, ocr_failed_sha256 = (
        _validate_dispositions(connection, values)
    )
    _validate_profiles(connection, values)
    _validate_structure(connection, values)
    _validate_source_coverage(connection, values)
    _validate_view_source_spans(connection, values)

    view_count = int(connection.execute("SELECT count(*) FROM embedding_views").fetchone()[0])
    if view_count != count:
        raise ServingDatabaseV5Error("embedding view count differs from metadata")
    indices = [
        int(row[0])
        for row in connection.execute("SELECT row_index FROM embedding_views ORDER BY row_index")
    ]
    if indices != list(range(count)):
        raise ServingDatabaseV5Error("embedding view row indices are not contiguous from zero")
    view_types = {
        str(row[0]): int(row[1])
        for row in connection.execute(
            "SELECT view_type,count(*) FROM embedding_views GROUP BY view_type"
        )
    }
    if set(view_types) - _VIEW_TYPES:
        raise ServingDatabaseV5Error("unknown embedding view type exists")
    for view_type in _VIEW_TYPES:
        key = f"embedding_view_count.{view_type}"
        if view_types.get(view_type, 0) != _metadata_int(values, key):
            raise ServingDatabaseV5Error("embedding view type count differs from metadata")
    invalid_view = connection.execute(
        """SELECT 1 FROM embedding_views AS v
             LEFT JOIN structure_nodes AS n
               ON n.node_id=v.node_id AND n.contract_revision_id=v.contract_revision_id
             WHERE n.node_id IS NULL OR v.profile_id != ?
                OR length(v.input_sha256) != 64 OR length(v.display_text)=0
             LIMIT 1""",
        (values["primary_embedding_profile_id"],),
    ).fetchone()
    if invalid_view is not None:
        raise ServingDatabaseV5Error("embedding view is not bound to its node/input")
    if any(
        _SHA256.fullmatch(str(row[0])) is None
        for row in connection.execute("SELECT input_sha256 FROM embedding_views")
    ):
        raise ServingDatabaseV5Error("embedding view input hash is invalid")
    missing_revision_view = connection.execute(
        """SELECT r.contract_revision_id FROM contract_revisions AS r
             LEFT JOIN embedding_views AS v
               ON v.contract_revision_id=r.contract_revision_id
             GROUP BY r.contract_revision_id HAVING count(v.row_index)=0 LIMIT 1"""
    ).fetchone()
    if missing_revision_view is not None:
        raise ServingDatabaseV5Error("one or more contract revisions have no embedding views")

    exact_row_corpus_sha256 = _exact_row_corpus_identity(connection, values)
    declared_exact_row_corpus_sha256 = values.get("exact_row_corpus_sha256")
    if declared_exact_row_corpus_sha256 is not None and (
        _SHA256.fullmatch(declared_exact_row_corpus_sha256) is None
        or declared_exact_row_corpus_sha256 != exact_row_corpus_sha256
    ):
        raise ServingDatabaseV5Error("v5 exact-row corpus metadata differs from its rows")
    aggregation_status, aggregation_policy, aggregation_profile_sha256 = (
        _document_aggregation_metadata(
            connection,
            values,
            exact_row_corpus_sha256=exact_row_corpus_sha256,
        )
    )

    try:
        return ServingMetadata.model_validate(
            {
                "schema_id": SCHEMA_ID_V5,
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
                "primary_embedding_profile_id": values["primary_embedding_profile_id"],
                "vector_sidecar_sha256": values["vector_sidecar_sha256"],
                "vector_sidecar_size_bytes": sidecar_size,
                "exact_row_corpus_sha256": exact_row_corpus_sha256,
                "document_aggregation_status": aggregation_status,
                "document_aggregation_policy": aggregation_policy,
                "sealed_profile_sha256": aggregation_profile_sha256,
            }
        )
    except Exception as exc:
        raise ServingDatabaseV5Error("v5 serving metadata is invalid") from exc


@dataclass(frozen=True, slots=True)
class LoadedVectorsV5:
    row_indices: tuple[int, ...]
    node_ids: tuple[str, ...]
    contract_revision_ids: tuple[str, ...]
    view_types: tuple[str, ...]
    profile_ids: tuple[str, ...]
    matrix: NDArray[np.float32]
    norms: NDArray[np.float32]


def load_vectors_v5(
    connection: sqlite3.Connection,
    sidecar_path: Path,
    *,
    metadata: ServingMetadata,
    maximum_sidecar_bytes: int,
) -> LoadedVectorsV5:
    """Hash, mmap, and block-validate one immutable FP32 v5 sidecar."""

    if sidecar_path.is_symlink() or not sidecar_path.is_file():
        raise ServingDatabaseV5Error("v5 vector sidecar is missing or unsafe")
    expected_size = metadata.embedding_count * V5_VECTOR_ROW_BYTES
    if expected_size > maximum_sidecar_bytes:
        raise ServingDatabaseV5Error("v5 vector sidecar exceeds the promotion limit")
    digest, size = _file_sha256(sidecar_path)
    if (
        metadata.vector_sidecar_sha256 is None
        or digest != metadata.vector_sidecar_sha256
        or metadata.vector_sidecar_size_bytes != size
        or size != expected_size
    ):
        raise ServingDatabaseV5Error("v5 vector sidecar hash or size differs from metadata")
    matrix = np.memmap(
        sidecar_path,
        mode="r",
        dtype="<f4",
        shape=(metadata.embedding_count, V5_EMBEDDING_DIMENSION),
        order="C",
    )
    norms = np.empty((metadata.embedding_count,), dtype=np.float32)
    for start in range(0, metadata.embedding_count, 4096):
        block = np.asarray(matrix[start : start + 4096], dtype=np.float32)
        if not bool(np.isfinite(block).all()):
            raise ServingDatabaseV5Error("v5 vector sidecar contains a non-finite value")
        block_norms = np.linalg.norm(block, axis=1).astype(np.float32, copy=False)
        if block_norms.size and not bool(np.allclose(block_norms, 1.0, rtol=2e-5, atol=2e-5)):
            raise ServingDatabaseV5Error("v5 vector sidecar contains a non-normalized row")
        norms[start : start + len(block_norms)] = block_norms
    if any(not math.isfinite(float(value)) or value <= 0 for value in norms):
        raise ServingDatabaseV5Error("v5 vector sidecar contains a zero or invalid norm")

    rows = connection.execute(
        """SELECT row_index,node_id,contract_revision_id,view_type,profile_id
             FROM embedding_views ORDER BY row_index"""
    ).fetchall()
    if len(rows) != metadata.embedding_count:
        raise ServingDatabaseV5Error("embedding views changed while loading sidecar")
    return LoadedVectorsV5(
        row_indices=tuple(int(row[0]) for row in rows),
        node_ids=tuple(str(row[1]) for row in rows),
        contract_revision_ids=tuple(str(row[2]) for row in rows),
        view_types=tuple(str(row[3]) for row in rows),
        profile_ids=tuple(str(row[4]) for row in rows),
        matrix=matrix,
        norms=norms,
    )
