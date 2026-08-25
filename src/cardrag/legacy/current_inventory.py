"""Read-only export of the currently published v0.2.1 OCR inventory.

The exporter deliberately does not instantiate ``ContentAddressedObjectStore``:
its constructor prepares writable directories, while this cutover path must not
mutate the archived CAS.  Database rows and object paths are instead validated
in place and emitted as the ledger consumed by the v1 Worker adoption command.
"""

from __future__ import annotations

import re
import stat
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any

from cardrag.db import Postgres
from cardrag.domain.canonical import canonical_json_bytes, canonical_sha256
from cardrag.storage.paths import atomic_write_bytes, portable_relative_path

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ISSUER_CODE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,31}$")


class CurrentInventoryExportError(RuntimeError):
    """The published v0.2.1 state is not safe to adopt."""


_CURRENT_INVENTORY_SQL = """
SELECT
    current_database() AS database_name,
    a.generation_id,
    g.state::text AS generation_state,
    g.manifest_sha256,
    g.schema_version AS generation_schema_version,
    g.root_uri AS generation_root_uri,
    g.latest_document_count,
    d.document_id,
    d.issuer,
    d.product_code,
    d.document_type,
    d.effective_date,
    d.source_version,
    d.pdf_sha256,
    d.raw_object_key,
    d.pdf_size_bytes,
    d.pdf_page_count,
    d.ocr_sha256,
    d.ocr_object_key,
    jsonb_typeof(d.ocr_pages) AS ocr_pages_type,
    CASE
        WHEN jsonb_typeof(d.ocr_pages) = 'array' THEN jsonb_array_length(d.ocr_pages)
        ELSE NULL
    END AS ocr_page_count,
    jsonb_typeof(d.ocr_manifest) AS ocr_manifest_type
FROM active_generation AS a
JOIN generations AS g ON g.generation_id = a.generation_id
JOIN generation_documents AS d ON d.generation_id = a.generation_id
WHERE a.singleton IS TRUE AND d.is_latest IS TRUE
ORDER BY d.issuer, d.product_code, d.document_id
"""


def _required_text(row: Mapping[str, Any], field: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise CurrentInventoryExportError(f"published row has invalid {field}")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise CurrentInventoryExportError(f"published row has invalid {field}")
    return value


def _required_sha256(row: Mapping[str, Any], field: str) -> str:
    value = _required_text(row, field)
    if _SHA256.fullmatch(value) is None:
        raise CurrentInventoryExportError(f"published row has invalid {field}")
    return value


def _positive_integer(row: Mapping[str, Any], field: str) -> int:
    value = row.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CurrentInventoryExportError(f"published row has invalid {field}")
    return value


def _effective_date(row: Mapping[str, Any]) -> str:
    value = row.get("effective_date")
    if isinstance(value, date):
        return value.isoformat()
    if not isinstance(value, str):
        raise CurrentInventoryExportError("published row has invalid effective_date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise CurrentInventoryExportError("published row has invalid effective_date") from exc
    if parsed.isoformat() != value:
        raise CurrentInventoryExportError("published row has invalid effective_date")
    return value


def _storage_root(path: Path) -> Path:
    if not path.is_absolute():
        raise CurrentInventoryExportError("CARDRAG_STORAGE_ROOT must be absolute")
    try:
        root = path.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise CurrentInventoryExportError("CARDRAG_STORAGE_ROOT is unavailable") from exc
    if not root.is_dir():
        raise CurrentInventoryExportError("CARDRAG_STORAGE_ROOT must be a directory")
    return root


def _cas_path(root: Path, object_key: str, digest: str, *, expected_size: int | None = None) -> Path:
    try:
        relative = portable_relative_path(object_key)
    except ValueError as exc:
        raise CurrentInventoryExportError("published object key is unsafe") from exc
    expected = portable_relative_path(f"sha256/{digest[:2]}/{digest}")
    if relative != expected:
        raise CurrentInventoryExportError("published object key is not bound to its ledger SHA-256")

    candidate = root.joinpath(*relative.parts)
    component = root
    for part in relative.parts:
        component /= part
        if component.is_symlink():
            raise CurrentInventoryExportError("published CAS object path contains a symlink")
    try:
        resolved = candidate.resolve(strict=True)
        metadata = resolved.stat()
    except (FileNotFoundError, OSError) as exc:
        raise CurrentInventoryExportError("published CAS object is unavailable") from exc
    if not resolved.is_relative_to(root):
        raise CurrentInventoryExportError("published CAS object escapes CARDRAG_STORAGE_ROOT")
    if not stat.S_ISREG(metadata.st_mode):
        raise CurrentInventoryExportError("published CAS object must be a regular file")
    if expected_size is not None and metadata.st_size != expected_size:
        raise CurrentInventoryExportError("published PDF size differs from its database ledger")
    return resolved


def _consistent_generation(rows: Sequence[Mapping[str, Any]]) -> dict[str, str | int]:
    if not rows:
        raise CurrentInventoryExportError("no active generation with latest documents was found")
    first = rows[0]
    generation: dict[str, str | int] = {
        "database_name": _required_text(first, "database_name"),
        "generation_id": _required_text(first, "generation_id"),
        "generation_state": _required_text(first, "generation_state"),
        "manifest_sha256": _required_sha256(first, "manifest_sha256"),
        "generation_schema_version": _required_text(first, "generation_schema_version"),
        "generation_root_uri": _required_text(first, "generation_root_uri"),
        "latest_document_count": _positive_integer(first, "latest_document_count"),
    }
    if generation["generation_state"] != "published":
        raise CurrentInventoryExportError("active generation is not published")
    for row in rows[1:]:
        for field, expected in generation.items():
            if row.get(field) != expected:
                raise CurrentInventoryExportError("active generation identity changed within the snapshot")
    if generation["latest_document_count"] != len(rows):
        raise CurrentInventoryExportError("active generation latest-document count is inconsistent")
    return generation


def normalize_current_inventory(
    rows: Sequence[Mapping[str, Any]], storage_root: Path
) -> tuple[dict[str, str], ...]:
    """Validate DB rows and resolve their immutable CAS paths."""

    root = _storage_root(storage_root)
    generation = _consistent_generation(rows)
    source_database_id = "v0.2.1-postgres:" + canonical_sha256(
        {
            "database_name": generation["database_name"],
            "generation_id": generation["generation_id"],
            "generation_manifest_sha256": generation["manifest_sha256"],
            "generation_root_uri": generation["generation_root_uri"],
            "generation_schema_version": generation["generation_schema_version"],
        }
    )
    products: set[tuple[str, str]] = set()
    documents: set[str] = set()
    output: list[dict[str, str]] = []
    for row in rows:
        issuer = _required_text(row, "issuer")
        if _ISSUER_CODE.fullmatch(issuer) is None:
            raise CurrentInventoryExportError("published row has invalid issuer")
        product_code = _required_text(row, "product_code")
        product_key = (issuer, product_code)
        if product_key in products:
            raise CurrentInventoryExportError(
                f"active generation contains duplicate product: {issuer}/{product_code}"
            )
        products.add(product_key)

        document_id = _required_text(row, "document_id")
        if document_id in documents:
            raise CurrentInventoryExportError("active generation contains a duplicate document ID")
        documents.add(document_id)
        document_type = _required_text(row, "document_type")
        effective_date = _effective_date(row)
        source_version = _required_text(row, "source_version")
        pdf_sha256 = _required_sha256(row, "pdf_sha256")
        ocr_sha256 = _required_sha256(row, "ocr_sha256")
        pdf_size = _positive_integer(row, "pdf_size_bytes")
        pdf_pages = _positive_integer(row, "pdf_page_count")
        if row.get("ocr_pages_type") != "array" or row.get("ocr_manifest_type") != "object":
            raise CurrentInventoryExportError("published row has incomplete OCR metadata")
        if row.get("ocr_page_count") != pdf_pages:
            raise CurrentInventoryExportError("published OCR page count differs from the PDF ledger")

        pdf_path = _cas_path(
            root,
            _required_text(row, "raw_object_key"),
            pdf_sha256,
            expected_size=pdf_size,
        )
        ocr_path = _cas_path(
            root,
            _required_text(row, "ocr_object_key"),
            ocr_sha256,
        )
        v1_document_id = "doc_" + canonical_sha256(
            {
                "document_type": document_type,
                "effective_date": effective_date,
                "issuer": issuer,
                "pdf_sha256": pdf_sha256,
                "product_code": product_code,
                "version": source_version,
            }
        )
        output.append(
            {
                "document_type": document_type,
                "effective_date": effective_date,
                "issuer": issuer,
                "ledger_ocr_sha256": ocr_sha256,
                "ledger_pdf_sha256": pdf_sha256,
                "legacy_source_document_id": document_id,
                "ocr_path": str(ocr_path),
                "pdf_path": str(pdf_path),
                "product_code": product_code,
                "source_bundle_id": str(generation["generation_id"]),
                "source_bundle_sha256": str(generation["manifest_sha256"]),
                "source_database_id": source_database_id,
                "source_document_id": document_id,
                "source_version": source_version,
                "v1_document_id": v1_document_id,
            }
        )
    return tuple(sorted(output, key=lambda item: (item["issuer"], item["product_code"])))


def export_current_inventory(database: Postgres, storage_root: Path) -> tuple[dict[str, str], ...]:
    """Read the active generation in one repeatable, explicitly read-only transaction."""

    with database.connection() as connection:
        try:
            with connection.cursor() as cursor:
                cursor.execute("BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                cursor.execute(_CURRENT_INVENTORY_SQL)
                rows = cursor.fetchall()
        finally:
            connection.rollback()
    return normalize_current_inventory(rows, storage_root)


def canonical_inventory_jsonl(rows: Sequence[Mapping[str, str]]) -> bytes:
    """Return deterministic UTF-8 JSONL suitable for the v1 Worker loader."""

    return b"".join(canonical_json_bytes(dict(row)) + b"\n" for row in rows)


def write_current_inventory(rows: Sequence[Mapping[str, str]], output: Path, *, protected_root: Path) -> Path:
    """Create a new inventory file without ever writing into the archived CAS."""

    if not output.is_absolute():
        raise CurrentInventoryExportError("inventory output path must be absolute")
    root = _storage_root(protected_root)
    try:
        parent = output.parent.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise CurrentInventoryExportError("inventory output parent must already exist") from exc
    target = parent / output.name
    if target == root or target.is_relative_to(root):
        raise CurrentInventoryExportError("inventory output must be outside CARDRAG_STORAGE_ROOT")
    if target.exists() or target.is_symlink():
        raise CurrentInventoryExportError("inventory output already exists")
    try:
        return atomic_write_bytes(target, canonical_inventory_jsonl(rows), mode=0o600)
    except OSError as exc:
        raise CurrentInventoryExportError("could not create inventory output") from exc
