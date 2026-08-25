"""Read-only validation and Worker inventory export for the 2026 legacy data-kit.

The released data-kit has no signed, complete file checksum manifest.  This
module therefore treats its small control files and byte-identical SQLite
catalogues as a snapshot identity, cross-checks their complete document ledger,
and revalidates the bytes of each selected latest PDF/OCR candidate.  It never
normalizes or writes back to the source tree.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cardrag.domain import canonical_json_bytes, canonical_sha256
from cardrag.pdf import PDFEngineError, open_pdf
from cardrag.storage.paths import atomic_write_bytes

DATA_KIT_INVENTORY_SCHEMA = "cardrag.data-kit-adoption-inventory.v1"
DATA_KIT_REJECTION_SCHEMA = "cardrag.data-kit-adoption-rejection.v1"
DATA_KIT_SOURCE_SCHEMA = "cardrag.data-kit-source.v1"

_DATA_PACK_RELATIVE = Path("DATA_PACK_MANIFEST.json")
_MASTER_MANIFEST_RELATIVE = Path("artifacts/manifests/cardrag_master_manifest.json")
_INVENTORY_RELATIVE = Path("data/db/inventory.sqlite3")
_OCR_INVENTORY_RELATIVE = Path("data/db/ocr_inventory.sqlite3")
_EXPECTED_INCLUDED_ROOTS = ["data/", "artifacts/", "reports/", "logs/", "outputs/"]
_ISSUER_MAP = {"wooricard": "woori", "kbcard": "kb"}
_ISSUER_CODE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,31}$")
_PRODUCT_CODE = re.compile(r"^[A-Za-z0-9_-]+$")
_DOCUMENT_TYPE = re.compile(r"^[a-z][a-z0-9_]*$")
_RELEASE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PAGE_MARKER = re.compile(r"^## Page ([1-9][0-9]*)$", re.MULTILINE)

_DOCUMENT_COLUMNS = (
    "doc_id",
    "card_company",
    "product_code",
    "product_name",
    "doc_type",
    "effective_date",
    "version",
    "file_name",
    "status",
    "output_base_rel",
    "guide_remote_rel",
    "ocr_remote_rel",
    "metadata_remote_rel",
    "pages",
    "guide_chars",
    "ocr_chars",
    "completed_at",
    "error",
    "is_latest",
)
_INDEX_STATE_COLUMNS = ("doc_id", "state", "last_error", "updated_at")
_MASTER_TO_DATABASE = {
    "doc_version_id": "doc_id",
    "cardCompany": "card_company",
    "productCode": "product_code",
    "productName": "product_name",
    "docType": "doc_type",
    "beginDt": "effective_date",
    "gdccVer": "version",
    "fileNm": "file_name",
    "status": "status",
    "output_base_rel": "output_base_rel",
    "guide_remote_rel": "guide_remote_rel",
    "ocr_remote_rel": "ocr_remote_rel",
    "metadata_remote_rel": "metadata_remote_rel",
    "pages": "pages",
    "guide_chars": "guide_chars",
    "ocr_chars": "ocr_chars",
    "completed_at": "completed_at",
    "error": "error",
}
_MASTER_REQUIRED = frozenset(_MASTER_TO_DATABASE) | {"pdf_sha256", "sourceUrl", "sourcePostId"}
_MASTER_ALLOWED = _MASTER_REQUIRED | {
    "fileSize",
    "pdf_fingerprint",
    "raw_pdf_rel_path",
}


class DataKitInventoryError(RuntimeError):
    """The data-kit control plane is inconsistent or unsafe to inspect."""


class _CandidateRejected(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class DataKitInventoryExport:
    rows: tuple[dict[str, Any], ...]
    rejected: tuple[dict[str, Any], ...]
    source_bundle_id: str
    source_bundle_sha256: str
    source_database_id: str
    selected_documents: int


@dataclass(frozen=True, slots=True)
class _SourceControls:
    data_pack: dict[str, Any]
    master_entries: tuple[dict[str, Any], ...]
    database_rows: tuple[dict[str, Any], ...]
    source_bundle_id: str
    source_bundle_sha256: str
    source_database_id: str
    data_pack_manifest_sha256: str
    master_manifest_sha256: str


class _FileHasher:
    """Hash each path once and reject a file that changes while being read."""

    def __init__(self) -> None:
        self._cache: dict[Path, tuple[str, int]] = {}

    def hash(self, path: Path) -> tuple[str, int]:
        cached = self._cache.get(path)
        if cached is not None:
            return cached
        before = path.stat()
        if not stat.S_ISREG(before.st_mode):
            raise DataKitInventoryError(f"source object is not a regular file: {path}")
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
                size += len(block)
        after = path.stat()
        identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        if identity_before != identity_after or size != after.st_size:
            raise DataKitInventoryError(f"source object changed while it was read: {path}")
        result = (digest.hexdigest(), size)
        self._cache[path] = result
        return result


def _reject_symlink_components(path: Path, *, field: str) -> None:
    absolute = path.absolute()
    for component in (absolute, *absolute.parents):
        if component.is_symlink():
            raise DataKitInventoryError(f"{field} contains a symlink component")


def _audit_source_tree(source: Path) -> tuple[Path, ...]:
    """Return sorted raw PDF files after rejecting every link/special node."""

    raw_root = source / "artifacts" / "raw-pdfs"
    if not raw_root.is_dir():
        raise DataKitInventoryError("data-kit has no artifacts/raw-pdfs directory")
    pdfs: list[Path] = []
    for current, directory_names, file_names in os.walk(source, followlinks=False):
        parent = Path(current)
        for name in [*directory_names, *file_names]:
            candidate = parent / name
            metadata = candidate.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise DataKitInventoryError("data-kit contains a symlink")
            if name in directory_names:
                if not stat.S_ISDIR(metadata.st_mode):
                    raise DataKitInventoryError("data-kit contains a non-directory tree node")
            elif not stat.S_ISREG(metadata.st_mode):
                raise DataKitInventoryError("data-kit contains a special file")
            elif candidate.is_relative_to(raw_root) and candidate.suffix.casefold() == ".pdf":
                pdfs.append(candidate)
    return tuple(sorted(pdfs, key=lambda path: path.relative_to(source).as_posix()))


def _source_file(source: Path, value: object, *, field: str) -> Path:
    raw = str(value or "")
    relative = Path(raw)
    if (
        not raw
        or relative.is_absolute()
        or "\\" in raw
        or "\x00" in raw
        or any(part in {"", ".", ".."} for part in relative.parts)
        or relative.as_posix() != raw
    ):
        raise _CandidateRejected("unsafe_path", f"{field} is not a safe relative path")
    candidate = source / relative
    _reject_symlink_components(candidate, field=field)
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError as exc:
        raise _CandidateRejected("missing_file", f"{field} does not exist") from exc
    if not resolved.is_relative_to(source) or not resolved.is_file():
        raise DataKitInventoryError(f"{field} escapes the data-kit")
    return resolved


def _read_stable_bytes(path: Path, *, field: str) -> bytes:
    before = path.stat()
    payload = path.read_bytes()
    after = path.stat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ) or len(payload) != after.st_size:
        raise DataKitInventoryError(f"{field} changed while it was read")
    return payload


def _declared_sha256(value: object, *, field: str) -> str:
    digest = str(value or "").strip().casefold().removeprefix("sha256:")
    if _SHA256.fullmatch(digest) is None:
        raise _CandidateRejected("missing_hash", f"{field} has no valid SHA-256")
    return digest


def _load_json_object(path: Path, *, field: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise DataKitInventoryError(f"{field} is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise DataKitInventoryError(f"{field} must be a JSON object")
    return value


def _validate_data_pack(value: dict[str, Any]) -> None:
    required = {
        "name",
        "release_name",
        "created_kst",
        "created_utc",
        "data_kit_path",
        "included_roots",
        "sqlite_dbs",
        "key_counts",
        "excluded_transient",
        "notes",
        "source_hatch_root",
    }
    if set(value) != required:
        raise DataKitInventoryError("DATA_PACK_MANIFEST has missing or extra fields")
    release_name = value.get("release_name")
    if not isinstance(release_name, str) or _RELEASE_NAME.fullmatch(release_name) is None:
        raise DataKitInventoryError("DATA_PACK_MANIFEST release_name is invalid")
    if (
        value.get("name") != "cardrag-conveyor data-kit"
        or value.get("data_kit_path") != "data-kit/cardrag-conveyor-data"
        or value.get("included_roots") != _EXPECTED_INCLUDED_ROOTS
    ):
        raise DataKitInventoryError("DATA_PACK_MANIFEST layout contract is invalid")
    sqlite_dbs = value.get("sqlite_dbs")
    if not isinstance(sqlite_dbs, list) or _INVENTORY_RELATIVE.as_posix() not in sqlite_dbs:
        raise DataKitInventoryError("DATA_PACK_MANIFEST does not declare inventory.sqlite3")
    counts = value.get("key_counts")
    if not isinstance(counts, dict):
        raise DataKitInventoryError("DATA_PACK_MANIFEST key_counts is invalid")
    for field in ("manifest_entries", "inventory_docs"):
        if not isinstance(counts.get(field), int) or counts[field] < 1:
            raise DataKitInventoryError(f"DATA_PACK_MANIFEST {field} is invalid")


def _load_master(path: Path, expected_count: int) -> tuple[dict[str, Any], ...]:
    value = _load_json_object(path, field="cardrag master manifest")
    if set(value) != {"schema_version", "entries"}:
        raise DataKitInventoryError("cardrag master manifest has missing or extra fields")
    if value.get("schema_version") != "cardrag_master_manifest.v2":
        raise DataKitInventoryError("cardrag master manifest schema is unsupported")
    raw_entries = value.get("entries")
    if not isinstance(raw_entries, list) or len(raw_entries) != expected_count:
        raise DataKitInventoryError("cardrag master manifest count disagrees with DATA_PACK_MANIFEST")
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_entries:
        if not isinstance(raw, dict):
            raise DataKitInventoryError("cardrag master manifest contains a non-object entry")
        if not _MASTER_REQUIRED.issubset(raw) or not set(raw).issubset(_MASTER_ALLOWED):
            raise DataKitInventoryError("cardrag master manifest entry schema is invalid")
        document_id = raw.get("doc_version_id")
        if not isinstance(document_id, str) or not document_id or document_id in seen:
            raise DataKitInventoryError("cardrag master manifest document IDs are invalid or duplicated")
        seen.add(document_id)
        entries.append(dict(raw))
    return tuple(entries)


def _sqlite_rows(path: Path) -> tuple[dict[str, Any], ...]:
    for suffix in ("-journal", "-wal", "-shm"):
        if Path(f"{path}{suffix}").exists():
            raise DataKitInventoryError("inventory SQLite has a live sidecar and is not immutable")
    before = path.stat()
    uri = f"file:{path.as_posix()}?mode=ro&immutable=1"
    try:
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise DataKitInventoryError("inventory SQLite integrity_check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise DataKitInventoryError("inventory SQLite foreign_key_check failed")
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        }
        if tables != {"documents", "index_state"}:
            raise DataKitInventoryError("inventory SQLite table set is unsupported")
        document_columns = tuple(str(row[1]) for row in connection.execute("PRAGMA table_info(documents)"))
        state_columns = tuple(str(row[1]) for row in connection.execute("PRAGMA table_info(index_state)"))
        if document_columns != _DOCUMENT_COLUMNS or state_columns != _INDEX_STATE_COLUMNS:
            raise DataKitInventoryError("inventory SQLite column contract is unsupported")
        rows = tuple(dict(row) for row in connection.execute("SELECT * FROM documents ORDER BY doc_id"))
        state_ids = [
            str(row[0]) for row in connection.execute("SELECT doc_id FROM index_state ORDER BY doc_id")
        ]
        if state_ids != [str(row["doc_id"]) for row in rows]:
            raise DataKitInventoryError("inventory SQLite index_state coverage is incomplete")
    except sqlite3.Error as exc:
        raise DataKitInventoryError("inventory SQLite cannot be read safely") from exc
    finally:
        if "connection" in locals():
            connection.close()
    after = path.stat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise DataKitInventoryError("inventory SQLite changed while it was read")
    return rows


def _validate_database_master(
    database_rows: Sequence[Mapping[str, Any]],
    master_entries: Sequence[Mapping[str, Any]],
    data_pack: Mapping[str, Any],
) -> None:
    counts = data_pack["key_counts"]
    if len(database_rows) != counts["inventory_docs"]:
        raise DataKitInventoryError("inventory SQLite count disagrees with DATA_PACK_MANIFEST")
    master_by_id = {str(row["doc_version_id"]): row for row in master_entries}
    database_by_id = {str(row["doc_id"]): row for row in database_rows}
    if len(database_by_id) != len(database_rows) or set(database_by_id) != set(master_by_id):
        raise DataKitInventoryError("inventory SQLite and master manifest document sets disagree")
    issuer_counts: dict[str, int] = defaultdict(int)
    for document_id, database_row in database_by_id.items():
        master = master_by_id[document_id]
        for master_field, database_field in _MASTER_TO_DATABASE.items():
            if master.get(master_field) != database_row.get(database_field):
                raise DataKitInventoryError(
                    f"inventory SQLite and master manifest disagree for {document_id}/{database_field}"
                )
        if database_row.get("is_latest") not in {0, 1}:
            raise DataKitInventoryError("inventory SQLite has an invalid is_latest value")
        issuer = str(database_row["card_company"])
        if issuer not in _ISSUER_MAP:
            raise DataKitInventoryError(f"inventory SQLite has an unsupported issuer: {issuer}")
        issuer_counts[issuer] += 1
    declared_issuer_counts = data_pack["key_counts"].get("inventory_by_issuer")
    if not isinstance(declared_issuer_counts, list):
        raise DataKitInventoryError("DATA_PACK_MANIFEST inventory_by_issuer is invalid")
    try:
        normalized_counts = {str(key): int(value) for key, value in declared_issuer_counts}
    except (TypeError, ValueError) as exc:
        raise DataKitInventoryError("DATA_PACK_MANIFEST inventory_by_issuer is invalid") from exc
    if dict(issuer_counts) != normalized_counts:
        raise DataKitInventoryError("inventory issuer counts disagree with DATA_PACK_MANIFEST")

    grouped: dict[tuple[str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in database_rows:
        grouped[(str(row["card_company"]), str(row["product_code"]), str(row["doc_type"]))].append(row)
    # The archived ledger intentionally keeps one manually approved exception:
    # wooricard 103437 v9 is active while same-date v10 has incomplete OCR page
    # coverage.  Preserve that DB decision instead of silently recomputing it
    # from natural version order, but require a single unambiguous active row.
    if any(sum(row["is_latest"] == 1 for row in rows) != 1 for rows in grouped.values()):
        raise DataKitInventoryError("inventory SQLite must select exactly one latest row per product")


def _load_controls(source: Path, hasher: _FileHasher) -> _SourceControls:
    data_pack_path = source / _DATA_PACK_RELATIVE
    master_path = source / _MASTER_MANIFEST_RELATIVE
    inventory_path = source / _INVENTORY_RELATIVE
    ocr_inventory_path = source / _OCR_INVENTORY_RELATIVE
    for path, field in (
        (data_pack_path, "DATA_PACK_MANIFEST"),
        (master_path, "cardrag master manifest"),
        (inventory_path, "inventory SQLite"),
        (ocr_inventory_path, "OCR inventory SQLite"),
    ):
        if path.is_symlink() or not path.is_file():
            raise DataKitInventoryError(f"{field} is missing or unsafe")
    data_pack = _load_json_object(data_pack_path, field="DATA_PACK_MANIFEST")
    _validate_data_pack(data_pack)
    master_entries = _load_master(master_path, int(data_pack["key_counts"]["manifest_entries"]))
    database_rows = _sqlite_rows(inventory_path)
    _validate_database_master(database_rows, master_entries, data_pack)

    data_pack_sha, _ = hasher.hash(data_pack_path)
    master_sha, _ = hasher.hash(master_path)
    inventory_sha, _ = hasher.hash(inventory_path)
    ocr_inventory_sha, _ = hasher.hash(ocr_inventory_path)
    if inventory_sha != ocr_inventory_sha:
        raise DataKitInventoryError("inventory.sqlite3 and ocr_inventory.sqlite3 are not identical")
    source_bundle_sha = canonical_sha256(
        {
            "data_pack_manifest_sha256": data_pack_sha,
            "master_manifest_sha256": master_sha,
            "release_name": data_pack["release_name"],
            "schema_version": DATA_KIT_SOURCE_SCHEMA,
        }
    )
    return _SourceControls(
        data_pack=data_pack,
        master_entries=master_entries,
        database_rows=database_rows,
        source_bundle_id=f"data-kit-{source_bundle_sha[:12]}",
        source_bundle_sha256=source_bundle_sha,
        source_database_id=f"data-kit-sqlite-sha256:{inventory_sha}",
        data_pack_manifest_sha256=data_pack_sha,
        master_manifest_sha256=master_sha,
    )


def _canonical_ocr(payload: bytes, *, expected_pages: int, expected_sha256: str) -> tuple[str, str, int]:
    digest = hashlib.sha256(payload).hexdigest()
    if digest != expected_sha256:
        raise _CandidateRejected("ocr_hash_mismatch", "OCR bytes differ from metadata SHA-256")
    if b"\x00" in payload or b"\r" in payload:
        raise _CandidateRejected("ocr_noncanonical", "OCR contains forbidden NUL or CR bytes")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _CandidateRejected("ocr_invalid_utf8", "OCR is not valid UTF-8") from exc
    markers = list(_PAGE_MARKER.finditer(text))
    if [int(match.group(1)) for match in markers] != list(range(1, expected_pages + 1)):
        raise _CandidateRejected("ocr_page_coverage", "OCR page markers do not cover the PDF")
    pages = tuple(
        text[match.start() : markers[index + 1].start() if index + 1 < len(markers) else len(text)].strip()
        for index, match in enumerate(markers)
    )
    if any(len(page) < 20 for page in pages):
        raise _CandidateRejected("ocr_short_page", "OCR contains an implausibly short page")
    if text != "\n\n".join(pages) + "\n":
        raise _CandidateRejected("ocr_noncanonical", "OCR is not in canonical page-join form")
    return digest, text, len(payload)


def _candidate_pdf_path(
    source: Path,
    master: Mapping[str, Any],
    *,
    expected_sha256: str,
    all_pdfs: Sequence[Path],
    hasher: _FileHasher,
) -> Path:
    direct = master.get("raw_pdf_rel_path")
    if direct:
        path = _source_file(source, direct, field="raw_pdf_rel_path")
        actual, _ = hasher.hash(path)
        if actual != expected_sha256:
            raise _CandidateRejected("pdf_hash_mismatch", "declared raw PDF hash does not match")
        return path

    output_base = str(master.get("output_base_rel") or "")
    file_name = str(master.get("fileNm") or "")
    derived: Path | None = None
    if output_base.startswith("artifacts/ocr/") and file_name and "/" not in file_name:
        candidate_relative = "artifacts/raw-pdfs/" + output_base.removeprefix("artifacts/ocr/")
        candidate_relative += f"__{file_name}"
        try:
            derived = _source_file(source, candidate_relative, field="derived raw PDF path")
            actual, _ = hasher.hash(derived)
            if actual == expected_sha256:
                return derived
        except _CandidateRejected:
            derived = None

    for candidate in all_pdfs:
        if derived is not None and candidate == derived:
            continue
        actual, _ = hasher.hash(candidate)
        if actual == expected_sha256:
            return candidate
    raise _CandidateRejected("pdf_unresolved", "no raw PDF matches the master manifest hash")


def _validated_candidate(
    source: Path,
    database: Mapping[str, Any],
    master: Mapping[str, Any],
    *,
    controls: _SourceControls,
    all_pdfs: Sequence[Path],
    hasher: _FileHasher,
) -> dict[str, Any]:
    legacy_document_id = str(database["doc_id"])
    issuer = _ISSUER_MAP[str(database["card_company"])]
    product_code = str(database["product_code"])
    document_type = str(database["doc_type"])
    effective_date = str(database["effective_date"])
    source_version = str(database["version"])
    if (
        _ISSUER_CODE.fullmatch(issuer) is None
        or _PRODUCT_CODE.fullmatch(product_code) is None
        or _DOCUMENT_TYPE.fullmatch(document_type) is None
        or not source_version
    ):
        raise DataKitInventoryError(f"unsafe document identity in SQLite: {legacy_document_id}")
    try:
        if Path(effective_date).name != effective_date:
            raise ValueError
        from datetime import date

        if date.fromisoformat(effective_date).isoformat() != effective_date:
            raise ValueError
    except ValueError as exc:
        raise DataKitInventoryError(f"invalid effective date in SQLite: {legacy_document_id}") from exc

    expected_pdf = _declared_sha256(master.get("pdf_sha256"), field="master pdf_sha256")
    pdf_path = _candidate_pdf_path(
        source,
        master,
        expected_sha256=expected_pdf,
        all_pdfs=all_pdfs,
        hasher=hasher,
    )
    actual_pdf, _ = hasher.hash(pdf_path)
    try:
        with open_pdf(pdf_path) as document:
            document.validate_all_pages()
            page_count = document.page_count
    except PDFEngineError as exc:
        raise _CandidateRejected("pdf_invalid", "PDF cannot be fully validated") from exc
    if page_count < 1 or page_count != database.get("pages"):
        raise _CandidateRejected("pdf_page_count", "PDF page count differs from the ledger")

    metadata_path = _source_file(source, master.get("metadata_remote_rel"), field="metadata_remote_rel")
    try:
        metadata = json.loads(_read_stable_bytes(metadata_path, field="OCR metadata"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise _CandidateRejected("metadata_invalid", "OCR metadata is not valid UTF-8 JSON") from exc
    if not isinstance(metadata, dict):
        raise _CandidateRejected("metadata_invalid", "OCR metadata is not a JSON object")
    metadata_page_count = metadata.get("page_count", metadata.get("pages"))
    if (
        metadata.get("schema_version") not in {"cardrag_imported_ocr_asset.v1", "ocr_result_manifest.v2"}
        or metadata.get("doc_version_id") != legacy_document_id
        or metadata.get("status") != "success"
        or metadata.get("primary_text_artifact") != "ocr.md"
        or metadata.get("metadata_rel_path") != master.get("metadata_remote_rel")
        or metadata.get("ocr_md_rel_path") != master.get("ocr_remote_rel")
        or metadata_page_count != page_count
    ):
        raise _CandidateRejected("metadata_identity", "OCR metadata does not bind the selected document")
    metadata_pdf = metadata.get("raw_pdf_sha256")
    if (
        metadata_pdf is not None
        and _declared_sha256(metadata_pdf, field="metadata raw_pdf_sha256") != actual_pdf
    ):
        raise _CandidateRejected("metadata_pdf_hash", "OCR metadata PDF hash differs")
    metadata_pdf_relative = metadata.get("raw_pdf_rel_path")
    if metadata_pdf_relative:
        metadata_pdf_path = _source_file(
            source,
            metadata_pdf_relative,
            field="metadata raw_pdf_rel_path",
        )
        metadata_path_sha, _ = hasher.hash(metadata_pdf_path)
        if metadata_path_sha != actual_pdf:
            raise _CandidateRejected("metadata_pdf_hash", "OCR metadata PDF path differs")

    ocr_path = _source_file(source, master.get("ocr_remote_rel"), field="ocr_remote_rel")
    expected_ocr = _declared_sha256(metadata.get("ocr_md_sha256"), field="metadata ocr_md_sha256")
    payload = _read_stable_bytes(ocr_path, field="OCR object")
    actual_ocr, text, _ = _canonical_ocr(
        payload,
        expected_pages=page_count,
        expected_sha256=expected_ocr,
    )
    if metadata.get("ocr_md_chars") != len(text):
        raise _CandidateRejected("ocr_char_count", "OCR character count differs from metadata")

    return {
        "schema_version": DATA_KIT_INVENTORY_SCHEMA,
        "issuer": issuer,
        "product_code": product_code,
        "source_bundle_id": controls.source_bundle_id,
        "source_bundle_sha256": controls.source_bundle_sha256,
        "source_database_id": controls.source_database_id,
        "source_data_pack_manifest_sha256": controls.data_pack_manifest_sha256,
        "source_master_manifest_sha256": controls.master_manifest_sha256,
        "source_document_id": legacy_document_id,
        "legacy_source_document_id": legacy_document_id,
        "document_type": document_type,
        "effective_date": effective_date,
        "source_version": source_version,
        "pdf_path": str(pdf_path),
        "ocr_path": str(ocr_path),
        "ledger_pdf_sha256": actual_pdf,
        "ledger_ocr_sha256": actual_ocr,
    }


def export_data_kit_inventory(source_root: Path) -> DataKitInventoryExport:
    """Validate the raw data-kit and return only strictly reusable latest OCR rows."""

    if not source_root.is_absolute():
        raise DataKitInventoryError("data-kit source must be an absolute path")
    _reject_symlink_components(source_root, field="data-kit source")
    source = source_root.resolve(strict=True)
    if not source.is_dir():
        raise DataKitInventoryError("data-kit source must be a directory")
    all_pdfs = _audit_source_tree(source)
    hasher = _FileHasher()
    controls = _load_controls(source, hasher)
    master_by_id = {str(row["doc_version_id"]): row for row in controls.master_entries}
    selected = tuple(
        row
        for row in controls.database_rows
        if row["is_latest"] == 1 and row["status"] == "done" and row["error"] == ""
    )
    if not selected:
        raise DataKitInventoryError("data-kit has no latest successful documents")

    rows: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for database in selected:
        legacy_document_id = str(database["doc_id"])
        try:
            rows.append(
                _validated_candidate(
                    source,
                    database,
                    master_by_id[legacy_document_id],
                    controls=controls,
                    all_pdfs=all_pdfs,
                    hasher=hasher,
                )
            )
        except _CandidateRejected as exc:
            rejected.append(
                {
                    "schema_version": DATA_KIT_REJECTION_SCHEMA,
                    "source_bundle_id": controls.source_bundle_id,
                    "source_bundle_sha256": controls.source_bundle_sha256,
                    "source_database_id": controls.source_database_id,
                    "source_data_pack_manifest_sha256": controls.data_pack_manifest_sha256,
                    "source_master_manifest_sha256": controls.master_manifest_sha256,
                    "source_document_id": legacy_document_id,
                    "issuer": _ISSUER_MAP.get(str(database.get("card_company")), "unknown"),
                    "product_code": str(database.get("product_code") or ""),
                    "reason": exc.code,
                    "detail": str(exc),
                }
            )
    ordering = lambda row: (  # noqa: E731 - shared deterministic JSONL order
        str(row["issuer"]),
        str(row["product_code"]),
        str(row["source_document_id"]),
    )
    return DataKitInventoryExport(
        rows=tuple(sorted(rows, key=ordering)),
        rejected=tuple(sorted(rejected, key=ordering)),
        source_bundle_id=controls.source_bundle_id,
        source_bundle_sha256=controls.source_bundle_sha256,
        source_database_id=controls.source_database_id,
        selected_documents=len(selected),
    )


def canonical_data_kit_jsonl(rows: Sequence[Mapping[str, Any]]) -> bytes:
    """Serialize inventory or rejection rows as deterministic canonical JSONL."""

    ordered = sorted(
        (dict(row) for row in rows),
        key=lambda row: (
            str(row["issuer"]),
            str(row["product_code"]),
            str(row["source_document_id"]),
        ),
    )
    return b"".join(canonical_json_bytes(row) + b"\n" for row in ordered)


def write_data_kit_inventory(
    result: DataKitInventoryExport,
    output: Path,
    *,
    source_root: Path,
    rejected_output: Path | None = None,
) -> tuple[Path, Path]:
    """Create the Worker inventory and an explicit per-document rejection ledger."""

    rejected = rejected_output or output.with_name(f"{output.name}.rejected.jsonl")
    source = source_root.resolve(strict=True)
    targets = (output, rejected)
    for target in targets:
        if not target.is_absolute():
            raise DataKitInventoryError("data-kit export outputs must be absolute")
        _reject_symlink_components(target.parent, field="data-kit export parent")
        parent = target.parent.resolve(strict=True)
        resolved = parent / target.name
        if resolved == source or resolved.is_relative_to(source):
            raise DataKitInventoryError("data-kit export outputs must be outside the source")
        if resolved.exists() or resolved.is_symlink():
            raise DataKitInventoryError(f"data-kit export output already exists: {resolved}")
    if output == rejected:
        raise DataKitInventoryError("inventory and rejection outputs must be different paths")

    rejected_written = atomic_write_bytes(
        rejected,
        canonical_data_kit_jsonl(result.rejected),
        mode=0o600,
    )
    try:
        inventory_written = atomic_write_bytes(
            output,
            canonical_data_kit_jsonl(result.rows),
            mode=0o600,
        )
    except BaseException:
        rejected_written.unlink(missing_ok=True)
        raise
    return inventory_written, rejected_written
