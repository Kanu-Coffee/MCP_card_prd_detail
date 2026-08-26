#!/usr/bin/env python3
"""Strict, read-only normalization export for the archived CardRAG data-kit.

This is an operator tool, not a runtime component.  It validates the complete
legacy control plane and every selected latest PDF/OCR pair before creating a
new, self-contained export directory.  The source tree is never modified.

The only permitted normalization is removal of the exact 24-byte generated
Woori heading ``# OCR 처리 완료본\n\n`` immediately before
``## Page 1``.  Original and normalized identities remain separately bound in
the v2 receipts and source-bundle identity.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import sqlite3
import stat
import sys
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any, Final

from cardrag_core import canonical_json_bytes, canonical_sha256
from cardrag_worker.downloader import PDFValidationError, validate_pdf

ADOPTION_POLICY_VERSION: Final = "cardrag.legacy-ocr-adoption.v2"
INVENTORY_SCHEMA: Final = "cardrag.data-kit-adoption-inventory.v2"
REJECTION_SCHEMA: Final = "cardrag.data-kit-adoption-rejection.v2"
RECEIPT_SCHEMA: Final = "cardrag.data-kit-normalization-receipt.v2"
SOURCE_SCHEMA: Final = "cardrag.data-kit-source.v2"
EXPORT_MANIFEST_SCHEMA: Final = "cardrag.data-kit-adoption-export.v2"

EXACT_PROFILE: Final = "exact"
WOORI_PREFIX_PROFILE: Final = "strip-exact-generated-prefix-v1"
WOORI_GENERATED_PREFIX: Final = "# OCR 처리 완료본\n\n".encode()
WOORI_GENERATED_PREFIX_SHA256: Final = hashlib.sha256(WOORI_GENERATED_PREFIX).hexdigest()

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
_MASTER_ALLOWED = _MASTER_REQUIRED | {"fileSize", "pdf_fingerprint", "raw_pdf_rel_path"}


class DataKitExportError(RuntimeError):
    """The source controls, candidate bytes, or output target are unsafe."""


def _file_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        metadata.st_mode,
        metadata.st_nlink,
    )


def _node_identity(metadata: os.stat_result) -> tuple[int, int, int]:
    return metadata.st_dev, metadata.st_ino, stat.S_IFMT(metadata.st_mode)


def _directory_open_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _open_directory_at(parent_descriptor: int, name: str, *, field: str) -> tuple[int, os.stat_result]:
    try:
        descriptor = os.open(name, _directory_open_flags(), dir_fd=parent_descriptor)
    except OSError as exc:
        raise DataKitExportError(f"{field} could not be pinned without following symlinks") from exc
    try:
        opened = os.fstat(descriptor)
        named = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or not stat.S_ISDIR(named.st_mode)
            or _node_identity(opened) != _node_identity(named)
        ):
            raise DataKitExportError(f"{field} is not one stable directory")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, opened


def _open_absolute_directory_nofollow(path: Path, *, field: str) -> tuple[int, os.stat_result]:
    """Pin an absolute directory without resolving any symlinked component."""

    if not path.is_absolute() or ".." in path.parts:
        raise DataKitExportError(f"{field} must be an absolute normalized path")
    try:
        descriptor = os.open(os.sep, _directory_open_flags())
    except OSError as exc:
        raise DataKitExportError(f"{field} could not be pinned") from exc
    opened = os.fstat(descriptor)
    try:
        for component in path.parts[1:]:
            child, opened = _open_directory_at(descriptor, component, field=field)
            os.close(descriptor)
            descriptor = child
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, opened


def _verify_absolute_directory(path: Path, descriptor: int, *, field: str) -> None:
    """Require *path* to still name the directory held by *descriptor*."""

    try:
        current, _ = _open_absolute_directory_nofollow(path, field=field)
    except DataKitExportError as exc:
        raise DataKitExportError(f"{field} changed after it was pinned") from exc
    try:
        if _node_identity(os.fstat(current)) != _node_identity(os.fstat(descriptor)):
            raise DataKitExportError(f"{field} changed after it was pinned")
    finally:
        os.close(current)


def _safe_export_parts(relative: str) -> tuple[str, ...]:
    path = Path(relative)
    if (
        not relative
        or path.is_absolute()
        or "\\" in relative
        or "\x00" in relative
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.as_posix() != relative
    ):
        raise DataKitExportError("export object path is not a safe relative path")
    return path.parts


@dataclass(slots=True)
class _CreatedExportFile:
    parent_descriptor: int
    name: str
    node_identity: tuple[int, int, int]
    expected_sha256: str
    expected_size: int
    sealed_identity: tuple[int, int, int, int, int, int, int] | None = None


class _ExportWriter:
    """Create and verify export objects beneath one pinned private root."""

    def __init__(self, root_descriptor: int) -> None:
        self._directories: dict[tuple[str, ...], int] = {(): os.dup(root_descriptor)}
        self._created_directories: list[tuple[int, str, int, tuple[int, int, int]]] = []
        self._created_files: list[_CreatedExportFile] = []

    def _directory(self, parts: tuple[str, ...]) -> int:
        current_parts: tuple[str, ...] = ()
        current = self._directories[current_parts]
        for component in parts:
            current_parts = (*current_parts, component)
            cached = self._directories.get(current_parts)
            if cached is not None:
                current = cached
                continue
            try:
                os.mkdir(component, mode=0o700, dir_fd=current)
            except FileExistsError as exc:
                raise DataKitExportError(f"export directory collision: {'/'.join(current_parts)}") from exc
            child, metadata = _open_directory_at(
                current,
                component,
                field=f"export directory {'/'.join(current_parts)}",
            )
            os.fchmod(child, 0o700)
            identity = _node_identity(metadata)
            self._directories[current_parts] = child
            self._created_directories.append((current, component, child, identity))
            current = child
        return current

    @staticmethod
    def _unlink_if_identity(parent: int, name: str, identity: tuple[int, int, int]) -> None:
        try:
            named = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except OSError:
            return
        if _node_identity(named) != identity:
            return
        try:
            os.unlink(name, dir_fd=parent)
        except (FileNotFoundError, IsADirectoryError):
            return

    def create(self, relative: str, payload: bytes) -> None:
        parts = _safe_export_parts(relative)
        parent = self._directory(parts[:-1])
        target_name = parts[-1]
        temporary_name = f".partial-{secrets.token_hex(16)}"
        temporary_identity: tuple[int, int, int] | None = None
        temporary_descriptor: int | None = None
        created: _CreatedExportFile | None = None
        try:
            flags = (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            temporary_descriptor = os.open(temporary_name, flags, 0o600, dir_fd=parent)
            os.fchmod(temporary_descriptor, 0o600)
            temporary_metadata = os.fstat(temporary_descriptor)
            if not stat.S_ISREG(temporary_metadata.st_mode):
                raise DataKitExportError("export temporary object is not a regular file")
            temporary_identity = _node_identity(temporary_metadata)
            remaining = memoryview(payload)
            while remaining:
                written = os.write(temporary_descriptor, remaining)
                if written < 1:
                    raise DataKitExportError("export temporary object write made no progress")
                remaining = remaining[written:]
            os.fsync(temporary_descriptor)
            try:
                os.link(
                    temporary_name,
                    target_name,
                    src_dir_fd=parent,
                    dst_dir_fd=parent,
                    follow_symlinks=False,
                )
            except FileExistsError as exc:
                raise DataKitExportError(f"export output collision: {relative}") from exc
            created = _CreatedExportFile(
                parent_descriptor=parent,
                name=target_name,
                node_identity=temporary_identity,
                expected_sha256=hashlib.sha256(payload).hexdigest(),
                expected_size=len(payload),
            )
            self._created_files.append(created)
            target = os.stat(target_name, dir_fd=parent, follow_symlinks=False)
            if (
                _node_identity(target) != temporary_identity
                or not stat.S_ISREG(target.st_mode)
                or stat.S_IMODE(target.st_mode) != 0o600
            ):
                raise DataKitExportError(f"export output is not one stable mode-0600 file: {relative}")
        finally:
            if temporary_descriptor is not None:
                os.close(temporary_descriptor)
            if temporary_identity is not None:
                self._unlink_if_identity(parent, temporary_name, temporary_identity)
        if created is None:
            raise DataKitExportError(f"export output was not recorded: {relative}")
        sealed = os.stat(target_name, dir_fd=parent, follow_symlinks=False)
        if (
            _node_identity(sealed) != created.node_identity
            or not stat.S_ISREG(sealed.st_mode)
            or stat.S_IMODE(sealed.st_mode) != 0o600
        ):
            raise DataKitExportError(f"export output changed while it was sealed: {relative}")
        created.sealed_identity = _file_identity(sealed)

    def verify(self) -> None:
        for parent, name, descriptor, identity in self._created_directories:
            try:
                named = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except OSError as exc:
                raise DataKitExportError("export directory changed while it was written") from exc
            if (
                _node_identity(named) != identity
                or _node_identity(os.fstat(descriptor)) != identity
                or stat.S_IMODE(named.st_mode) != 0o700
            ):
                raise DataKitExportError("export directory changed while it was written")
        for created in self._created_files:
            if created.sealed_identity is None:
                raise DataKitExportError("export file was not completely sealed")
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(created.name, flags, dir_fd=created.parent_descriptor)
            except OSError as exc:
                raise DataKitExportError("export file changed while it was written") from exc
            try:
                before = os.fstat(descriptor)
                named_before = os.stat(
                    created.name,
                    dir_fd=created.parent_descriptor,
                    follow_symlinks=False,
                )
                digest = hashlib.sha256()
                size = 0
                while block := os.read(descriptor, 1024 * 1024):
                    digest.update(block)
                    size += len(block)
                after = os.fstat(descriptor)
                named_after = os.stat(
                    created.name,
                    dir_fd=created.parent_descriptor,
                    follow_symlinks=False,
                )
            except OSError as exc:
                raise DataKitExportError("export file changed while it was verified") from exc
            finally:
                os.close(descriptor)
            if (
                _file_identity(before) != created.sealed_identity
                or _file_identity(after) != created.sealed_identity
                or _file_identity(named_before) != created.sealed_identity
                or _file_identity(named_after) != created.sealed_identity
                or not stat.S_ISREG(after.st_mode)
                or stat.S_IMODE(after.st_mode) != 0o600
                or size != created.expected_size
                or digest.hexdigest() != created.expected_sha256
            ):
                raise DataKitExportError("export file bytes or identity changed while it was written")

    def cleanup(self) -> None:
        """Remove only nodes whose identities this writer created."""

        for created in reversed(self._created_files):
            self._unlink_if_identity(
                created.parent_descriptor,
                created.name,
                created.node_identity,
            )
        for parent, name, _descriptor, identity in reversed(self._created_directories):
            try:
                named = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except OSError:
                continue
            if _node_identity(named) != identity:
                continue
            try:
                os.rmdir(name, dir_fd=parent)
            except OSError:
                continue

    def close(self) -> None:
        for descriptor in reversed(tuple(self._directories.values())):
            os.close(descriptor)
        self._directories.clear()


def _open_regular_nofollow(path: Path, *, field: str) -> tuple[int, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise DataKitExportError(f"{field} could not be opened without following links") from exc
    try:
        opened = os.fstat(descriptor)
        named = path.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(named.st_mode)
            or _file_identity(opened) != _file_identity(named)
        ):
            raise DataKitExportError(f"{field} is not one stable regular file")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, opened


def _finish_stable_read(
    path: Path,
    *,
    descriptor: int,
    before: os.stat_result,
    bytes_read: int,
    field: str,
) -> tuple[int, int, int, int, int, int, int]:
    try:
        after = os.fstat(descriptor)
        named = path.lstat()
    except OSError as exc:
        raise DataKitExportError(f"{field} changed while it was read") from exc
    identity = _file_identity(before)
    if identity != _file_identity(after) or identity != _file_identity(named) or bytes_read != after.st_size:
        raise DataKitExportError(f"{field} changed while it was read")
    return identity


class _CandidateRejected(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class SourceControls:
    data_pack: dict[str, Any]
    master_entries: tuple[dict[str, Any], ...]
    database_rows: tuple[dict[str, Any], ...]
    data_pack_manifest_sha256: str
    master_manifest_sha256: str
    inventory_sha256: str
    ocr_inventory_sha256: str
    source_database_id: str

    def identity_fields(self) -> dict[str, str]:
        return {
            "data_pack_manifest_sha256": self.data_pack_manifest_sha256,
            "inventory_sha256": self.inventory_sha256,
            "master_manifest_sha256": self.master_manifest_sha256,
            "ocr_inventory_sha256": self.ocr_inventory_sha256,
        }


@dataclass(frozen=True, slots=True)
class CandidatePlan:
    inventory: dict[str, Any]
    receipt: dict[str, Any]
    source_ocr_path: Path
    normalized_object_relative: str | None

    def transformation_identity(self) -> dict[str, Any]:
        return {
            "issuer": self.receipt["issuer"],
            "normalization_profile": self.receipt["normalization_profile"],
            "normalized_ocr_sha256": self.receipt["normalized_ocr_sha256"],
            "normalized_ocr_size_bytes": self.receipt["normalized_ocr_size_bytes"],
            "prefix_sha256": self.receipt["prefix_sha256"],
            "product_code": self.receipt["product_code"],
            "removed_bytes": self.receipt["removed_bytes"],
            "source_document_id": self.receipt["source_document_id"],
            "source_ocr_sha256": self.receipt["source_ocr_sha256"],
            "source_ocr_size_bytes": self.receipt["source_ocr_size_bytes"],
        }


@dataclass(frozen=True, slots=True)
class DataKitExportPlan:
    source_root: Path
    controls: SourceControls
    candidates: tuple[CandidatePlan, ...]
    rejected: tuple[dict[str, Any], ...]
    selected_documents: int
    source_bundle_id: str
    source_bundle_sha256: str

    @property
    def rows(self) -> tuple[dict[str, Any], ...]:
        return tuple(candidate.inventory for candidate in self.candidates)

    @property
    def receipts(self) -> tuple[dict[str, Any], ...]:
        return tuple(candidate.receipt for candidate in self.candidates)


@dataclass(frozen=True, slots=True)
class DataKitExportResult:
    output_root: Path
    inventory_path: Path
    rejected_path: Path
    receipts_path: Path
    manifest_path: Path
    accepted_documents: int
    rejected_documents: int
    exact_documents: int
    normalized_documents: int
    source_bundle_id: str
    source_bundle_sha256: str


class _FileHasher:
    """Hash each path once and reject a file that changes while being read."""

    def __init__(self) -> None:
        self._cache: dict[Path, tuple[str, int, tuple[int, int, int, int, int, int, int]]] = {}

    def hash(self, path: Path) -> tuple[str, int]:
        cached = self._cache.get(path)
        if cached is not None:
            cached_digest, size, identity = cached
            try:
                current = path.lstat()
            except OSError as exc:
                raise DataKitExportError(f"cached source object disappeared: {path}") from exc
            if _file_identity(current) != identity:
                raise DataKitExportError(f"cached source object changed: {path}")
            return cached_digest, size
        descriptor, before = _open_regular_nofollow(path, field="source object")
        hasher = hashlib.sha256()
        size = 0
        try:
            for block in iter(lambda: os.read(descriptor, 1024 * 1024), b""):
                hasher.update(block)
                size += len(block)
            identity = _finish_stable_read(
                path,
                descriptor=descriptor,
                before=before,
                bytes_read=size,
                field="source object",
            )
        finally:
            os.close(descriptor)
        result = (hasher.hexdigest(), size)
        self._cache[path] = (*result, identity)
        return result


def _reject_symlink_components(path: Path, *, field: str) -> None:
    absolute = path.absolute()
    for component in (absolute, *absolute.parents):
        if component.is_symlink():
            raise DataKitExportError(f"{field} contains a symlink component")


def _audit_source_tree(source: Path) -> tuple[Path, ...]:
    raw_root = source / "artifacts" / "raw-pdfs"
    if not raw_root.is_dir():
        raise DataKitExportError("data-kit has no artifacts/raw-pdfs directory")
    pdfs: list[Path] = []
    for current, directory_names, file_names in os.walk(source, followlinks=False):
        parent = Path(current)
        for name in [*directory_names, *file_names]:
            candidate = parent / name
            metadata = candidate.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise DataKitExportError("data-kit contains a symlink")
            if name in directory_names:
                if not stat.S_ISDIR(metadata.st_mode):
                    raise DataKitExportError("data-kit contains a non-directory tree node")
            elif not stat.S_ISREG(metadata.st_mode):
                raise DataKitExportError("data-kit contains a special file")
            elif candidate.is_relative_to(raw_root) and candidate.suffix.casefold() == ".pdf":
                pdfs.append(candidate.resolve())
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
        raise DataKitExportError(f"{field} escapes the data-kit")
    return resolved


def _read_stable_bytes_and_sha256(path: Path, *, field: str) -> tuple[bytes, str]:
    descriptor, before = _open_regular_nofollow(path, field=field)
    chunks: list[bytes] = []
    digest = hashlib.sha256()
    size = 0
    try:
        for block in iter(lambda: os.read(descriptor, 1024 * 1024), b""):
            chunks.append(block)
            digest.update(block)
            size += len(block)
        _finish_stable_read(
            path,
            descriptor=descriptor,
            before=before,
            bytes_read=size,
            field=field,
        )
    finally:
        os.close(descriptor)
    return b"".join(chunks), digest.hexdigest()


def _read_stable_bytes(path: Path, *, field: str) -> bytes:
    payload, _ = _read_stable_bytes_and_sha256(path, field=field)
    return payload


def _declared_sha256(value: object, *, field: str) -> str:
    digest = str(value or "").strip().casefold().removeprefix("sha256:")
    if _SHA256.fullmatch(digest) is None:
        raise _CandidateRejected("missing_hash", f"{field} has no valid SHA-256")
    return digest


def _load_json_object_and_sha256(path: Path, *, field: str) -> tuple[dict[str, Any], str]:
    try:
        payload, digest = _read_stable_bytes_and_sha256(path, field=field)
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise DataKitExportError(f"{field} is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise DataKitExportError(f"{field} must be a JSON object")
    return value, digest


def _load_json_object(path: Path, *, field: str) -> dict[str, Any]:
    value, _ = _load_json_object_and_sha256(path, field=field)
    return value


def _validate_data_pack(value: Mapping[str, Any]) -> None:
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
        raise DataKitExportError("DATA_PACK_MANIFEST has missing or extra fields")
    release_name = value.get("release_name")
    if not isinstance(release_name, str) or _RELEASE_NAME.fullmatch(release_name) is None:
        raise DataKitExportError("DATA_PACK_MANIFEST release_name is invalid")
    if (
        value.get("name") != "cardrag-conveyor data-kit"
        or value.get("data_kit_path") != "data-kit/cardrag-conveyor-data"
        or value.get("included_roots") != _EXPECTED_INCLUDED_ROOTS
    ):
        raise DataKitExportError("DATA_PACK_MANIFEST layout contract is invalid")
    sqlite_dbs = value.get("sqlite_dbs")
    if not isinstance(sqlite_dbs, list) or _INVENTORY_RELATIVE.as_posix() not in sqlite_dbs:
        raise DataKitExportError("DATA_PACK_MANIFEST does not declare inventory.sqlite3")
    counts = value.get("key_counts")
    if not isinstance(counts, dict):
        raise DataKitExportError("DATA_PACK_MANIFEST key_counts is invalid")
    for field in ("manifest_entries", "inventory_docs"):
        if not isinstance(counts.get(field), int) or isinstance(counts.get(field), bool) or counts[field] < 1:
            raise DataKitExportError(f"DATA_PACK_MANIFEST {field} is invalid")


def _validate_master(value: Mapping[str, Any], expected_count: int) -> tuple[dict[str, Any], ...]:
    if set(value) != {"schema_version", "entries"}:
        raise DataKitExportError("cardrag master manifest has missing or extra fields")
    if value.get("schema_version") != "cardrag_master_manifest.v2":
        raise DataKitExportError("cardrag master manifest schema is unsupported")
    raw_entries = value.get("entries")
    if not isinstance(raw_entries, list) or len(raw_entries) != expected_count:
        raise DataKitExportError("cardrag master manifest count disagrees with DATA_PACK_MANIFEST")
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_entries:
        if not isinstance(raw, dict):
            raise DataKitExportError("cardrag master manifest contains a non-object entry")
        if not _MASTER_REQUIRED.issubset(raw) or not set(raw).issubset(_MASTER_ALLOWED):
            raise DataKitExportError("cardrag master manifest entry schema is invalid")
        document_id = raw.get("doc_version_id")
        if not isinstance(document_id, str) or not document_id or document_id in seen:
            raise DataKitExportError("cardrag master manifest document IDs are invalid or duplicated")
        seen.add(document_id)
        entries.append(dict(raw))
    return tuple(entries)


def _reject_sqlite_sidecars(path: Path) -> None:
    for suffix in ("-journal", "-wal", "-shm"):
        if os.path.lexists(f"{path}{suffix}"):
            raise DataKitExportError("inventory SQLite has a live sidecar and is not immutable")


def _sqlite_rows_and_sha256(path: Path) -> tuple[tuple[dict[str, Any], ...], str]:
    _reject_sqlite_sidecars(path)
    payload, digest = _read_stable_bytes_and_sha256(path, field="inventory SQLite")
    _reject_sqlite_sidecars(path)
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(":memory:")
        connection.deserialize(payload)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise DataKitExportError("inventory SQLite integrity_check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise DataKitExportError("inventory SQLite foreign_key_check failed")
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        }
        if tables != {"documents", "index_state"}:
            raise DataKitExportError("inventory SQLite table set is unsupported")
        document_columns = tuple(str(row[1]) for row in connection.execute("PRAGMA table_info(documents)"))
        state_columns = tuple(str(row[1]) for row in connection.execute("PRAGMA table_info(index_state)"))
        if document_columns != _DOCUMENT_COLUMNS or state_columns != _INDEX_STATE_COLUMNS:
            raise DataKitExportError("inventory SQLite column contract is unsupported")
        rows = tuple(dict(row) for row in connection.execute("SELECT * FROM documents ORDER BY doc_id"))
        state_ids = [
            str(row[0]) for row in connection.execute("SELECT doc_id FROM index_state ORDER BY doc_id")
        ]
        if state_ids != [str(row["doc_id"]) for row in rows]:
            raise DataKitExportError("inventory SQLite index_state coverage is incomplete")
    except sqlite3.Error as exc:
        raise DataKitExportError("inventory SQLite cannot be read safely") from exc
    finally:
        if connection is not None:
            connection.close()
    _reject_sqlite_sidecars(path)
    return rows, digest


def _validate_database_master(
    database_rows: Sequence[Mapping[str, Any]],
    master_entries: Sequence[Mapping[str, Any]],
    data_pack: Mapping[str, Any],
) -> None:
    counts = data_pack["key_counts"]
    if len(database_rows) != counts["inventory_docs"]:
        raise DataKitExportError("inventory SQLite count disagrees with DATA_PACK_MANIFEST")
    master_by_id = {str(row["doc_version_id"]): row for row in master_entries}
    database_by_id = {str(row["doc_id"]): row for row in database_rows}
    if len(database_by_id) != len(database_rows) or set(database_by_id) != set(master_by_id):
        raise DataKitExportError("inventory SQLite and master manifest document sets disagree")
    issuer_counts: dict[str, int] = defaultdict(int)
    for document_id, database_row in database_by_id.items():
        master = master_by_id[document_id]
        for master_field, database_field in _MASTER_TO_DATABASE.items():
            if master.get(master_field) != database_row.get(database_field):
                raise DataKitExportError(
                    f"inventory SQLite and master manifest disagree for {document_id}/{database_field}"
                )
        if database_row.get("is_latest") not in {0, 1}:
            raise DataKitExportError("inventory SQLite has an invalid is_latest value")
        issuer = str(database_row["card_company"])
        if issuer not in _ISSUER_MAP:
            raise DataKitExportError(f"inventory SQLite has an unsupported issuer: {issuer}")
        issuer_counts[issuer] += 1
    declared_issuer_counts = counts.get("inventory_by_issuer")
    if not isinstance(declared_issuer_counts, list):
        raise DataKitExportError("DATA_PACK_MANIFEST inventory_by_issuer is invalid")
    try:
        normalized_counts = {str(key): int(value) for key, value in declared_issuer_counts}
    except (TypeError, ValueError) as exc:
        raise DataKitExportError("DATA_PACK_MANIFEST inventory_by_issuer is invalid") from exc
    if dict(issuer_counts) != normalized_counts:
        raise DataKitExportError("inventory issuer counts disagree with DATA_PACK_MANIFEST")
    grouped: dict[tuple[str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in database_rows:
        grouped[(str(row["card_company"]), str(row["product_code"]), str(row["doc_type"]))].append(row)
    if any(sum(row["is_latest"] == 1 for row in rows) != 1 for rows in grouped.values()):
        raise DataKitExportError("inventory SQLite must select exactly one latest row per product")


def _load_controls(source: Path, hasher: _FileHasher) -> SourceControls:
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
            raise DataKitExportError(f"{field} is missing or unsafe")
    data_pack, data_pack_sha = _load_json_object_and_sha256(
        data_pack_path,
        field="DATA_PACK_MANIFEST",
    )
    _validate_data_pack(data_pack)
    master, master_sha = _load_json_object_and_sha256(
        master_path,
        field="cardrag master manifest",
    )
    master_entries = _validate_master(master, int(data_pack["key_counts"]["manifest_entries"]))
    database_rows, inventory_sha = _sqlite_rows_and_sha256(inventory_path)
    _validate_database_master(database_rows, master_entries, data_pack)
    _reject_sqlite_sidecars(ocr_inventory_path)
    ocr_inventory_sha, _ = hasher.hash(ocr_inventory_path)
    _reject_sqlite_sidecars(ocr_inventory_path)
    if inventory_sha != ocr_inventory_sha:
        raise DataKitExportError("inventory.sqlite3 and ocr_inventory.sqlite3 are not identical")
    return SourceControls(
        data_pack=data_pack,
        master_entries=master_entries,
        database_rows=database_rows,
        data_pack_manifest_sha256=data_pack_sha,
        master_manifest_sha256=master_sha,
        inventory_sha256=inventory_sha,
        ocr_inventory_sha256=ocr_inventory_sha,
        source_database_id=f"data-kit-sqlite-v2-sha256:{inventory_sha}",
    )


def _canonical_ocr_text(payload: bytes, *, expected_pages: int) -> tuple[str, tuple[str, ...]]:
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
    return text, pages


def _normalize_ocr(
    payload: bytes,
    *,
    issuer: str,
    expected_pages: int,
    expected_sha256: str,
) -> tuple[bytes, str, str | None, int, str]:
    source_sha = hashlib.sha256(payload).hexdigest()
    if source_sha != expected_sha256:
        raise _CandidateRejected("ocr_hash_mismatch", "OCR bytes differ from metadata SHA-256")
    try:
        _canonical_ocr_text(payload, expected_pages=expected_pages)
    except _CandidateRejected as exact_error:
        if issuer != "woori" or not payload.startswith(WOORI_GENERATED_PREFIX):
            raise exact_error
        normalized = payload[len(WOORI_GENERATED_PREFIX) :]
        if not normalized.startswith(b"## Page 1"):
            raise _CandidateRejected(
                "ocr_prefix_near_match",
                "the generated Woori prefix is not immediately followed by Page 1",
            ) from exact_error
        _canonical_ocr_text(normalized, expected_pages=expected_pages)
        return (
            normalized,
            WOORI_PREFIX_PROFILE,
            WOORI_GENERATED_PREFIX_SHA256,
            len(WOORI_GENERATED_PREFIX),
            normalized.decode("utf-8"),
        )
    return payload, EXACT_PROFILE, None, 0, payload.decode("utf-8")


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
        relative = "artifacts/raw-pdfs/" + output_base.removeprefix("artifacts/ocr/")
        relative += f"__{file_name}"
        try:
            derived = _source_file(source, relative, field="derived raw PDF path")
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
    controls: SourceControls,
    all_pdfs: Sequence[Path],
    hasher: _FileHasher,
) -> CandidatePlan:
    document_id = str(database["doc_id"])
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
        raise DataKitExportError(f"unsafe document identity in SQLite: {document_id}")
    try:
        if (
            Path(effective_date).name != effective_date
            or date.fromisoformat(effective_date).isoformat() != effective_date
        ):
            raise ValueError
    except ValueError as exc:
        raise DataKitExportError(f"invalid effective date in SQLite: {document_id}") from exc

    expected_pdf = _declared_sha256(master.get("pdf_sha256"), field="master pdf_sha256")
    pdf_path = _candidate_pdf_path(
        source,
        master,
        expected_sha256=expected_pdf,
        all_pdfs=all_pdfs,
        hasher=hasher,
    )
    try:
        actual_pdf, pdf_size, page_count = validate_pdf(pdf_path, expected_sha256=expected_pdf)
    except (PDFValidationError, OSError) as exc:
        raise _CandidateRejected("pdf_invalid", "PDF cannot be fully validated") from exc
    if page_count != database.get("pages"):
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
        or metadata.get("doc_version_id") != document_id
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
        metadata_pdf_path = _source_file(source, metadata_pdf_relative, field="metadata raw_pdf_rel_path")
        metadata_path_sha, _ = hasher.hash(metadata_pdf_path)
        if metadata_path_sha != actual_pdf:
            raise _CandidateRejected("metadata_pdf_hash", "OCR metadata PDF path differs")

    ocr_path = _source_file(source, master.get("ocr_remote_rel"), field="ocr_remote_rel")
    source_ocr_sha = _declared_sha256(metadata.get("ocr_md_sha256"), field="metadata ocr_md_sha256")
    source_payload = _read_stable_bytes(ocr_path, field="OCR object")
    normalized, profile, prefix_sha, removed_bytes, source_or_normalized_text = _normalize_ocr(
        source_payload,
        issuer=issuer,
        expected_pages=page_count,
        expected_sha256=source_ocr_sha,
    )
    try:
        source_text = source_payload.decode("utf-8")
    except UnicodeDecodeError as exc:  # already checked, but keeps the metadata check explicit
        raise _CandidateRejected("ocr_invalid_utf8", "OCR is not valid UTF-8") from exc
    if metadata.get("ocr_md_chars") != len(source_text):
        raise _CandidateRejected("ocr_char_count", "OCR character count differs from metadata")
    del source_or_normalized_text
    normalized_sha = hashlib.sha256(normalized).hexdigest()
    normalized_relative = (
        f"objects/ocr/sha256/{normalized_sha[:2]}/{normalized_sha}.md" if profile != EXACT_PROFILE else None
    )
    source_relative = ocr_path.relative_to(source).as_posix()
    common_lineage = {
        "ledger_ocr_sha256": source_ocr_sha,
        "normalization_profile": profile,
        "normalized_ocr_sha256": normalized_sha,
        "normalized_ocr_size_bytes": len(normalized),
        "prefix_sha256": prefix_sha,
        "removed_bytes": removed_bytes,
        "source_ocr_sha256": source_ocr_sha,
        "source_ocr_size_bytes": len(source_payload),
    }
    inventory = {
        "schema_version": INVENTORY_SCHEMA,
        "policy_version": ADOPTION_POLICY_VERSION,
        "issuer": issuer,
        "product_code": product_code,
        "source_database_id": controls.source_database_id,
        "source_data_pack_manifest_sha256": controls.data_pack_manifest_sha256,
        "source_inventory_sha256": controls.inventory_sha256,
        "source_master_manifest_sha256": controls.master_manifest_sha256,
        "source_ocr_inventory_sha256": controls.ocr_inventory_sha256,
        "source_document_id": document_id,
        "legacy_source_document_id": document_id,
        "document_type": document_type,
        "effective_date": effective_date,
        "source_version": source_version,
        "pdf_path": str(pdf_path),
        "source_ocr_path": str(ocr_path),
        "ocr_path": str(ocr_path),
        "ledger_pdf_sha256": actual_pdf,
        "pdf_size_bytes": pdf_size,
        "page_count": page_count,
        **common_lineage,
    }
    receipt = {
        "schema_version": RECEIPT_SCHEMA,
        "policy_version": ADOPTION_POLICY_VERSION,
        "controls": controls.identity_fields(),
        "issuer": issuer,
        "product_code": product_code,
        "source_database_id": controls.source_database_id,
        "source_document_id": document_id,
        "source_ocr_relative_path": source_relative,
        "normalized_ocr_object_relative_path": normalized_relative,
        **common_lineage,
    }
    return CandidatePlan(
        inventory=inventory,
        receipt=receipt,
        source_ocr_path=ocr_path,
        normalized_object_relative=normalized_relative,
    )


def _row_order(row: Mapping[str, Any]) -> tuple[str, str, str]:
    return str(row["issuer"]), str(row["product_code"]), str(row["source_document_id"])


def compute_source_bundle_sha256(
    controls: SourceControls,
    transformations: Sequence[Mapping[str, Any]],
) -> str:
    """Bind original controls, policy, and the complete sorted transformation plan."""

    return canonical_sha256(
        {
            "policy_version": ADOPTION_POLICY_VERSION,
            "controls": controls.identity_fields(),
            "schema_version": SOURCE_SCHEMA,
            "transformations": sorted((dict(row) for row in transformations), key=_row_order),
        }
    )


def plan_data_kit_adoption_v2(source_root: Path) -> DataKitExportPlan:
    """Validate the immutable source and construct a deterministic export plan."""

    if not source_root.is_absolute():
        raise DataKitExportError("data-kit source must be an absolute path")
    _reject_symlink_components(source_root, field="data-kit source")
    source = source_root.resolve(strict=True)
    if not source.is_dir():
        raise DataKitExportError("data-kit source must be a directory")
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
        raise DataKitExportError("data-kit has no latest successful documents")
    candidates: list[CandidatePlan] = []
    rejected: list[dict[str, Any]] = []
    for database in selected:
        document_id = str(database["doc_id"])
        try:
            candidates.append(
                _validated_candidate(
                    source,
                    database,
                    master_by_id[document_id],
                    controls=controls,
                    all_pdfs=all_pdfs,
                    hasher=hasher,
                )
            )
        except _CandidateRejected as exc:
            rejected.append(
                {
                    "schema_version": REJECTION_SCHEMA,
                    "policy_version": ADOPTION_POLICY_VERSION,
                    "controls": controls.identity_fields(),
                    "source_database_id": controls.source_database_id,
                    "source_document_id": document_id,
                    "issuer": _ISSUER_MAP.get(str(database.get("card_company")), "unknown"),
                    "product_code": str(database.get("product_code") or ""),
                    "reason": exc.code,
                    "detail": str(exc),
                }
            )
    candidates.sort(key=lambda candidate: _row_order(candidate.inventory))
    transformations = [candidate.transformation_identity() for candidate in candidates]
    bundle_sha = compute_source_bundle_sha256(controls, transformations)
    bundle_id = f"data-kit-v2-{bundle_sha[:12]}"
    bound_candidates = tuple(
        replace(
            candidate,
            inventory={
                **candidate.inventory,
                "source_bundle_id": bundle_id,
                "source_bundle_sha256": bundle_sha,
            },
            receipt={
                **candidate.receipt,
                "source_bundle_id": bundle_id,
                "source_bundle_sha256": bundle_sha,
            },
        )
        for candidate in candidates
    )
    bound_rejected = tuple(
        {
            **row,
            "source_bundle_id": bundle_id,
            "source_bundle_sha256": bundle_sha,
        }
        for row in sorted(rejected, key=_row_order)
    )
    return DataKitExportPlan(
        source_root=source,
        controls=controls,
        candidates=bound_candidates,
        rejected=bound_rejected,
        selected_documents=len(selected),
        source_bundle_id=bundle_id,
        source_bundle_sha256=bundle_sha,
    )


def canonical_jsonl(rows: Sequence[Mapping[str, Any]]) -> bytes:
    return b"".join(canonical_json_bytes(row) + b"\n" for row in sorted(rows, key=_row_order))


def _materialize_normalized(candidate: CandidatePlan, writer: _ExportWriter) -> None:
    relative = candidate.normalized_object_relative
    if relative is None:
        return
    source_payload = _read_stable_bytes(candidate.source_ocr_path, field="OCR object during export")
    source_sha = hashlib.sha256(source_payload).hexdigest()
    if source_sha != candidate.receipt["source_ocr_sha256"]:
        raise DataKitExportError("source OCR changed after validation")
    if candidate.receipt["normalization_profile"] != WOORI_PREFIX_PROFILE:
        raise DataKitExportError("unsupported normalization profile in export plan")
    if not source_payload.startswith(WOORI_GENERATED_PREFIX):
        raise DataKitExportError("source OCR no longer has the approved exact prefix")
    normalized = source_payload[len(WOORI_GENERATED_PREFIX) :]
    if (
        hashlib.sha256(normalized).hexdigest() != candidate.receipt["normalized_ocr_sha256"]
        or len(normalized) != candidate.receipt["normalized_ocr_size_bytes"]
    ):
        raise DataKitExportError("normalized OCR changed after validation")
    writer.create(relative, normalized)


def _create_export_root(parent_descriptor: int, name: str) -> tuple[int, tuple[int, int, int]]:
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent_descriptor)
    except FileExistsError as exc:
        raise DataKitExportError(f"data-kit export output already exists: {name}") from exc
    descriptor, metadata = _open_directory_at(
        parent_descriptor,
        name,
        field="data-kit export output",
    )
    os.fchmod(descriptor, 0o700)
    identity = _node_identity(metadata)
    named = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    if _node_identity(named) != identity or stat.S_IMODE(named.st_mode) != 0o700:
        os.close(descriptor)
        raise DataKitExportError("data-kit export output changed while it was created")
    return descriptor, identity


def _verify_directory_entry(
    parent_descriptor: int,
    name: str,
    descriptor: int,
    identity: tuple[int, int, int],
    *,
    field: str,
) -> None:
    try:
        named = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except OSError as exc:
        raise DataKitExportError(f"{field} changed while it was written") from exc
    if (
        _node_identity(named) != identity
        or _node_identity(os.fstat(descriptor)) != identity
        or not stat.S_ISDIR(named.st_mode)
        or stat.S_IMODE(named.st_mode) != 0o700
    ):
        raise DataKitExportError(f"{field} changed while it was written")


def _remove_directory_if_identity(
    parent_descriptor: int,
    name: str,
    identity: tuple[int, int, int],
) -> None:
    try:
        named = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except OSError:
        return
    if _node_identity(named) != identity:
        return
    try:
        os.rmdir(name, dir_fd=parent_descriptor)
    except OSError:
        return


def export_data_kit_adoption_v2(source_root: Path, output_root: Path) -> DataKitExportResult:
    """Validate *source_root* and create a new v2 export at *output_root*."""

    if not source_root.is_absolute():
        raise DataKitExportError("data-kit source must be an absolute path")
    # Preserve the operator-supplied path until every component has been
    # audited. Resolving first would erase evidence that the source root itself
    # (or one of its parents) was a symlink.
    _reject_symlink_components(source_root, field="data-kit source")
    source = source_root.resolve(strict=True)
    if not output_root.is_absolute():
        raise DataKitExportError("data-kit export output must be an absolute path")
    if output_root.name in {"", ".", ".."} or "\x00" in output_root.name:
        raise DataKitExportError("data-kit export output name is unsafe")
    _reject_symlink_components(output_root.parent, field="data-kit export parent")
    parent_descriptor, _ = _open_absolute_directory_nofollow(
        output_root.parent,
        field="data-kit export parent",
    )
    root_descriptor: int | None = None
    root_identity: tuple[int, int, int] | None = None
    writer: _ExportWriter | None = None
    try:
        parent = output_root.parent.resolve(strict=True)
        _verify_absolute_directory(
            output_root.parent,
            parent_descriptor,
            field="data-kit export parent",
        )
        destination = parent / output_root.name
        if destination == source or destination.is_relative_to(source):
            raise DataKitExportError("data-kit export output must be outside the source")
        try:
            os.stat(output_root.name, dir_fd=parent_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise DataKitExportError(f"data-kit export output already exists: {destination}")

        plan = plan_data_kit_adoption_v2(source_root)
        _verify_absolute_directory(
            output_root.parent,
            parent_descriptor,
            field="data-kit export parent",
        )
        root_descriptor, root_identity = _create_export_root(
            parent_descriptor,
            output_root.name,
        )
        writer = _ExportWriter(root_descriptor)
        normalized_by_sha: dict[str, CandidatePlan] = {}
        for candidate in plan.candidates:
            if candidate.normalized_object_relative is not None:
                normalized_by_sha.setdefault(str(candidate.receipt["normalized_ocr_sha256"]), candidate)
        for digest in sorted(normalized_by_sha):
            _materialize_normalized(normalized_by_sha[digest], writer)

        rows: list[dict[str, Any]] = []
        receipts: list[dict[str, Any]] = []
        for candidate in plan.candidates:
            if candidate.normalized_object_relative is None:
                ocr_path = candidate.source_ocr_path
            else:
                ocr_path = destination / candidate.normalized_object_relative
            rows.append({**candidate.inventory, "ocr_path": str(ocr_path)})
            receipts.append(candidate.receipt)
        inventory_payload = canonical_jsonl(rows)
        rejected_payload = canonical_jsonl(plan.rejected)
        receipts_payload = canonical_jsonl(receipts)
        inventory_path = destination / "inventory.jsonl"
        rejected_path = destination / "rejected.jsonl"
        receipts_path = destination / "normalization-receipts.jsonl"
        writer.create("inventory.jsonl", inventory_payload)
        writer.create("rejected.jsonl", rejected_payload)
        writer.create("normalization-receipts.jsonl", receipts_payload)
        exact = sum(
            candidate.receipt["normalization_profile"] == EXACT_PROFILE for candidate in plan.candidates
        )
        normalized = len(plan.candidates) - exact
        manifest = {
            "schema_version": EXPORT_MANIFEST_SCHEMA,
            "policy_version": ADOPTION_POLICY_VERSION,
            "source_root": str(plan.source_root),
            "source_bundle_id": plan.source_bundle_id,
            "source_bundle_sha256": plan.source_bundle_sha256,
            "source_database_id": plan.controls.source_database_id,
            "selected_documents": plan.selected_documents,
            "accepted_documents": len(plan.candidates),
            "rejected_documents": len(plan.rejected),
            "exact_documents": exact,
            "normalized_documents": normalized,
            "inventory_sha256": hashlib.sha256(inventory_payload).hexdigest(),
            "rejected_sha256": hashlib.sha256(rejected_payload).hexdigest(),
            "receipts_sha256": hashlib.sha256(receipts_payload).hexdigest(),
            "normalized_objects": len(normalized_by_sha),
        }
        manifest_path = destination / "export-manifest.json"
        writer.create("export-manifest.json", canonical_json_bytes(manifest) + b"\n")
        writer.verify()
        _verify_directory_entry(
            parent_descriptor,
            output_root.name,
            root_descriptor,
            root_identity,
            field="data-kit export output",
        )
        _verify_absolute_directory(
            output_root.parent,
            parent_descriptor,
            field="data-kit export parent",
        )
    except BaseException:
        if writer is not None:
            writer.cleanup()
        if root_identity is not None:
            _remove_directory_if_identity(parent_descriptor, output_root.name, root_identity)
        raise
    finally:
        if writer is not None:
            writer.close()
        if root_descriptor is not None:
            os.close(root_descriptor)
        os.close(parent_descriptor)
    return DataKitExportResult(
        output_root=destination,
        inventory_path=inventory_path,
        rejected_path=rejected_path,
        receipts_path=receipts_path,
        manifest_path=manifest_path,
        accepted_documents=len(plan.candidates),
        rejected_documents=len(plan.rejected),
        exact_documents=exact,
        normalized_documents=normalized,
        source_bundle_id=plan.source_bundle_id,
        source_bundle_sha256=plan.source_bundle_sha256,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate and export the legacy CardRAG data-kit under adoption policy v2."
    )
    parser.add_argument("--source", required=True, type=Path, help="absolute read-only data-kit root")
    parser.add_argument("--output", required=True, type=Path, help="absolute new output directory")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        result = export_data_kit_adoption_v2(arguments.source, arguments.output)
    except (DataKitExportError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "accepted": result.accepted_documents,
                "exact": result.exact_documents,
                "normalized": result.normalized_documents,
                "output": str(result.output_root),
                "rejected": result.rejected_documents,
                "source_bundle_id": result.source_bundle_id,
                "source_bundle_sha256": result.source_bundle_sha256,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
