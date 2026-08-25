"""Auditable validation of legacy/current OCR exports before WebDAV adoption."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from cardrag_core import canonical_json_bytes, canonical_sha256
from cardrag_core.domain import ArtifactRef
from cardrag_core.manifests import (
    AdoptedOCRArtifactManifest,
    LegacyAdoptionReceipt,
    LegacyAdoptionValidation,
    OCRReady,
    adopted_ocr_reuse_key,
)
from cardrag_core.ocr import OCRInput, verify_ocr_bytes
from cardrag_core.paths import ocr_manifest_path, ocr_ready_path

from .downloader import validate_pdf
from .webdav import WebDAVClient

ADOPTION_POLICY_VERSION = "cardrag.legacy-ocr-adoption.v1"
LEGACY_LEDGER_SCHEMA = "cardrag.legacy-adoption-ledger.v1"
ADOPTION_CONTROL_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class AdoptionError(RuntimeError):
    pass


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


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


def load_inventory(path: Path) -> tuple[dict[str, Any], ...]:
    if path.is_dir():
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
        or manifest.get("adoption_policy") != ADOPTION_POLICY_VERSION
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


def validate_candidate(
    row: Mapping[str, Any], *, source_kind: Literal["legacy", "current"]
) -> dict[str, Any]:
    pdf_path = _regular_file(row.get("pdf_path"), field="pdf_path")
    ocr_path = _regular_file(row.get("ocr_path"), field="ocr_path")
    pdf_sha, pdf_size, pages = validate_pdf(pdf_path)
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
        adoption_policy_version=ADOPTION_POLICY_VERSION,
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
        adoption_policy_version=ADOPTION_POLICY_VERSION,
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
            (str(row["receipt"]["pdf_sha256"]), str(row["receipt"]["ocr_sha256"])) for row in candidates
        }
        if len(identities) > 1:
            conflicts.append(
                AdoptionConflict(
                    source_document_id=document_id,
                    reason="same source_document_id has conflicting PDF/OCR bytes",
                    candidates=tuple(
                        {
                            "pdf_sha256": str(row["receipt"]["pdf_sha256"]),
                            "ocr_sha256": str(row["receipt"]["ocr_sha256"]),
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
                            "ocr_sha256": str(previous["receipt"]["ocr_sha256"]),
                        },
                        {
                            "source_document_id": str(row["receipt"]["source_document_id"]),
                            "pdf_sha256": str(row["receipt"]["pdf_sha256"]),
                            "ocr_sha256": str(row["receipt"]["ocr_sha256"]),
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
                            "ocr_sha256": str(current_row["receipt"]["ocr_sha256"]),
                        },
                        {
                            "source_document_id": str(row["receipt"]["source_document_id"]),
                            "pdf_sha256": str(row["receipt"]["pdf_sha256"]),
                            "ocr_sha256": str(row["receipt"]["ocr_sha256"]),
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
            str(current_row["receipt"]["ocr_sha256"]),
        )
        legacy_identity = (
            str(row["receipt"]["source_document_id"]),
            str(row["receipt"]["pdf_sha256"]),
            str(row["receipt"]["ocr_sha256"]),
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
                            "ocr_sha256": current_identity[2],
                        },
                        {
                            "source": "legacy",
                            "source_document_id": legacy_identity[0],
                            "pdf_sha256": legacy_identity[1],
                            "ocr_sha256": legacy_identity[2],
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


async def publish_adoptions(result: AdoptionResult, webdav: WebDAVClient) -> int:
    published = 0
    for row in result.receipts:
        manifest = AdoptedOCRArtifactManifest.model_validate_json(
            json.dumps(row["manifest"], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )
        ocr_body = Path(row["ocr_path"]).read_bytes()
        object_sha, _ = await webdav.put_cas(ocr_body, media_type="text/markdown; charset=utf-8")
        if object_sha != manifest.output.sha256:
            raise AdoptionError("OCR changed after validation and before publication")
        manifest_body = await webdav.put_json(
            ocr_manifest_path(manifest.reuse_key, kind="adopted"),
            manifest.model_dump(mode="json"),
            immutable=True,
        )
        ready = OCRReady(
            reuse_key=manifest.reuse_key,
            manifest_sha256=hashlib.sha256(manifest_body).hexdigest(),
            ocr_sha256=manifest.output.sha256,
        )
        await webdav.put_json(
            ocr_ready_path(manifest.reuse_key, kind="adopted"),
            ready.model_dump(mode="json"),
            immutable=True,
        )
        published += 1
    return published
