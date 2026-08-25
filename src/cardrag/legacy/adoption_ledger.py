"""Read-only export of the trusted v0.2.1 legacy-adoption DB ledger."""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from cardrag.db import Postgres
from cardrag.domain.canonical import canonical_json_bytes, canonical_sha256
from cardrag.storage.paths import atomic_write_bytes

ADOPTION_LEDGER_SCHEMA = "cardrag.legacy-adoption-ledger.v1"

_BUNDLE_ID = re.compile(r"^bundle-[0-9a-f]{12}$")
_ISSUER_CODE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,31}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class AdoptionLedgerExportError(RuntimeError):
    """The production import ledger is not safe to export for v1 adoption."""


_ADOPTION_LEDGER_SQL = """
SELECT
    current_database() AS database_name,
    i.import_id,
    i.bundle_id,
    i.bundle_sha256,
    i.generation_id,
    i.state::text AS import_state,
    i.phase AS import_phase,
    d.document_id AS source_document_id,
    d.document_key,
    d.issuer,
    d.pdf_sha256,
    d.ocr_sha256,
    d.disposition,
    d.state::text AS document_state
FROM legacy_imports AS i
LEFT JOIN legacy_import_documents AS d
  ON d.import_id = i.import_id
 AND d.disposition = 'adopted'
WHERE i.state = 'succeeded'
{selection}
ORDER BY i.import_id, d.document_key, d.document_id
"""


def _required_text(row: Mapping[str, Any], field: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise AdoptionLedgerExportError(f"legacy adoption ledger has invalid {field}")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise AdoptionLedgerExportError(f"legacy adoption ledger has invalid {field}")
    return value


def _required_sha256(row: Mapping[str, Any], field: str) -> str:
    value = _required_text(row, field)
    if _SHA256.fullmatch(value) is None:
        raise AdoptionLedgerExportError(f"legacy adoption ledger has invalid {field}")
    return value


def _required_uuid(row: Mapping[str, Any], field: str) -> str:
    value = str(row.get(field) or "")
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise AdoptionLedgerExportError(f"legacy adoption ledger has invalid {field}") from exc
    if str(parsed) != value:
        raise AdoptionLedgerExportError(f"legacy adoption ledger has non-canonical {field}")
    return value


def normalize_adoption_ledger(
    rows: Sequence[Mapping[str, Any]],
    *,
    expected_import_id: uuid.UUID | None = None,
) -> tuple[dict[str, str], ...]:
    """Validate one complete succeeded import and return its adopted document rows."""

    if not rows:
        suffix = f" {expected_import_id}" if expected_import_id is not None else ""
        raise AdoptionLedgerExportError(f"no succeeded legacy import{suffix} was found")

    import_ids = {_required_uuid(row, "import_id") for row in rows}
    if expected_import_id is None and len(import_ids) != 1:
        raise AdoptionLedgerExportError(
            "multiple succeeded legacy imports exist; select one with --import-id"
        )
    if len(import_ids) != 1:
        raise AdoptionLedgerExportError("selected legacy import rows contain multiple import IDs")
    import_id = next(iter(import_ids))
    if expected_import_id is not None and import_id != str(expected_import_id):
        raise AdoptionLedgerExportError("database returned rows for a different legacy import")

    first = rows[0]
    database_name = _required_text(first, "database_name")
    bundle_id = _required_text(first, "bundle_id")
    bundle_sha256 = _required_sha256(first, "bundle_sha256")
    generation_id = _required_text(first, "generation_id")
    if _BUNDLE_ID.fullmatch(bundle_id) is None or bundle_id != f"bundle-{bundle_sha256[:12]}":
        raise AdoptionLedgerExportError("legacy import bundle ID does not match its SHA-256")
    if first.get("import_state") != "succeeded":
        raise AdoptionLedgerExportError("legacy import is not succeeded")
    if first.get("import_phase") not in {"published", "no_change"}:
        raise AdoptionLedgerExportError("legacy import did not reach a published terminal phase")

    header = {
        "database_name": database_name,
        "import_id": import_id,
        "bundle_id": bundle_id,
        "bundle_sha256": bundle_sha256,
        "generation_id": generation_id,
        "import_state": "succeeded",
        "import_phase": str(first["import_phase"]),
    }
    source_database_id = "v0.2.1-postgres:" + canonical_sha256(header)
    seen_documents: set[str] = set()
    seen_keys: set[str] = set()
    normalized: list[dict[str, str]] = []
    for row in rows:
        actual_header = {
            "database_name": _required_text(row, "database_name"),
            "import_id": _required_uuid(row, "import_id"),
            "bundle_id": _required_text(row, "bundle_id"),
            "bundle_sha256": _required_sha256(row, "bundle_sha256"),
            "generation_id": _required_text(row, "generation_id"),
            "import_state": str(row.get("import_state") or ""),
            "import_phase": str(row.get("import_phase") or ""),
        }
        if actual_header != header:
            raise AdoptionLedgerExportError("legacy import identity changed within the snapshot")
        if row.get("document_state") != "succeeded" or row.get("disposition") != "adopted":
            raise AdoptionLedgerExportError("adopted legacy document is not succeeded")
        source_document_id = _required_text(row, "source_document_id")
        document_key = _required_text(row, "document_key")
        issuer = _required_text(row, "issuer")
        if _ISSUER_CODE.fullmatch(issuer) is None:
            raise AdoptionLedgerExportError("legacy adoption ledger has invalid issuer")
        pdf_sha256 = _required_sha256(row, "pdf_sha256")
        ocr_sha256 = _required_sha256(row, "ocr_sha256")
        if source_document_id in seen_documents:
            raise AdoptionLedgerExportError("legacy adoption ledger has a duplicate document ID")
        if document_key in seen_keys:
            raise AdoptionLedgerExportError("legacy adoption ledger has a duplicate document key")
        seen_documents.add(source_document_id)
        seen_keys.add(document_key)
        normalized.append(
            {
                "schema_version": ADOPTION_LEDGER_SCHEMA,
                "source_database_id": source_database_id,
                "import_id": import_id,
                "bundle_id": bundle_id,
                "bundle_sha256": bundle_sha256,
                "generation_id": generation_id,
                "source_document_id": source_document_id,
                "document_key": document_key,
                "issuer": issuer,
                "pdf_sha256": pdf_sha256,
                "ocr_sha256": ocr_sha256,
                "disposition": "adopted",
                "status": "succeeded",
            }
        )
    if not normalized:
        raise AdoptionLedgerExportError("succeeded legacy import has no adopted documents")
    return tuple(
        sorted(
            normalized,
            key=lambda item: (item["issuer"], item["document_key"], item["source_document_id"]),
        )
    )


def export_adoption_ledger(
    database: Postgres,
    *,
    import_id: uuid.UUID | None = None,
) -> tuple[dict[str, str], ...]:
    """Read one succeeded legacy import in a repeatable, read-only transaction."""

    selection = "" if import_id is None else "AND i.import_id = %s"
    statement = _ADOPTION_LEDGER_SQL.format(selection=selection)
    with database.connection() as connection:
        try:
            with connection.cursor() as cursor:
                cursor.execute("BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                if import_id is None:
                    cursor.execute(statement)
                else:
                    cursor.execute(statement, (import_id,))
                rows = cursor.fetchall()
        finally:
            connection.rollback()
    return normalize_adoption_ledger(rows, expected_import_id=import_id)


def canonical_adoption_ledger_jsonl(rows: Sequence[Mapping[str, str]]) -> bytes:
    """Return deterministic, sorted JSONL for the v1 Worker ledger loader."""

    ordered = sorted(
        (dict(row) for row in rows),
        key=lambda item: (item["issuer"], item["document_key"], item["source_document_id"]),
    )
    return b"".join(canonical_json_bytes(row) + b"\n" for row in ordered)


def write_adoption_ledger(
    rows: Sequence[Mapping[str, str]],
    output: Path,
    *,
    protected_root: Path,
) -> Path:
    """Create a new ledger file without writing in the archived source/CAS root."""

    if not output.is_absolute():
        raise AdoptionLedgerExportError("adoption ledger output must be absolute")
    try:
        storage_root = protected_root.resolve(strict=True)
        parent = output.parent.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise AdoptionLedgerExportError(
            "adoption ledger output parent or storage root is unavailable"
        ) from exc
    if not storage_root.is_dir() or not parent.is_dir():
        raise AdoptionLedgerExportError("adoption ledger output parent and storage root must be directories")
    target = parent / output.name
    if target == storage_root or target.is_relative_to(storage_root):
        raise AdoptionLedgerExportError("adoption ledger output must be outside CARDRAG_STORAGE_ROOT")
    if target.exists() or target.is_symlink():
        raise AdoptionLedgerExportError("adoption ledger output already exists")
    try:
        return atomic_write_bytes(
            target,
            canonical_adoption_ledger_jsonl(rows),
            mode=0o600,
        )
    except OSError as exc:
        raise AdoptionLedgerExportError("could not create adoption ledger output") from exc
