"""Auditable validation of legacy/current OCR exports before WebDAV adoption."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from cardrag_core import canonical_json_bytes, canonical_sha256
from cardrag_core.domain import ArtifactRef
from cardrag_core.manifests import (
    LEGACY_ADOPTION_POLICY_V1,
    LEGACY_ADOPTION_POLICY_V2,
    LEGACY_OCR_APPROVED_PREFIX,
    LEGACY_OCR_APPROVED_PREFIX_SHA256,
    LEGACY_OCR_NORMALIZATION_EXACT,
    LEGACY_OCR_NORMALIZATION_STRIP_PREFIX_V1,
    AdoptedOCRArtifactManifest,
    LegacyAdoptionReceipt,
    LegacyAdoptionReceiptV2,
    LegacyAdoptionValidation,
    LegacyAdoptionValidationV2,
    OCRReady,
    adopted_ocr_reuse_key,
)
from cardrag_core.ocr import OCRInput, verify_ocr_bytes
from cardrag_core.paths import STABLE_POINTER_PATH, ocr_manifest_path, ocr_ready_path

from .downloader import validate_pdf
from .webdav import WebDAVClient

ADOPTION_POLICY_VERSION = LEGACY_ADOPTION_POLICY_V2
LEGACY_ADOPTION_POLICY_VERSION = LEGACY_ADOPTION_POLICY_V1
LEGACY_LEDGER_SCHEMA = "cardrag.legacy-adoption-ledger.v1"
DATA_KIT_INVENTORY_SCHEMA_V2 = "cardrag.data-kit-adoption-inventory.v2"
DATA_KIT_RECEIPT_SCHEMA_V2 = "cardrag.data-kit-normalization-receipt.v2"
DATA_KIT_REJECTION_SCHEMA_V2 = "cardrag.data-kit-adoption-rejection.v2"
DATA_KIT_EXPORT_SCHEMA_V2 = "cardrag.data-kit-adoption-export.v2"
DATA_KIT_SOURCE_SCHEMA_V2 = "cardrag.data-kit-source.v2"
ADOPTION_CONTROL_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class AdoptionError(RuntimeError):
    pass


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _file_sha256(path: Path) -> str:
    return _stable_file_sha256(path, field="file")


def _open_readonly_nofollow(path: Path, *, field: str) -> int:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise AdoptionError(f"{field} cannot be opened as a non-symlink file") from exc
    opened = os.fstat(descriptor)
    if not stat.S_ISREG(opened.st_mode):
        os.close(descriptor)
        raise AdoptionError(f"{field} must be a regular file")
    return descriptor


def _fd_and_path_identity_match(descriptor: int, path: Path, before: os.stat_result) -> bool:
    after = os.fstat(descriptor)
    try:
        path_after = path.lstat()
    except FileNotFoundError:
        return False
    before_identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    path_identity = (
        path_after.st_dev,
        path_after.st_ino,
        path_after.st_size,
        path_after.st_mtime_ns,
    )
    return (
        before_identity == after_identity == path_identity
        and stat.S_ISREG(path_after.st_mode)
        and not stat.S_ISLNK(path_after.st_mode)
    )


def _stable_file_bytes(path: Path, *, field: str) -> bytes:
    descriptor = _open_readonly_nofollow(path, field=field)
    try:
        before = os.fstat(descriptor)
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            body = stream.read()
        if len(body) != before.st_size or not _fd_and_path_identity_match(descriptor, path, before):
            raise AdoptionError(f"{field} changed while it was read")
        return body
    finally:
        os.close(descriptor)


def _stable_file_sha256(path: Path, *, field: str) -> str:
    descriptor = _open_readonly_nofollow(path, field=field)
    digest = hashlib.sha256()
    size = 0
    try:
        before = os.fstat(descriptor)
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
                size += len(block)
        if size != before.st_size or not _fd_and_path_identity_match(descriptor, path, before):
            raise AdoptionError(f"{field} changed while it was hashed")
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _validate_pdf_stable(path: Path) -> tuple[str, int, int]:
    descriptor = _open_readonly_nofollow(path, field="adoption PDF")
    before = os.fstat(descriptor)
    try:
        result = validate_pdf(Path(f"/proc/self/fd/{descriptor}"))
        if not _fd_and_path_identity_match(descriptor, path, before):
            raise AdoptionError("adoption PDF changed while it was validated")
        return result
    finally:
        os.close(descriptor)


def _reject_symlink_components(path: Path, *, field: str) -> None:
    absolute = path.absolute()
    for component in (absolute, *absolute.parents):
        if component.is_symlink():
            raise AdoptionError(f"{field} contains a symlink component")


def _regular_file(value: object, *, field: str) -> Path:
    raw = Path(str(value))
    _reject_symlink_components(raw, field=field)
    path = raw.resolve()
    if not path.is_file():
        raise AdoptionError(f"{field} must be a regular non-symlink file")
    return path


_V2_EXPORT_MANIFEST_FIELDS = {
    "schema_version",
    "policy_version",
    "source_root",
    "source_bundle_id",
    "source_bundle_sha256",
    "source_database_id",
    "selected_documents",
    "accepted_documents",
    "rejected_documents",
    "exact_documents",
    "normalized_documents",
    "inventory_sha256",
    "rejected_sha256",
    "receipts_sha256",
    "normalized_objects",
}
_V2_NORMALIZATION_RECEIPT_FIELDS = {
    "schema_version",
    "policy_version",
    "controls",
    "issuer",
    "product_code",
    "source_database_id",
    "source_document_id",
    "source_ocr_relative_path",
    "normalized_ocr_object_relative_path",
    "ledger_ocr_sha256",
    "normalization_profile",
    "normalized_ocr_sha256",
    "normalized_ocr_size_bytes",
    "prefix_sha256",
    "removed_bytes",
    "source_ocr_sha256",
    "source_ocr_size_bytes",
    "source_bundle_id",
    "source_bundle_sha256",
}
_V2_REJECTION_FIELDS = {
    "schema_version",
    "policy_version",
    "controls",
    "source_database_id",
    "source_document_id",
    "issuer",
    "product_code",
    "reason",
    "detail",
    "source_bundle_id",
    "source_bundle_sha256",
}
_V2_CONTROL_FIELDS = {
    "data_pack_manifest_sha256",
    "inventory_sha256",
    "master_manifest_sha256",
    "ocr_inventory_sha256",
}
_V2_LINEAGE_FIELDS = (
    "ledger_ocr_sha256",
    "normalization_profile",
    "normalized_ocr_sha256",
    "normalized_ocr_size_bytes",
    "prefix_sha256",
    "removed_bytes",
    "source_ocr_sha256",
    "source_ocr_size_bytes",
)


def _read_canonical_json(path: Path, *, field: str) -> tuple[dict[str, Any], bytes]:
    body = _stable_file_bytes(_regular_file(path, field=field), field=field)
    try:
        value = json.loads(body)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise AdoptionError(f"{field} is not valid JSON") from exc
    if not isinstance(value, dict) or body != canonical_json_bytes(value) + b"\n":
        raise AdoptionError(f"{field} is not canonical JSON")
    return value, body


def _read_canonical_jsonl(path: Path, *, field: str) -> tuple[tuple[dict[str, Any], ...], bytes]:
    body = _stable_file_bytes(_regular_file(path, field=field), field=field)
    lines = body.splitlines()
    rows: list[dict[str, Any]] = []
    try:
        for line in lines:
            value = json.loads(line)
            if not isinstance(value, dict) or line != canonical_json_bytes(value):
                raise AdoptionError(f"{field} contains a non-canonical row")
            rows.append(value)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise AdoptionError(f"{field} is not valid canonical JSONL") from exc
    expected = b"".join(canonical_json_bytes(row) + b"\n" for row in rows)
    if body != expected:
        raise AdoptionError(f"{field} is not canonical JSONL")
    return tuple(rows), body


def _export_row_order(row: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(row.get("issuer") or ""),
        str(row.get("product_code") or ""),
        str(row.get("source_document_id") or ""),
    )


def _v2_controls_from_inventory(row: Mapping[str, Any]) -> dict[str, str]:
    return {
        "data_pack_manifest_sha256": str(row.get("source_data_pack_manifest_sha256") or ""),
        "inventory_sha256": str(row.get("source_inventory_sha256") or ""),
        "master_manifest_sha256": str(row.get("source_master_manifest_sha256") or ""),
        "ocr_inventory_sha256": str(row.get("source_ocr_inventory_sha256") or ""),
    }


def _validate_control_map(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != _V2_CONTROL_FIELDS:
        raise AdoptionError("v2 export control hash map is invalid")
    controls = {field: str(value[field]) for field in sorted(_V2_CONTROL_FIELDS)}
    if any(_SHA256_RE.fullmatch(digest) is None for digest in controls.values()):
        raise AdoptionError("v2 export control hash map contains an invalid SHA-256")
    return controls


def _strict_manifest_count(manifest: Mapping[str, Any], field: str) -> int:
    value = manifest.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise AdoptionError(f"v2 export manifest {field} must be a non-negative integer")
    return value


def _selected_data_kit_documents(inventory_path: Path) -> dict[str, dict[str, Any]]:
    for suffix in ("-journal", "-wal", "-shm"):
        if os.path.lexists(f"{inventory_path}{suffix}"):
            raise AdoptionError("v2 source inventory SQLite has a live sidecar and is not immutable")
    descriptor = _open_readonly_nofollow(inventory_path, field="v2 source inventory SQLite")
    before = os.fstat(descriptor)
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            f"file:/proc/self/fd/{descriptor}?mode=ro&immutable=1",
            uri=True,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        rows = tuple(
            dict(row)
            for row in connection.execute(
                "SELECT doc_id, card_company, product_code, doc_type, effective_date, version, "
                "ocr_remote_rel, metadata_remote_rel, pages "
                "FROM documents "
                "WHERE is_latest=1 AND status='done' AND error='' ORDER BY doc_id"
            )
        )
    except sqlite3.Error as exc:
        os.close(descriptor)
        raise AdoptionError("v2 source inventory SQLite cannot be queried safely") from exc
    finally:
        if connection is not None:
            connection.close()
    if not _fd_and_path_identity_match(descriptor, inventory_path, before):
        os.close(descriptor)
        raise AdoptionError("v2 source inventory SQLite changed while it was queried")
    os.close(descriptor)
    values = tuple(str(row.get("doc_id") or "") for row in rows)
    if values != tuple(sorted(set(values))) or any(not value for value in values):
        raise AdoptionError("v2 source inventory SQLite selected IDs are not unique")
    return {value: row for value, row in zip(values, rows, strict=True)}


def _source_relative_file(source_root: Path, value: object, *, field: str) -> Path:
    if not isinstance(value, str):
        raise AdoptionError(f"{field} must be a source-root relative path")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts or relative.as_posix() != value:
        raise AdoptionError(f"{field} is an unsafe source-root relative path")
    path = _regular_file(source_root / relative, field=field)
    if not path.is_relative_to(source_root):
        raise AdoptionError(f"{field} escapes source_root")
    return path


def _source_master_by_document_id(source_root: Path) -> dict[str, dict[str, Any]]:
    master_path = _regular_file(
        source_root / "artifacts/manifests/cardrag_master_manifest.json",
        field="v2 source master manifest",
    )
    body = _stable_file_bytes(master_path, field="v2 source master manifest")
    try:
        value = json.loads(body)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise AdoptionError("v2 source master manifest is invalid JSON") from exc
    if not isinstance(value, dict) or value.get("schema_version") != "cardrag_master_manifest.v2":
        raise AdoptionError("v2 source master manifest schema is invalid")
    entries = value.get("entries")
    if not isinstance(entries, list) or not all(isinstance(entry, dict) for entry in entries):
        raise AdoptionError("v2 source master manifest entries are invalid")
    by_id: dict[str, dict[str, Any]] = {}
    for raw in entries:
        entry = cast(dict[str, Any], raw)
        document_id = entry.get("doc_version_id")
        if not isinstance(document_id, str) or not document_id or document_id in by_id:
            raise AdoptionError("v2 source master manifest document IDs are invalid")
        by_id[document_id] = entry
    return by_id


def _normalized_source_sha256(value: object, *, field: str) -> str:
    digest = str(value or "").strip().casefold().removeprefix("sha256:")
    if _SHA256_RE.fullmatch(digest) is None:
        raise AdoptionError(f"{field} has no valid SHA-256")
    return digest


def _bind_v2_row_to_source_ledgers(
    row: Mapping[str, Any],
    receipt: Mapping[str, Any],
    *,
    source_root: Path,
    database: Mapping[str, Any],
    master: Mapping[str, Any],
) -> None:
    issuer_map = {"kbcard": "kb", "wooricard": "woori"}
    expected_fields = {
        "issuer": issuer_map.get(str(database.get("card_company") or ""), ""),
        "product_code": str(database.get("product_code") or ""),
        "document_type": str(database.get("doc_type") or ""),
        "effective_date": str(database.get("effective_date") or ""),
        "source_version": str(database.get("version") or ""),
        "page_count": database.get("pages"),
    }
    if any(row.get(field) != value for field, value in expected_fields.items()):
        raise AdoptionError("v2 inventory document identity differs from source SQLite")
    master_to_database = {
        "doc_version_id": "doc_id",
        "cardCompany": "card_company",
        "productCode": "product_code",
        "docType": "doc_type",
        "beginDt": "effective_date",
        "gdccVer": "version",
        "ocr_remote_rel": "ocr_remote_rel",
        "metadata_remote_rel": "metadata_remote_rel",
        "pages": "pages",
    }
    if any(
        master.get(master_field) != database.get(database_field)
        for master_field, database_field in master_to_database.items()
    ):
        raise AdoptionError("v2 source master manifest differs from source SQLite")
    master_pdf_sha = _normalized_source_sha256(master.get("pdf_sha256"), field="v2 source master PDF")
    if row.get("ledger_pdf_sha256") != master_pdf_sha:
        raise AdoptionError("v2 inventory PDF hash differs from source master manifest")
    source_ocr_relative = str(receipt.get("source_ocr_relative_path") or "")
    if source_ocr_relative != database.get("ocr_remote_rel"):
        raise AdoptionError("v2 inventory source OCR path differs from source SQLite")
    metadata_relative = database.get("metadata_remote_rel")
    metadata_path = _source_relative_file(
        source_root,
        metadata_relative,
        field="v2 source OCR metadata",
    )
    metadata_body = _stable_file_bytes(metadata_path, field="v2 source OCR metadata")
    try:
        metadata = json.loads(metadata_body)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise AdoptionError("v2 source OCR metadata is invalid JSON") from exc
    metadata_pages = metadata.get("page_count", metadata.get("pages")) if isinstance(metadata, dict) else None
    if (
        not isinstance(metadata, dict)
        or metadata.get("schema_version") not in {"cardrag_imported_ocr_asset.v1", "ocr_result_manifest.v2"}
        or metadata.get("doc_version_id") != row.get("source_document_id")
        or metadata.get("status") != "success"
        or metadata.get("primary_text_artifact") != "ocr.md"
        or metadata.get("metadata_rel_path") != metadata_relative
        or metadata.get("ocr_md_rel_path") != source_ocr_relative
        or metadata_pages != row.get("page_count")
        or _normalized_source_sha256(metadata.get("ocr_md_sha256"), field="v2 source metadata OCR")
        != row.get("source_ocr_sha256")
    ):
        raise AdoptionError("v2 source OCR metadata does not bind the inventory row")
    metadata_pdf_sha_raw = metadata.get("raw_pdf_sha256")
    metadata_pdf_sha = (
        None
        if metadata_pdf_sha_raw is None
        else _normalized_source_sha256(metadata_pdf_sha_raw, field="v2 source metadata PDF")
    )
    if metadata_pdf_sha is not None and metadata_pdf_sha != master_pdf_sha:
        raise AdoptionError("v2 source OCR metadata PDF hash differs")


def _validate_v2_source_controls(source_root_value: object, controls: Mapping[str, str]) -> Path:
    if not isinstance(source_root_value, str):
        raise AdoptionError("v2 export source_root must be an absolute path")
    raw = Path(source_root_value)
    if not raw.is_absolute() or str(raw) != source_root_value:
        raise AdoptionError("v2 export source_root must be a canonical absolute path")
    _reject_symlink_components(raw, field="v2 export source_root")
    source_root = raw.resolve(strict=True)
    if not source_root.is_dir() or str(source_root) != source_root_value:
        raise AdoptionError("v2 export source_root is not a canonical directory")
    control_paths = {
        "data_pack_manifest_sha256": source_root / "DATA_PACK_MANIFEST.json",
        "master_manifest_sha256": source_root / "artifacts/manifests/cardrag_master_manifest.json",
        "inventory_sha256": source_root / "data/db/inventory.sqlite3",
        "ocr_inventory_sha256": source_root / "data/db/ocr_inventory.sqlite3",
    }
    for control_field, path in control_paths.items():
        control_path = _regular_file(path, field=f"v2 source control {control_field}")
        if (
            _stable_file_sha256(control_path, field=f"v2 source control {control_field}")
            != controls[control_field]
        ):
            raise AdoptionError(f"v2 source control hash differs: {control_field}")
    return source_root


def _v2_source_bundle_sha256(
    controls: Mapping[str, str],
    rows: Sequence[Mapping[str, Any]],
) -> str:
    transformations = [
        {
            "issuer": str(row["issuer"]),
            "normalization_profile": row["normalization_profile"],
            "normalized_ocr_sha256": row["normalized_ocr_sha256"],
            "normalized_ocr_size_bytes": row["normalized_ocr_size_bytes"],
            "prefix_sha256": row["prefix_sha256"],
            "product_code": str(row["product_code"]),
            "removed_bytes": row["removed_bytes"],
            "source_document_id": str(row["source_document_id"]),
            "source_ocr_sha256": row["source_ocr_sha256"],
            "source_ocr_size_bytes": row["source_ocr_size_bytes"],
        }
        for row in rows
    ]
    return canonical_sha256(
        {
            "policy_version": LEGACY_ADOPTION_POLICY_V2,
            "controls": dict(controls),
            "schema_version": DATA_KIT_SOURCE_SCHEMA_V2,
            "transformations": sorted(transformations, key=_export_row_order),
        }
    )


def load_data_kit_adoption_export(export_root: Path) -> tuple[dict[str, Any], ...]:
    """Open one sealed v2 exporter root and re-bind it to the read-only source data-kit."""

    _reject_symlink_components(export_root, field="v2 adoption export")
    root = export_root.resolve(strict=True)
    if not root.is_dir():
        raise AdoptionError("v2 adoption export must be a directory")
    for path in root.rglob("*"):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise AdoptionError("v2 adoption export contains a symlink or special node")
    manifest, _ = _read_canonical_json(root / "export-manifest.json", field="v2 export manifest")
    if set(manifest) != _V2_EXPORT_MANIFEST_FIELDS:
        raise AdoptionError("v2 export manifest has missing or extra fields")
    if (
        manifest.get("schema_version") != DATA_KIT_EXPORT_SCHEMA_V2
        or manifest.get("policy_version") != LEGACY_ADOPTION_POLICY_V2
    ):
        raise AdoptionError("v2 export manifest schema/policy is invalid")
    for field in (
        "source_bundle_sha256",
        "inventory_sha256",
        "rejected_sha256",
        "receipts_sha256",
    ):
        if _SHA256_RE.fullmatch(str(manifest.get(field) or "")) is None:
            raise AdoptionError(f"v2 export manifest {field} is invalid")
    inventory, inventory_body = _read_canonical_jsonl(root / "inventory.jsonl", field="v2 export inventory")
    receipts, receipts_body = _read_canonical_jsonl(
        root / "normalization-receipts.jsonl", field="v2 export receipts"
    )
    rejected, rejected_body = _read_canonical_jsonl(root / "rejected.jsonl", field="v2 export rejections")
    if (
        hashlib.sha256(inventory_body).hexdigest() != manifest["inventory_sha256"]
        or hashlib.sha256(receipts_body).hexdigest() != manifest["receipts_sha256"]
        or hashlib.sha256(rejected_body).hexdigest() != manifest["rejected_sha256"]
    ):
        raise AdoptionError("v2 export manifest does not bind its exact JSONL bytes")
    accepted_count = _strict_manifest_count(manifest, "accepted_documents")
    rejected_count = _strict_manifest_count(manifest, "rejected_documents")
    selected_count = _strict_manifest_count(manifest, "selected_documents")
    exact_count = _strict_manifest_count(manifest, "exact_documents")
    normalized_count = _strict_manifest_count(manifest, "normalized_documents")
    normalized_objects = _strict_manifest_count(manifest, "normalized_objects")
    if (
        accepted_count != len(inventory)
        or accepted_count != len(receipts)
        or rejected_count != len(rejected)
        or selected_count != accepted_count + rejected_count
        or accepted_count != exact_count + normalized_count
        or accepted_count < 1
    ):
        raise AdoptionError("v2 export manifest document counts disagree")
    if list(map(_export_row_order, inventory)) != sorted(map(_export_row_order, inventory)):
        raise AdoptionError("v2 export inventory is not deterministically sorted")
    if list(map(_export_row_order, receipts)) != sorted(map(_export_row_order, receipts)):
        raise AdoptionError("v2 export receipts are not deterministically sorted")
    if list(map(_export_row_order, rejected)) != sorted(map(_export_row_order, rejected)):
        raise AdoptionError("v2 export rejections are not deterministically sorted")
    inventory_keys = [_export_row_order(row) for row in inventory]
    receipt_keys = [_export_row_order(row) for row in receipts]
    rejected_keys = [_export_row_order(row) for row in rejected]
    if (
        len(inventory_keys) != len(set(inventory_keys))
        or len(receipt_keys) != len(set(receipt_keys))
        or len(rejected_keys) != len(set(rejected_keys))
        or inventory_keys != receipt_keys
    ):
        raise AdoptionError("v2 export has duplicate or unmatched inventory/receipt identities")

    first_controls = _v2_controls_from_inventory(inventory[0])
    controls = _validate_control_map(first_controls)
    source_bundle_sha = str(manifest["source_bundle_sha256"])
    source_bundle_id = str(manifest.get("source_bundle_id") or "")
    source_database_id = str(manifest.get("source_database_id") or "")
    expected_object_files: set[Path] = set()
    profiles: list[str] = []
    for row, receipt in zip(inventory, receipts, strict=True):
        _validate_v2_inventory_shape(row)
        if set(receipt) != _V2_NORMALIZATION_RECEIPT_FIELDS:
            raise AdoptionError("v2 normalization receipt has missing or extra fields")
        if (
            receipt.get("schema_version") != DATA_KIT_RECEIPT_SCHEMA_V2
            or receipt.get("policy_version") != LEGACY_ADOPTION_POLICY_V2
            or _validate_control_map(receipt.get("controls")) != controls
            or _v2_controls_from_inventory(row) != controls
        ):
            raise AdoptionError("v2 normalization receipt control identity differs")
        if any(receipt.get(field) != row.get(field) for field in _V2_LINEAGE_FIELDS):
            raise AdoptionError("v2 normalization receipt lineage differs from inventory")
        for field in ("issuer", "product_code", "source_database_id", "source_document_id"):
            if receipt.get(field) != row.get(field):
                raise AdoptionError(f"v2 normalization receipt {field} differs from inventory")
        if (
            row.get("legacy_source_document_id") != row.get("source_document_id")
            or row.get("source_bundle_id") != source_bundle_id
            or receipt.get("source_bundle_id") != source_bundle_id
            or row.get("source_bundle_sha256") != source_bundle_sha
            or receipt.get("source_bundle_sha256") != source_bundle_sha
            or row.get("source_database_id") != source_database_id
        ):
            raise AdoptionError("v2 export row bundle/database identity differs")
        source_relative_raw = receipt.get("source_ocr_relative_path")
        if not isinstance(source_relative_raw, str):
            raise AdoptionError("v2 source OCR relative path is invalid")
        source_relative = Path(source_relative_raw)
        if (
            source_relative.is_absolute()
            or ".." in source_relative.parts
            or source_relative.as_posix() != source_relative_raw
        ):
            raise AdoptionError("v2 source OCR relative path is unsafe")
        source_ocr_path = Path(str(row["source_ocr_path"]))
        profile = str(row["normalization_profile"])
        profiles.append(profile)
        normalized_relative_raw = receipt.get("normalized_ocr_object_relative_path")
        if profile == LEGACY_OCR_NORMALIZATION_EXACT:
            if normalized_relative_raw is not None or Path(str(row["ocr_path"])) != source_ocr_path:
                raise AdoptionError("exact v2 export OCR paths are inconsistent")
        else:
            expected_relative = (
                f"objects/ocr/sha256/{str(row['normalized_ocr_sha256'])[:2]}/"
                f"{row['normalized_ocr_sha256']}.md"
            )
            if normalized_relative_raw != expected_relative:
                raise AdoptionError("normalized v2 export object path is not content-addressed")
            output_path = _regular_file(root / expected_relative, field="v2 normalized OCR object")
            row_output_path = _regular_file(row["ocr_path"], field="v2 inventory normalized OCR path")
            if row_output_path != output_path:
                raise AdoptionError("v2 inventory normalized OCR path escapes its export")
            expected_object_files.add(output_path)
        row["_sealed_source_ocr_relative_path"] = source_relative.as_posix()
    if profiles.count(LEGACY_OCR_NORMALIZATION_EXACT) != exact_count:
        raise AdoptionError("v2 export exact profile count differs")
    if profiles.count(LEGACY_OCR_NORMALIZATION_STRIP_PREFIX_V1) != normalized_count:
        raise AdoptionError("v2 export normalized profile count differs")
    if len(expected_object_files) != normalized_objects:
        raise AdoptionError("v2 export normalized object count differs")

    for row in rejected:
        if set(row) != _V2_REJECTION_FIELDS:
            raise AdoptionError("v2 rejection receipt has missing or extra fields")
        if (
            row.get("schema_version") != DATA_KIT_REJECTION_SCHEMA_V2
            or row.get("policy_version") != LEGACY_ADOPTION_POLICY_V2
            or _validate_control_map(row.get("controls")) != controls
            or row.get("source_bundle_id") != source_bundle_id
            or row.get("source_bundle_sha256") != source_bundle_sha
            or row.get("source_database_id") != source_database_id
            or not str(row.get("reason") or "")
            or not str(row.get("detail") or "")
        ):
            raise AdoptionError("v2 rejection receipt identity is invalid")

    computed_bundle_sha = _v2_source_bundle_sha256(controls, inventory)
    if (
        computed_bundle_sha != source_bundle_sha
        or source_bundle_id != f"data-kit-v2-{computed_bundle_sha[:12]}"
    ):
        raise AdoptionError("v2 export source bundle identity is not reproducible")
    source_root = _validate_v2_source_controls(manifest.get("source_root"), controls)
    if source_root == root or source_root.is_relative_to(root) or root.is_relative_to(source_root):
        raise AdoptionError("v2 export and source data-kit roots must be separate")
    for row in inventory:
        source_relative = Path(str(row.pop("_sealed_source_ocr_relative_path")))
        source_ocr_path = _regular_file(row["source_ocr_path"], field="v2 source OCR")
        pdf_path = _regular_file(row["pdf_path"], field="v2 source PDF")
        if source_ocr_path != (source_root / source_relative).resolve(strict=True):
            raise AdoptionError("v2 source OCR path is not bound to source_root")
        if not pdf_path.is_relative_to(source_root):
            raise AdoptionError("v2 source PDF path escapes source_root")
    selected_documents = _selected_data_kit_documents(source_root / "data/db/inventory.sqlite3")
    source_master = _source_master_by_document_id(source_root)
    exported_ids = tuple(sorted({key[2] for key in inventory_keys + rejected_keys}))
    if (
        tuple(selected_documents) != exported_ids
        or len(exported_ids) != len(inventory_keys) + len(rejected_keys)
        or len(selected_documents) != selected_count
    ):
        raise AdoptionError("v2 export does not exactly cover the source SQLite selected documents")
    for row, receipt in zip(inventory, receipts, strict=True):
        document_id = str(row["source_document_id"])
        database = selected_documents.get(document_id)
        master = source_master.get(document_id)
        if database is None or master is None:
            raise AdoptionError("v2 export row is absent from source SQLite/master controls")
        _bind_v2_row_to_source_ledgers(
            row,
            receipt,
            source_root=source_root,
            database=database,
            master=master,
        )

    expected_files = {
        (root / "export-manifest.json").resolve(),
        (root / "inventory.jsonl").resolve(),
        (root / "normalization-receipts.jsonl").resolve(),
        (root / "rejected.jsonl").resolve(),
        *expected_object_files,
    }
    actual_files = {path.resolve() for path in root.rglob("*") if path.is_file()}
    if actual_files != expected_files:
        raise AdoptionError("v2 adoption export contains unlisted or missing files")
    expected_directories = {root}
    for path in expected_files:
        parent = path.parent
        while parent != root:
            expected_directories.add(parent)
            parent = parent.parent
    actual_directories = {root, *(path.resolve() for path in root.rglob("*") if path.is_dir())}
    if actual_directories != expected_directories:
        raise AdoptionError("v2 adoption export contains an unlisted or missing directory")
    return tuple(dict(row) for row in inventory)


def load_inventory(path: Path) -> tuple[dict[str, Any], ...]:
    if path.is_dir():
        if (path / "export-manifest.json").exists():
            return load_data_kit_adoption_export(path)
        candidates = sorted(path.glob("*.json")) + sorted(path.glob("*.jsonl"))
        rows: list[dict[str, Any]] = []
        for candidate in candidates:
            rows.extend(load_inventory(candidate))
        return tuple(rows)
    text = path.read_text(encoding="utf-8")
    if path.suffix.casefold() == ".jsonl":
        values = [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        decoded = json.loads(text)
        values = decoded if isinstance(decoded, list) else [decoded]
    if not all(isinstance(value, dict) for value in values):
        raise AdoptionError("adoption inventory entries must be JSON objects")
    if any(
        value.get("schema_version") == DATA_KIT_INVENTORY_SCHEMA_V2
        or value.get("policy_version") == LEGACY_ADOPTION_POLICY_V2
        for value in values
    ):
        raise AdoptionError("v2 adoption requires its sealed export directory, not bare JSONL")
    return tuple(values)


def _verify_legacy_bundle(root: Path) -> dict[str, Any]:
    allowed_top = {
        "READY",
        "bundle-manifest.json",
        "checksums.sha256",
        "manifests",
        "objects",
        "records",
        "reports",
    }
    if {path.name for path in root.iterdir()} != allowed_top:
        raise AdoptionError("legacy bundle has unexpected or missing top-level entries")
    for path in root.rglob("*"):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise AdoptionError("legacy bundle contains a symlink or special node")
    manifest_path = root / "bundle-manifest.json"
    checksums_path = root / "checksums.sha256"
    ready_path = root / "READY"
    if not all(path.is_file() for path in (manifest_path, checksums_path, ready_path)):
        raise AdoptionError("legacy prepare bundle is missing sealed control files")
    try:
        manifest = json.loads(manifest_path.read_bytes())
        ready = json.loads(ready_path.read_bytes())
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise AdoptionError("legacy bundle control JSON is invalid") from exc
    if not isinstance(manifest, dict) or not isinstance(ready, dict):
        raise AdoptionError("legacy bundle controls must be JSON objects")
    manifest_fields = {
        "schema_version",
        "bundle_id",
        "content_sha256",
        "source_manifest_sha256",
        "document_count",
        "adopted_count",
        "reocr_count",
        "unique_pdf_objects",
        "unique_ocr_objects",
        "unique_metadata_objects",
        "payload_bytes",
        "documents_manifest",
        "source_files_manifest",
        "exceptions_manifest",
        "adoption_policy",
    }
    ready_fields = {
        "schema_version",
        "bundle_id",
        "content_sha256",
        "bundle_manifest_sha256",
        "checksums_sha256",
    }
    if set(manifest) != manifest_fields or set(ready) != ready_fields:
        raise AdoptionError("legacy bundle control schema has missing or extra fields")
    if (
        manifest_path.read_bytes() != canonical_json_bytes(manifest) + b"\n"
        or ready_path.read_bytes() != canonical_json_bytes(ready) + b"\n"
    ):
        raise AdoptionError("legacy bundle control JSON is not canonical")
    content_sha = str(manifest.get("content_sha256") or "")
    bundle_id = str(manifest.get("bundle_id") or "")
    count_fields = (
        "document_count",
        "adopted_count",
        "reocr_count",
        "unique_pdf_objects",
        "unique_ocr_objects",
        "unique_metadata_objects",
        "payload_bytes",
    )
    if (
        manifest.get("schema_version") != "cardrag.legacy-bundle.v1"
        or manifest.get("adoption_policy") != LEGACY_ADOPTION_POLICY_VERSION
        or _SHA256_RE.fullmatch(content_sha) is None
        or bundle_id != f"bundle-{content_sha[:12]}"
        or ready.get("schema_version") != "cardrag.legacy-bundle-ready.v1"
        or ready.get("bundle_id") != bundle_id
        or ready.get("content_sha256") != content_sha
        or ready.get("bundle_manifest_sha256") != _file_sha256(manifest_path)
        or ready.get("checksums_sha256") != _file_sha256(checksums_path)
        or _SHA256_RE.fullmatch(str(manifest.get("source_manifest_sha256") or "")) is None
        or any(
            not isinstance(manifest.get(field), int)
            or isinstance(manifest.get(field), bool)
            or int(manifest[field]) < 0
            for field in count_fields
        )
        or int(manifest["adopted_count"]) + int(manifest["reocr_count"]) != int(manifest["document_count"])
    ):
        raise AdoptionError("legacy READY/manifest/checksum identity is not bound")

    expected_paths: dict[str, str] = {}
    checksum_lines = checksums_path.read_text(encoding="utf-8").splitlines()
    checksum_paths: list[str] = []
    for line in checksum_lines:
        parts = line.split("  ", 1)
        if len(parts) != 2 or _SHA256_RE.fullmatch(parts[0]) is None:
            raise AdoptionError("legacy checksums manifest has an invalid row")
        relative = Path(parts[1])
        if relative.is_absolute() or ".." in relative.parts or relative.as_posix() != parts[1]:
            raise AdoptionError("legacy checksums manifest has an unsafe path")
        if parts[1] in expected_paths:
            raise AdoptionError("legacy checksums manifest has a duplicate path")
        checksum_paths.append(parts[1])
        target = root / relative
        _reject_symlink_components(target, field="legacy checksum path")
        resolved = target.resolve(strict=True)
        if not resolved.is_relative_to(root) or not resolved.is_file():
            raise AdoptionError("legacy checksum path escapes its bundle")
        if _file_sha256(resolved) != parts[0]:
            raise AdoptionError(f"legacy bundle checksum mismatch: {parts[1]}")
        expected_paths[parts[1]] = parts[0]
    if checksum_paths != sorted(checksum_paths):
        raise AdoptionError("legacy checksums manifest is not deterministically sorted")
    actual_paths = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path.name not in {"checksums.sha256", "READY"}
    }
    if actual_paths != set(expected_paths):
        raise AdoptionError("legacy bundle contains unchecked or missing files")

    manifest_names = {
        "documents_manifest": "manifests/documents.jsonl",
        "source_files_manifest": "manifests/source-files.jsonl",
        "exceptions_manifest": "manifests/exceptions.jsonl",
    }
    for field, expected in manifest_names.items():
        if manifest.get(field) != expected or expected not in expected_paths:
            raise AdoptionError(f"legacy bundle has an invalid {field}")
    object_rows: list[dict[str, object]] = []
    object_counts: dict[str, int] = {"metadata": 0, "ocr": 0, "pdf": 0}
    payload_bytes = 0
    for kind, suffix in (("metadata", ".json"), ("ocr", ".md"), ("pdf", ".pdf")):
        object_root = root / "objects" / kind / "sha256"
        declared_count = int(manifest[f"unique_{kind}_objects"])
        if declared_count and not object_root.is_dir():
            raise AdoptionError(f"legacy bundle is missing {kind} object root")
        for path in sorted(object_root.glob(f"*/*{suffix}")):
            digest = path.stem
            if (
                _SHA256_RE.fullmatch(digest) is None
                or path.parent.name != digest[:2]
                or _file_sha256(path) != digest
            ):
                raise AdoptionError("legacy object path/content address mismatch")
            object_rows.append({"kind": kind, "sha256": digest, "size_bytes": path.stat().st_size})
            object_counts[kind] += 1
            payload_bytes += path.stat().st_size
    if (
        object_counts["pdf"] != manifest.get("unique_pdf_objects")
        or object_counts["ocr"] != manifest.get("unique_ocr_objects")
        or object_counts["metadata"] != manifest.get("unique_metadata_objects")
        or payload_bytes != manifest.get("payload_bytes")
    ):
        raise AdoptionError("legacy bundle object counts/bytes disagree")
    content_spec = {
        "adoption_policy": manifest["adoption_policy"],
        "documents_sha256": _file_sha256(root / manifest_names["documents_manifest"]),
        "exceptions_sha256": _file_sha256(root / manifest_names["exceptions_manifest"]),
        "objects": sorted(object_rows, key=lambda row: (str(row["kind"]), str(row["sha256"]))),
        "schema_version": manifest["schema_version"],
        "source_files_sha256": _file_sha256(root / manifest_names["source_files_manifest"]),
        "source_manifest_sha256": str(manifest.get("source_manifest_sha256") or ""),
    }
    if canonical_sha256(content_spec) != content_sha:
        raise AdoptionError("legacy bundle content identity is not reproducible")
    document_lines = (root / manifest_names["documents_manifest"]).read_text(encoding="utf-8").splitlines()
    documents = [json.loads(line) for line in document_lines if line]
    if not all(isinstance(row, dict) for row in documents):
        raise AdoptionError("legacy documents manifest contains a non-object row")
    if any(
        line.encode("utf-8") != canonical_json_bytes(row)
        for line, row in zip(document_lines, documents, strict=True)
    ):
        raise AdoptionError("legacy documents manifest JSON is not canonical")
    document_ids: set[str] = set()
    document_keys: set[str] = set()
    document_fields = {
        "document_id",
        "document_key",
        "issuer",
        "product_code",
        "product_name",
        "document_type",
        "effective_date",
        "source_version",
        "version_sort_key",
        "source_url",
        "source_post_id",
        "file_name",
        "discovered_at",
        "is_latest",
        "pdf_sha256",
        "pdf_size_bytes",
        "pdf_page_count",
        "pdf_object_path",
        "ocr_sha256",
        "ocr_size_bytes",
        "ocr_object_path",
        "metadata_sha256",
        "metadata_object_path",
        "metadata_schema",
        "adoption_status",
        "adoption_reason",
        "mapping_method",
        "source_pdf_path",
        "source_ocr_path",
        "source_metadata_path",
        "warnings",
    }
    for row in documents:
        if set(row) != document_fields:
            raise AdoptionError("legacy documents manifest row has missing or extra fields")
        if (
            not isinstance(row["is_latest"], bool)
            or row["adoption_status"] not in {"adopted", "reocr"}
            or row["mapping_method"] not in {"direct_path_and_hash", "hash_lookup"}
            or not isinstance(row["pdf_size_bytes"], int)
            or isinstance(row["pdf_size_bytes"], bool)
            or int(row["pdf_size_bytes"]) < 1
            or not isinstance(row["pdf_page_count"], int)
            or isinstance(row["pdf_page_count"], bool)
            or int(row["pdf_page_count"]) < 1
            or not isinstance(row["warnings"], list)
        ):
            raise AdoptionError("legacy documents manifest row has invalid strict field types")
        document_id = str(row["document_id"])
        document_key = str(row["document_key"])
        if document_id in document_ids or document_key in document_keys:
            raise AdoptionError("legacy documents manifest has duplicate identity")
        document_ids.add(document_id)
        document_keys.add(document_key)
        for kind, suffix in (("pdf", ".pdf"), ("ocr", ".md"), ("metadata", ".json")):
            digest_value = row.get(f"{kind}_sha256")
            path_value = row.get(f"{kind}_object_path")
            if digest_value is None and path_value is None:
                continue
            digest = str(digest_value or "")
            expected_path = f"objects/{kind}/sha256/{digest[:2]}/{digest}{suffix}"
            if _SHA256_RE.fullmatch(digest) is None or path_value != expected_path:
                raise AdoptionError(f"legacy document {document_id} has invalid {kind} reference")
            artifact_path = root / expected_path
            if not artifact_path.is_file() or _file_sha256(artifact_path) != digest:
                raise AdoptionError(f"legacy document {document_id} {kind} object is missing/corrupt")
            declared_size = row.get(f"{kind}_size_bytes")
            if declared_size is not None and declared_size != artifact_path.stat().st_size:
                raise AdoptionError(f"legacy document {document_id} {kind} size is unbound")
    adopted_count = sum(row.get("adoption_status") == "adopted" for row in documents)
    if (
        len(documents) != manifest.get("document_count")
        or adopted_count != manifest.get("adopted_count")
        or len(documents) - adopted_count != manifest.get("reocr_count")
    ):
        raise AdoptionError("legacy bundle document disposition counts disagree")

    source_lines = (root / manifest_names["source_files_manifest"]).read_text(encoding="utf-8").splitlines()
    source_rows = [json.loads(line) for line in source_lines if line]
    if any(
        not isinstance(row, dict) or line.encode("utf-8") != canonical_json_bytes(row)
        for line, row in zip(source_lines, source_rows, strict=True)
    ):
        raise AdoptionError("legacy source-files manifest is invalid or non-canonical")
    source_paths = [str(row.get("relative_path") or "") for row in source_rows]
    if source_paths != sorted(source_paths) or len(source_paths) != len(set(source_paths)):
        raise AdoptionError("legacy source-files paths are not sorted and unique")

    exception_lines = (root / manifest_names["exceptions_manifest"]).read_text(encoding="utf-8").splitlines()
    exception_rows = [json.loads(line) for line in exception_lines if line]
    if any(
        not isinstance(row, dict) or line.encode("utf-8") != canonical_json_bytes(row)
        for line, row in zip(exception_lines, exception_rows, strict=True)
    ):
        raise AdoptionError("legacy exceptions manifest is invalid or non-canonical")
    reocr_keys = {str(row["document_key"]) for row in documents if row["adoption_status"] == "reocr"}
    if {str(row.get("document_key") or "") for row in exception_rows} != reocr_keys:
        raise AdoptionError("legacy exceptions do not exactly account for re-OCR documents")

    record_rows: dict[str, dict[str, Any]] = {}
    for record_path in sorted((root / "records").rglob("record.json")):
        raw = record_path.read_bytes()
        try:
            record = json.loads(raw)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise AdoptionError("legacy record JSON is invalid") from exc
        if not isinstance(record, dict) or raw != canonical_json_bytes(record) + b"\n":
            raise AdoptionError("legacy record JSON is not canonical")
        record_id = str(record.get("document_id") or "")
        if record_id in record_rows:
            raise AdoptionError("legacy records contain a duplicate document")
        record_rows[record_id] = record
    if set(record_rows) != document_ids or any(
        record_rows[str(row["document_id"])] != row for row in documents
    ):
        raise AdoptionError("legacy records do not exactly mirror the documents manifest")

    expected_directories = {"manifests", "objects", "records", "reports"}
    for relative_name in expected_paths:
        parent = Path(relative_name).parent
        while parent != Path("."):
            expected_directories.add(parent.as_posix())
            parent = parent.parent
    actual_directories = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_dir()}
    if actual_directories != expected_directories:
        raise AdoptionError("legacy bundle contains an unlisted or missing directory")
    return manifest


def load_legacy_prepare_bundle(bundle_root: Path, ledger_path: Path) -> tuple[dict[str, Any], ...]:
    """Adapt v0.2.1 `legacy prepare` plus an exported DB-bound ledger, read-only."""

    _reject_symlink_components(bundle_root, field="legacy prepare bundle")
    root = bundle_root.resolve(strict=True)
    if not root.is_dir():
        raise AdoptionError("legacy prepare bundle must be a non-symlink directory")
    documents_path = root / "manifests" / "documents.jsonl"
    manifest = _verify_legacy_bundle(root)
    ledger_rows = load_inventory(ledger_path)
    ledger_by_document: dict[str, dict[str, Any]] = {}
    for ledger in ledger_rows:
        document_id = str(ledger.get("source_document_id") or ledger.get("document_id") or "")
        if not document_id:
            raise AdoptionError("legacy ledger row has no source_document_id")
        if document_id in ledger_by_document:
            raise AdoptionError(f"duplicate legacy ledger row for {document_id}")
        status = str(ledger.get("status") or ledger.get("state") or "").casefold()
        if status != "succeeded":
            continue
        if str(ledger.get("disposition") or "").casefold() != "adopted":
            continue
        if ledger.get("schema_version") != LEGACY_LEDGER_SCHEMA:
            raise AdoptionError(f"legacy ledger row {document_id} has an unsupported schema")
        required = (
            "source_database_id",
            "import_id",
            "bundle_id",
            "bundle_sha256",
            "generation_id",
            "document_key",
            "issuer",
            "pdf_sha256",
            "ocr_sha256",
        )
        if any(not str(ledger.get(field) or "") for field in required):
            raise AdoptionError(f"successful legacy ledger row {document_id} is incomplete")
        ledger_by_document[document_id] = ledger
    normalized: list[dict[str, Any]] = []
    for line in documents_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        document = json.loads(line)
        if not document.get("is_latest") or document.get("adoption_status") != "adopted":
            continue
        document_id = str(document.get("document_id") or "")
        matched_ledger = ledger_by_document.get(document_id)
        if matched_ledger is None:
            raise AdoptionError(f"legacy document {document_id} has no successful exported ledger row")
        if (
            str(matched_ledger["bundle_id"]) != str(manifest.get("bundle_id"))
            or str(matched_ledger["bundle_sha256"]) != str(manifest.get("content_sha256"))
            or str(matched_ledger["document_key"]) != str(document.get("document_key") or "")
            or str(matched_ledger["issuer"]) != str(document.get("issuer") or "")
            or str(matched_ledger.get("pdf_sha256") or "") != str(document.get("pdf_sha256") or "")
            or str(matched_ledger.get("ocr_sha256") or "") != str(document.get("ocr_sha256") or "")
        ):
            raise AdoptionError(f"legacy ledger identity mismatch for {document_id}")

        def bound_path(field: str, candidate: Mapping[str, Any] = document) -> Path:
            relative = Path(str(candidate.get(field) or ""))
            if relative.is_absolute() or ".." in relative.parts:
                raise AdoptionError(f"unsafe legacy {field}")
            raw_target = root / relative
            _reject_symlink_components(raw_target, field=f"legacy {field}")
            target = raw_target.resolve(strict=True)
            if not target.is_relative_to(root) or not target.is_file():
                raise AdoptionError(f"legacy {field} escapes the sealed bundle")
            return target

        normalized.append(
            {
                "issuer": str(document.get("issuer") or ""),
                "product_code": str(document.get("product_code") or ""),
                "source_bundle_id": str(manifest["bundle_id"]),
                "source_bundle_sha256": str(manifest["content_sha256"]),
                "source_database_id": str(
                    matched_ledger.get("source_database_id") or matched_ledger["import_id"]
                ),
                "source_document_id": document_id,
                "legacy_source_document_id": document_id,
                "document_type": str(document.get("document_type") or "product_description"),
                "effective_date": str(document.get("effective_date") or ""),
                "source_version": str(document.get("source_version") or ""),
                "pdf_path": str(bound_path("pdf_object_path")),
                "ocr_path": str(bound_path("ocr_object_path")),
                "ledger_pdf_sha256": str(matched_ledger["pdf_sha256"]),
                "ledger_ocr_sha256": str(matched_ledger["ocr_sha256"]),
            }
        )
    return tuple(normalized)


@dataclass(frozen=True, slots=True)
class AdoptionConflict:
    source_document_id: str
    reason: str
    candidates: tuple[Mapping[str, str], ...]
    kind: Literal["ambiguous_source", "current_over_legacy"] = "ambiguous_source"
    blocking: bool = True


@dataclass(frozen=True, slots=True)
class AdoptionResult:
    receipts: tuple[dict[str, Any], ...]
    conflicts: tuple[AdoptionConflict, ...]
    errors: tuple[dict[str, str], ...]


def _source_bundle_sha(row: Mapping[str, Any]) -> str:
    if row.get("source_bundle_path"):
        digest = _file_sha256(_regular_file(row["source_bundle_path"], field="source_bundle_path"))
        declared = str(row.get("source_bundle_sha256") or "")
        if declared and declared != digest:
            raise AdoptionError("source bundle hash differs from inventory ledger")
        return digest
    digest = str(row.get("source_bundle_sha256") or "")
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise AdoptionError("source_bundle_sha256 is required when no bundle path is supplied")
    return digest


def _v1_document_id(row: Mapping[str, Any], *, pdf_sha256: str) -> str:
    identity_fields = (
        str(row.get("issuer") or ""),
        str(row.get("product_code") or ""),
        str(row.get("document_type") or "product_description"),
        str(row.get("effective_date") or ""),
        str(row.get("source_version") or ""),
    )
    if all(identity_fields):
        issuer, product_code, document_type, effective_date, source_version = identity_fields
        computed = "doc_" + canonical_sha256(
            {
                "document_type": document_type,
                "effective_date": effective_date,
                "issuer": issuer,
                "pdf_sha256": pdf_sha256,
                "product_code": product_code,
                "version": source_version,
            }
        )
        declared = str(row.get("v1_document_id") or "")
        if declared and declared != computed:
            raise AdoptionError("declared v1_document_id differs from normalized future identity")
        return computed
    declared = str(row.get("v1_document_id") or row.get("source_document_id") or "")
    if not declared.startswith("doc_"):
        raise AdoptionError(
            "adoption inventory must provide normalized identity fields or exact v1_document_id"
        )
    return declared


_V2_INVENTORY_FIELDS = {
    "schema_version",
    "policy_version",
    "issuer",
    "product_code",
    "source_database_id",
    "source_data_pack_manifest_sha256",
    "source_inventory_sha256",
    "source_master_manifest_sha256",
    "source_ocr_inventory_sha256",
    "source_bundle_id",
    "source_bundle_sha256",
    "source_document_id",
    "legacy_source_document_id",
    "document_type",
    "effective_date",
    "source_version",
    "pdf_path",
    "source_ocr_path",
    "ocr_path",
    "ledger_pdf_sha256",
    "pdf_size_bytes",
    "page_count",
    "ledger_ocr_sha256",
    "source_ocr_sha256",
    "source_ocr_size_bytes",
    "normalized_ocr_sha256",
    "normalized_ocr_size_bytes",
    "normalization_profile",
    "prefix_sha256",
    "removed_bytes",
}


def _strict_positive_int(row: Mapping[str, Any], field: str) -> int:
    value = row.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise AdoptionError(f"v2 inventory {field} must be a positive integer")
    return value


def _strict_nonnegative_int(row: Mapping[str, Any], field: str) -> int:
    value = row.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise AdoptionError(f"v2 inventory {field} must be a non-negative integer")
    return value


def _declared_sha256(row: Mapping[str, Any], field: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise AdoptionError(f"v2 inventory {field} must be a lowercase SHA-256")
    return value


def _absolute_regular_file(row: Mapping[str, Any], field: str) -> Path:
    value = row.get(field)
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise AdoptionError(f"v2 inventory {field} must be an absolute path")
    return _regular_file(value, field=field)


def _validate_v2_inventory_shape(row: Mapping[str, Any]) -> None:
    if set(row) != _V2_INVENTORY_FIELDS:
        raise AdoptionError("v2 adoption inventory has missing or extra fields")
    if row.get("schema_version") != DATA_KIT_INVENTORY_SCHEMA_V2:
        raise AdoptionError("v2 adoption inventory schema is invalid")
    if row.get("policy_version") != LEGACY_ADOPTION_POLICY_V2:
        raise AdoptionError("v2 adoption inventory policy is invalid")
    for field in (
        "issuer",
        "product_code",
        "source_database_id",
        "source_bundle_id",
        "source_document_id",
        "legacy_source_document_id",
        "document_type",
        "effective_date",
        "source_version",
    ):
        if not isinstance(row.get(field), str) or not str(row[field]).strip():
            raise AdoptionError(f"v2 inventory {field} must be non-empty text")
    for field in (
        "source_data_pack_manifest_sha256",
        "source_inventory_sha256",
        "source_master_manifest_sha256",
        "source_ocr_inventory_sha256",
        "source_bundle_sha256",
        "ledger_pdf_sha256",
        "ledger_ocr_sha256",
        "source_ocr_sha256",
        "normalized_ocr_sha256",
    ):
        _declared_sha256(row, field)
    if row["ledger_ocr_sha256"] != row["source_ocr_sha256"]:
        raise AdoptionError("v2 source OCR hash is not bound to its original ledger hash")
    if row["legacy_source_document_id"] != row["source_document_id"]:
        raise AdoptionError("v2 legacy/source document identities differ")
    if row["source_inventory_sha256"] != row["source_ocr_inventory_sha256"]:
        raise AdoptionError("v2 inventory and OCR inventory control hashes differ")
    if row["source_database_id"] != (f"data-kit-sqlite-v2-sha256:{row['source_inventory_sha256']}"):
        raise AdoptionError("v2 source database identity does not match inventory SHA-256")
    _strict_positive_int(row, "pdf_size_bytes")
    _strict_positive_int(row, "page_count")
    _strict_positive_int(row, "source_ocr_size_bytes")
    _strict_positive_int(row, "normalized_ocr_size_bytes")
    _strict_nonnegative_int(row, "removed_bytes")


def _verify_v2_transformation(
    row: Mapping[str, Any],
    *,
    source_body: bytes,
    normalized_body: bytes,
) -> None:
    source_sha = hashlib.sha256(source_body).hexdigest()
    normalized_sha = hashlib.sha256(normalized_body).hexdigest()
    if source_sha != row["source_ocr_sha256"] or len(source_body) != row["source_ocr_size_bytes"]:
        raise AdoptionError("original source OCR bytes differ from the v2 inventory")
    if (
        normalized_sha != row["normalized_ocr_sha256"]
        or len(normalized_body) != row["normalized_ocr_size_bytes"]
    ):
        raise AdoptionError("normalized OCR bytes differ from the v2 inventory")
    profile = row["normalization_profile"]
    if profile == LEGACY_OCR_NORMALIZATION_EXACT:
        if (
            source_body != normalized_body
            or source_sha != normalized_sha
            or row["prefix_sha256"] is not None
            or row["removed_bytes"] != 0
        ):
            raise AdoptionError("exact v2 adoption changed the source OCR bytes")
        return
    if profile != LEGACY_OCR_NORMALIZATION_STRIP_PREFIX_V1:
        raise AdoptionError("v2 adoption requested an unsupported normalization")
    if row["prefix_sha256"] != LEGACY_OCR_APPROVED_PREFIX_SHA256 or row["removed_bytes"] != len(
        LEGACY_OCR_APPROVED_PREFIX
    ):
        raise AdoptionError("v2 adoption prefix proof is not the approved exact 24-byte prefix")
    if source_body != LEGACY_OCR_APPROVED_PREFIX + normalized_body:
        raise AdoptionError("source OCR is not exactly approved-prefix plus normalized OCR")
    if source_sha == normalized_sha:
        raise AdoptionError("prefix-strip v2 adoption did not change the OCR identity")


def _receipt_output_sha256(receipt: Mapping[str, Any]) -> str:
    value = receipt.get("normalized_ocr_sha256", receipt.get("ocr_sha256", ""))
    return str(value)


def _receipt_source_sha256(receipt: Mapping[str, Any]) -> str:
    value = receipt.get("source_ocr_sha256", receipt.get("ocr_sha256", ""))
    return str(value)


def _validate_candidate_v1(
    row: Mapping[str, Any], *, source_kind: Literal["legacy", "current"]
) -> dict[str, Any]:
    pdf_path = _regular_file(row.get("pdf_path"), field="pdf_path")
    ocr_path = _regular_file(row.get("ocr_path"), field="ocr_path")
    pdf_sha, pdf_size, pages = _validate_pdf_stable(pdf_path)
    declared_pdf = str(row.get("ledger_pdf_sha256") or "")
    if declared_pdf != pdf_sha:
        raise AdoptionError("PDF bytes are not bound to the source ledger hash")
    ocr_body = ocr_path.read_bytes()
    declared_ocr = str(row.get("ledger_ocr_sha256") or "")
    verified = verify_ocr_bytes(
        ocr_body,
        expected_page_count=pages,
        expected_sha256=declared_ocr,
    )
    source_document_id = _v1_document_id(row, pdf_sha256=pdf_sha)
    receipt = LegacyAdoptionReceipt(
        adoption_policy_version=LEGACY_ADOPTION_POLICY_VERSION,
        source_bundle_id=str(row.get("source_bundle_id") or ""),
        source_bundle_sha256=_source_bundle_sha(row),
        source_database_id=str(row.get("source_database_id") or source_kind),
        source_document_id=source_document_id,
        pdf_sha256=pdf_sha,
        ocr_sha256=verified.sha256,
        validation=LegacyAdoptionValidation(
            hash_verified=True,
            page_coverage_verified=True,
            utf8_verified=True,
            ledger_bound=True,
        ),
    )
    source = OCRInput(pdf_sha256=pdf_sha, pdf_size_bytes=pdf_size, page_count=pages)
    reuse_key = adopted_ocr_reuse_key(
        adoption_policy_version=LEGACY_ADOPTION_POLICY_VERSION,
        source_document_id=source_document_id,
        pdf_sha256=pdf_sha,
    )
    output = ArtifactRef.for_cas(
        sha256=verified.sha256,
        size_bytes=verified.size_bytes,
        media_type="text/markdown; charset=utf-8",
    )
    manifest = AdoptedOCRArtifactManifest(
        reuse_key=reuse_key,
        source=source,
        receipt=receipt,
        output=output,
        ocr_chars=verified.char_count,
        page_output_sha256=verified.page_sha256,
        # The immutable control object must be byte-identical across interrupted
        # or completed migration reruns. The receipt carries the real source IDs.
        created_at=ADOPTION_CONTROL_EPOCH,
    )
    return {
        "source_kind": source_kind,
        "legacy_source_document_id": str(
            row.get("legacy_source_document_id") or row.get("source_document_id") or ""
        ),
        "issuer": str(row.get("issuer") or ""),
        "product_code": str(row.get("product_code") or ""),
        "ocr_path": str(ocr_path),
        "pdf_path": str(pdf_path),
        "receipt": receipt.model_dump(mode="json"),
        "manifest": manifest.model_dump(mode="json"),
    }


def _validate_candidate_v2(
    row: Mapping[str, Any], *, source_kind: Literal["legacy", "current"]
) -> dict[str, Any]:
    _validate_v2_inventory_shape(row)
    pdf_path = _absolute_regular_file(row, "pdf_path")
    source_ocr_path = _absolute_regular_file(row, "source_ocr_path")
    ocr_path = _absolute_regular_file(row, "ocr_path")
    pdf_sha, pdf_size, pages = _validate_pdf_stable(pdf_path)
    if pdf_sha != row["ledger_pdf_sha256"]:
        raise AdoptionError("PDF bytes are not bound to the source ledger hash")
    if pdf_size != row["pdf_size_bytes"] or pages != row["page_count"]:
        raise AdoptionError("PDF size/page count differs from the v2 inventory")
    source_ocr_body = _stable_file_bytes(source_ocr_path, field="source_ocr_path")
    normalized_ocr_body = _stable_file_bytes(ocr_path, field="ocr_path")
    _verify_v2_transformation(
        row,
        source_body=source_ocr_body,
        normalized_body=normalized_ocr_body,
    )
    verified = verify_ocr_bytes(
        normalized_ocr_body,
        expected_page_count=pages,
        expected_sha256=str(row["normalized_ocr_sha256"]),
        expected_size_bytes=int(row["normalized_ocr_size_bytes"]),
    )
    source_document_id = _v1_document_id(row, pdf_sha256=pdf_sha)
    normalization_profile = cast(
        Literal["exact", "strip-exact-generated-prefix-v1"],
        row["normalization_profile"],
    )
    receipt = LegacyAdoptionReceiptV2(
        source_bundle_id=str(row["source_bundle_id"]),
        source_bundle_sha256=_source_bundle_sha(row),
        source_database_id=str(row["source_database_id"]),
        source_document_id=source_document_id,
        pdf_sha256=pdf_sha,
        source_ocr_sha256=str(row["source_ocr_sha256"]),
        source_ocr_size_bytes=int(row["source_ocr_size_bytes"]),
        normalized_ocr_sha256=verified.sha256,
        normalized_ocr_size_bytes=verified.size_bytes,
        normalization_profile=normalization_profile,
        prefix_sha256=(None if row["prefix_sha256"] is None else str(row["prefix_sha256"])),
        removed_bytes=int(row["removed_bytes"]),
        validation=LegacyAdoptionValidationV2(
            source_hash_verified=True,
            normalized_hash_verified=True,
            transformation_verified=True,
            page_coverage_verified=True,
            utf8_verified=True,
            ledger_bound=True,
        ),
    )
    source = OCRInput(pdf_sha256=pdf_sha, pdf_size_bytes=pdf_size, page_count=pages)
    reuse_key = adopted_ocr_reuse_key(
        adoption_policy_version=LEGACY_ADOPTION_POLICY_V2,
        source_document_id=source_document_id,
        pdf_sha256=pdf_sha,
    )
    output = ArtifactRef.for_cas(
        sha256=verified.sha256,
        size_bytes=verified.size_bytes,
        media_type="text/markdown; charset=utf-8",
    )
    manifest = AdoptedOCRArtifactManifest(
        schema_version="cardrag.ocr-artifact.v2",
        validation_profile=LEGACY_ADOPTION_POLICY_V2,
        reuse_key=reuse_key,
        source=source,
        receipt=receipt,
        output=output,
        ocr_chars=verified.char_count,
        page_output_sha256=verified.page_sha256,
        created_at=ADOPTION_CONTROL_EPOCH,
    )
    return {
        "source_kind": source_kind,
        "legacy_source_document_id": str(row["legacy_source_document_id"]),
        "issuer": str(row["issuer"]),
        "product_code": str(row["product_code"]),
        "source_ocr_path": str(source_ocr_path),
        "ocr_path": str(ocr_path),
        "pdf_path": str(pdf_path),
        "normalization_profile": str(row["normalization_profile"]),
        "receipt": receipt.model_dump(mode="json"),
        "manifest": manifest.model_dump(mode="json"),
    }


def validate_candidate(
    row: Mapping[str, Any], *, source_kind: Literal["legacy", "current"]
) -> dict[str, Any]:
    if row.get("schema_version") == DATA_KIT_INVENTORY_SCHEMA_V2:
        return _validate_candidate_v2(row, source_kind=source_kind)
    if row.get("policy_version") == LEGACY_ADOPTION_POLICY_V2:
        raise AdoptionError("v2 adoption inventory schema is invalid")
    return _validate_candidate_v1(row, source_kind=source_kind)


def validate_inventory(
    rows: Sequence[Mapping[str, Any]], *, source_kind: Literal["legacy", "current"]
) -> AdoptionResult:
    validated: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for index, row in enumerate(rows):
        try:
            validated.append(validate_candidate(row, source_kind=source_kind))
        except Exception as exc:
            errors.append(
                {
                    "index": str(index),
                    "source_document_id": str(row.get("source_document_id") or ""),
                    "policy_version": str(row.get("policy_version") or ""),
                    "error": str(exc),
                }
            )
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in validated:
        document_id = str(row["receipt"]["source_document_id"])
        grouped.setdefault(document_id, []).append(row)
    conflicts: list[AdoptionConflict] = []
    accepted: list[dict[str, Any]] = []
    for document_id, candidates in sorted(grouped.items()):
        identities = {
            (
                str(row["receipt"]["pdf_sha256"]),
                _receipt_source_sha256(row["receipt"]),
                _receipt_output_sha256(row["receipt"]),
            )
            for row in candidates
        }
        if len(identities) > 1:
            conflicts.append(
                AdoptionConflict(
                    source_document_id=document_id,
                    reason="same source_document_id has conflicting PDF/OCR bytes",
                    candidates=tuple(
                        {
                            "pdf_sha256": str(row["receipt"]["pdf_sha256"]),
                            "source_ocr_sha256": _receipt_source_sha256(row["receipt"]),
                            "ocr_sha256": _receipt_output_sha256(row["receipt"]),
                            "source_bundle_id": str(row["receipt"]["source_bundle_id"]),
                        }
                        for row in candidates
                    ),
                    kind="ambiguous_source",
                    blocking=True,
                )
            )
            continue
        accepted.append(sorted(candidates, key=lambda row: str(row["receipt"]["source_bundle_id"]))[0])
    return AdoptionResult(tuple(accepted), tuple(conflicts), tuple(errors))


def reconcile_inventories(
    current_rows: Sequence[Mapping[str, Any]],
    legacy_rows: Sequence[Mapping[str, Any]],
) -> AdoptionResult:
    """Current published identity wins; legacy only fills absent issuer/products."""

    current = validate_inventory(current_rows, source_kind="current")
    legacy = validate_inventory(legacy_rows, source_kind="legacy")
    conflicts = [*current.conflicts, *legacy.conflicts]
    accepted: dict[tuple[str, str], dict[str, Any]] = {}
    blocked: set[tuple[str, str]] = set()
    for row in current.receipts:
        identity = (str(row.get("issuer") or ""), str(row.get("product_code") or ""))
        if not all(identity):
            raise AdoptionError("current inventory requires issuer and product_code")
        previous = accepted.get(identity)
        if previous is None and identity not in blocked:
            accepted[identity] = row
        elif previous is not None and canonical_sha256(previous) != canonical_sha256(row):
            accepted.pop(identity, None)
            blocked.add(identity)
            conflicts.append(
                AdoptionConflict(
                    source_document_id=str(row["receipt"]["source_document_id"]),
                    reason=f"current inventory has multiple candidates for {identity[0]}/{identity[1]}",
                    candidates=(
                        {
                            "source_document_id": str(previous["receipt"]["source_document_id"]),
                            "pdf_sha256": str(previous["receipt"]["pdf_sha256"]),
                            "source_ocr_sha256": _receipt_source_sha256(previous["receipt"]),
                            "ocr_sha256": _receipt_output_sha256(previous["receipt"]),
                        },
                        {
                            "source_document_id": str(row["receipt"]["source_document_id"]),
                            "pdf_sha256": str(row["receipt"]["pdf_sha256"]),
                            "source_ocr_sha256": _receipt_source_sha256(row["receipt"]),
                            "ocr_sha256": _receipt_output_sha256(row["receipt"]),
                        },
                    ),
                    kind="ambiguous_source",
                    blocking=True,
                )
            )
    for row in legacy.receipts:
        identity = (str(row.get("issuer") or ""), str(row.get("product_code") or ""))
        if not all(identity):
            raise AdoptionError("legacy inventory requires issuer and product_code")
        if identity in blocked:
            continue
        current_row = accepted.get(identity)
        if current_row is None:
            accepted[identity] = row
            continue
        if current_row["source_kind"] == "legacy":
            if canonical_sha256(current_row) == canonical_sha256(row):
                continue
            accepted.pop(identity, None)
            blocked.add(identity)
            conflicts.append(
                AdoptionConflict(
                    source_document_id=str(row["receipt"]["source_document_id"]),
                    reason=f"legacy inventory has multiple candidates for {identity[0]}/{identity[1]}",
                    candidates=(
                        {
                            "source_document_id": str(current_row["receipt"]["source_document_id"]),
                            "pdf_sha256": str(current_row["receipt"]["pdf_sha256"]),
                            "source_ocr_sha256": _receipt_source_sha256(current_row["receipt"]),
                            "ocr_sha256": _receipt_output_sha256(current_row["receipt"]),
                        },
                        {
                            "source_document_id": str(row["receipt"]["source_document_id"]),
                            "pdf_sha256": str(row["receipt"]["pdf_sha256"]),
                            "source_ocr_sha256": _receipt_source_sha256(row["receipt"]),
                            "ocr_sha256": _receipt_output_sha256(row["receipt"]),
                        },
                    ),
                    kind="ambiguous_source",
                    blocking=True,
                )
            )
            continue
        current_identity = (
            str(current_row["receipt"]["source_document_id"]),
            str(current_row["receipt"]["pdf_sha256"]),
            _receipt_source_sha256(current_row["receipt"]),
            _receipt_output_sha256(current_row["receipt"]),
        )
        legacy_identity = (
            str(row["receipt"]["source_document_id"]),
            str(row["receipt"]["pdf_sha256"]),
            _receipt_source_sha256(row["receipt"]),
            _receipt_output_sha256(row["receipt"]),
        )
        if current_identity != legacy_identity:
            conflicts.append(
                AdoptionConflict(
                    source_document_id=current_identity[0],
                    reason=f"current identity overrides conflicting legacy identity for {identity[0]}/{identity[1]}",
                    candidates=(
                        {
                            "source": "current",
                            "source_document_id": current_identity[0],
                            "pdf_sha256": current_identity[1],
                            "source_ocr_sha256": current_identity[2],
                            "ocr_sha256": current_identity[3],
                        },
                        {
                            "source": "legacy",
                            "source_document_id": legacy_identity[0],
                            "pdf_sha256": legacy_identity[1],
                            "source_ocr_sha256": legacy_identity[2],
                            "ocr_sha256": legacy_identity[3],
                        },
                    ),
                    kind="current_over_legacy",
                    blocking=False,
                )
            )
    return AdoptionResult(
        receipts=tuple(accepted[key] for key in sorted(accepted)),
        conflicts=tuple(conflicts),
        errors=tuple([*current.errors, *legacy.errors]),
    )


def _atomic_json(path: Path, payload: object, *, lines: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    if lines:
        if not isinstance(payload, (list, tuple)):
            raise TypeError("JSON lines payload must be a list or tuple")
        body = "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
            for row in payload
        )
    else:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def write_reports(result: AdoptionResult, *, receipts: Path, conflicts: Path) -> None:
    _atomic_json(receipts, result.receipts, lines=True)
    _atomic_json(
        conflicts,
        {
            "schema_version": "cardrag.adoption-conflicts.v1",
            "conflicts": [
                {
                    "source_document_id": row.source_document_id,
                    "reason": row.reason,
                    "kind": row.kind,
                    "blocking": row.blocking,
                    "candidates": list(row.candidates),
                }
                for row in result.conflicts
            ],
            "errors": list(result.errors),
        },
    )


@dataclass(frozen=True, slots=True)
class _PreparedAdoption:
    manifest: AdoptedOCRArtifactManifest
    manifest_body: bytes
    ready: OCRReady
    ready_body: bytes
    ocr_body: bytes


def _prepare_adoptions(result: AdoptionResult) -> tuple[_PreparedAdoption, ...]:
    prepared: list[tuple[AdoptedOCRArtifactManifest, bytes]] = []
    for row in result.receipts:
        manifest = AdoptedOCRArtifactManifest.model_validate_json(
            json.dumps(row["manifest"], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )
        pdf_path = _regular_file(row["pdf_path"], field="pdf_path during publication")
        pdf_sha256, pdf_size_bytes, page_count = _validate_pdf_stable(pdf_path)
        if (
            pdf_sha256 != manifest.source.pdf_sha256
            or pdf_size_bytes != manifest.source.pdf_size_bytes
            or page_count != manifest.source.page_count
        ):
            raise AdoptionError("PDF changed after validation and before publication")
        ocr_path = _regular_file(row["ocr_path"], field="ocr_path during publication")
        ocr_body = _stable_file_bytes(ocr_path, field="OCR during publication")
        if isinstance(manifest.receipt, LegacyAdoptionReceiptV2):
            source_ocr_path = _regular_file(
                row.get("source_ocr_path"),
                field="source_ocr_path during publication",
            )
            source_ocr_body = _stable_file_bytes(
                source_ocr_path,
                field="source OCR during publication",
            )
            _verify_v2_transformation(
                manifest.receipt.model_dump(mode="python"),
                source_body=source_ocr_body,
                normalized_body=ocr_body,
            )
        try:
            verify_ocr_bytes(
                ocr_body,
                expected_page_count=manifest.source.page_count,
                expected_sha256=manifest.output.sha256,
                expected_size_bytes=manifest.output.size_bytes,
                expected_char_count=manifest.ocr_chars,
                expected_page_sha256=manifest.page_output_sha256,
            )
        except Exception as exc:
            raise AdoptionError("OCR changed after validation and before publication") from exc
        prepared.append((manifest, ocr_body))

    controls: list[_PreparedAdoption] = []
    for manifest, ocr_body in prepared:
        manifest_body = manifest.canonical_bytes()
        ready = OCRReady(
            reuse_key=manifest.reuse_key,
            manifest_sha256=hashlib.sha256(manifest_body).hexdigest(),
            ocr_sha256=manifest.output.sha256,
        )
        controls.append(
            _PreparedAdoption(
                manifest=manifest,
                manifest_body=manifest_body,
                ready=ready,
                ready_body=ready.canonical_bytes(),
                ocr_body=ocr_body,
            )
        )
    return tuple(controls)


async def guard_adoption_publication(webdav: WebDAVClient) -> None:
    """Fail unless the stable namespace is demonstrably unused, without reading its body."""

    if await webdav.exists(STABLE_POINTER_PATH):
        raise AdoptionError("stable generation pointer already exists; adoption publication is forbidden")


async def _require_exact_remote_bytes(
    webdav: WebDAVClient,
    *,
    path: str,
    expected: bytes,
    label: str,
) -> None:
    try:
        actual = await webdav.get_bytes(path, max_bytes=len(expected))
    except Exception as exc:
        raise AdoptionError(f"published {label} could not be read exactly") from exc
    expected_sha256 = hashlib.sha256(expected).hexdigest()
    if (
        actual is None
        or len(actual) != len(expected)
        or hashlib.sha256(actual).hexdigest() != expected_sha256
        or actual != expected
    ):
        raise AdoptionError(f"published {label} bytes, SHA-256, or size differ from the sealed export")


async def audit_published_adoptions(result: AdoptionResult, webdav: WebDAVClient) -> int:
    """Read back every v2 adoption control and OCR object against sealed local bytes."""

    if result.conflicts or result.errors:
        raise AdoptionError("refusing to audit an incomplete or conflicting adoption result")
    prepared = _prepare_adoptions(result)
    if not prepared:
        raise AdoptionError("v2 adoption audit requires at least one expected object")
    if any(not isinstance(item.manifest.receipt, LegacyAdoptionReceiptV2) for item in prepared):
        raise AdoptionError("published adoption audit accepts only sealed v2 exports")
    for item in prepared:
        manifest = item.manifest
        await _require_exact_remote_bytes(
            webdav,
            path=ocr_manifest_path(manifest.reuse_key, kind="adopted").as_posix(),
            expected=item.manifest_body,
            label="adoption manifest",
        )
        await _require_exact_remote_bytes(
            webdav,
            path=ocr_ready_path(manifest.reuse_key, kind="adopted").as_posix(),
            expected=item.ready_body,
            label="adoption READY",
        )
        await _require_exact_remote_bytes(
            webdav,
            path=manifest.output.path,
            expected=item.ocr_body,
            label="adopted OCR CAS object",
        )
    return len(prepared)


async def publish_adoptions(result: AdoptionResult, webdav: WebDAVClient) -> int:
    prepared = _prepare_adoptions(result)
    await guard_adoption_publication(webdav)

    published = 0
    for item in prepared:
        manifest = item.manifest
        object_sha, _ = await webdav.put_cas(
            item.ocr_body,
            media_type="text/markdown; charset=utf-8",
        )
        if object_sha != manifest.output.sha256:
            raise AdoptionError("OCR changed after validation and before publication")
        manifest_body = await webdav.put_json(
            ocr_manifest_path(manifest.reuse_key, kind="adopted"),
            manifest.model_dump(mode="json"),
            immutable=True,
        )
        if manifest_body != item.manifest_body:
            raise AdoptionError("published adoption manifest is not canonical")
        await webdav.put_json(
            ocr_ready_path(manifest.reuse_key, kind="adopted"),
            item.ready.model_dump(mode="json"),
            immutable=True,
        )
        published += 1
    return published
