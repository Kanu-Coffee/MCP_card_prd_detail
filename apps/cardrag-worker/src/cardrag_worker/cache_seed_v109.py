"""Fail-closed v1.0.9 PDF CAS inventory and v1.0.11 seed import.

The source is an immutable, read-only v1.0.9 worker state directory.  This
module never opens the source database for writing and never creates files
below the source root.  A successful apply imports through :class:`PDFCache`
and writes one content-addressed audit ledger below the destination state.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import uuid
from collections import defaultdict
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from .contracts import canonical_json_bytes
from .downloader import PDFValidationError, validate_pdf
from .pdf_cache import PDFCache, PDFSourceIdentity

LEDGER_SCHEMA_VERSION = "cardrag.cache-seed-v109-ledger.v1"
REPORT_SCHEMA_VERSION = "cardrag.cache-seed-v109-report.v1"
PIN_STATUS = "active_until_first_full_v5_seal"

MAX_DATABASE_BYTES = 1024 * 1024 * 1024
MAX_PDF_BYTES = 100 * 1024 * 1024
MAX_OBJECTS = 100_000
MAX_SOURCES = 100_000
MAX_REVISIONS = 500_000
MAX_LEDGER_BYTES = 256 * 1024 * 1024

_LEDGER_DIRECTORY = Path("audit-reports/cache-seed-v109")
_DATABASE_NAME = "worker-state.sqlite3"
_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_ID = re.compile(r"^source_[0-9a-f]{64}$")
_PREFIX = re.compile(r"^[0-9a-f]{2}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_TERMINAL_RUN_STATUSES = frozenset({"succeeded", "failed", "no_change", "interrupted"})

_RUN_COLUMNS = (
    "run_id",
    "started_at",
    "finished_at",
    "status",
    "corpus_sha256",
    "contract_sha256",
    "error",
)
_OBJECT_COLUMNS = (
    "pdf_sha256",
    "size_bytes",
    "page_count",
    "relative_path",
    "created_at",
    "last_verified_at",
)
_SOURCE_COLUMNS = (
    "source_id",
    "issuer",
    "product_code",
    "document_type",
    "source_url",
    "source_version",
    "source_post_id",
    "discovery_sha256",
    "first_observed_at",
    "last_observed_at",
    "last_verified_at",
    "superseded_by_source_id",
    "superseded_at",
)
_REVISION_COLUMNS = (
    "revision_id",
    "source_id",
    "pdf_sha256",
    "pdf_size_bytes",
    "page_count",
    "final_url",
    "etag",
    "last_modified",
    "first_observed_at",
    "last_observed_at",
    "verified_at",
    "superseded_at",
    "previous_revision_id",
)

type FileIdentity = tuple[int, int, int, int, int]
type EntryStatus = Literal["accepted", "missing"]


class V109CacheSeedError(RuntimeError):
    """A bounded error code safe to expose in release audit output."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class V109SeedObject:
    pdf_sha256: str
    size_bytes: int
    page_count: int
    relative_path: str
    source_path: Path
    status: EntryStatus
    missing_reason: str | None
    file_identity: FileIdentity | None


@dataclass(frozen=True, slots=True)
class V109SeedSource:
    identity: PDFSourceIdentity
    first_observed_at: datetime
    last_observed_at: datetime
    last_verified_at: datetime
    superseded_by_source_id: str | None
    superseded_at: datetime | None
    status: EntryStatus
    missing_reason: str | None


@dataclass(frozen=True, slots=True)
class V109SeedRevision:
    revision_id: int
    previous_revision_id: int | None
    source_id: str
    pdf_sha256: str
    pdf_size_bytes: int
    page_count: int
    final_url: str
    etag: str | None
    last_modified: str | None
    first_observed_at: datetime
    last_observed_at: datetime
    verified_at: datetime
    superseded_at: datetime | None
    status: EntryStatus
    missing_reason: str | None


@dataclass(frozen=True, slots=True)
class V109CacheSeedPlan:
    source_root: Path
    database_path: Path
    source_root_identity: tuple[int, int]
    database_identity: FileIdentity
    source_database_sha256: str
    latest_run_id: str | None
    latest_run_status: str | None
    objects: tuple[V109SeedObject, ...]
    sources: tuple[V109SeedSource, ...]
    revisions: tuple[V109SeedRevision, ...]
    ledger_bytes: bytes
    ledger_sha256: str

    @property
    def accepted_pdf_hashes(self) -> frozenset[str]:
        return frozenset(item.pdf_sha256 for item in self.objects if item.status == "accepted")

    @property
    def accepted_revisions(self) -> tuple[V109SeedRevision, ...]:
        return tuple(item for item in self.revisions if item.status == "accepted")

    def report(
        self,
        *,
        applied: bool,
        imported_pdf_objects: int = 0,
        reused_pdf_objects: int = 0,
        imported_revisions: int = 0,
        reused_revisions: int = 0,
        ledger_path: str | None = None,
    ) -> dict[str, Any]:
        missing_objects = sum(item.status == "missing" for item in self.objects)
        missing_sources = sum(item.status == "missing" for item in self.sources)
        missing_revisions = sum(item.status == "missing" for item in self.revisions)
        return {
            "accepted_pdf_objects": len(self.accepted_pdf_hashes),
            "accepted_revisions": len(self.accepted_revisions),
            "applied": applied,
            "dry_run": not applied,
            "imported_pdf_objects": imported_pdf_objects,
            "imported_revisions": imported_revisions,
            "latest_run_id": self.latest_run_id,
            "latest_run_status": self.latest_run_status,
            "ledger_path": ledger_path,
            "ledger_sha256": self.ledger_sha256,
            "ledger_size_bytes": len(self.ledger_bytes),
            "missing_objects": missing_objects,
            "missing_revisions": missing_revisions,
            "missing_sources": missing_sources,
            "reused_pdf_objects": reused_pdf_objects,
            "reused_revisions": reused_revisions,
            "schema_version": REPORT_SCHEMA_VERSION,
            "source_database_sha256": self.source_database_sha256,
            "status": "applied" if applied else "verified",
            "total_pdf_objects": len(self.objects),
            "total_revisions": len(self.revisions),
            "total_sources": len(self.sources),
        }


def _absolute_without_resolving(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _components(path: Path) -> tuple[Path, ...]:
    absolute = _absolute_without_resolving(path)
    current = Path(absolute.anchor)
    result = [current]
    for part in absolute.parts[1:]:
        current /= part
        result.append(current)
    return tuple(result)


def _require_secure_directory(path: Path, *, missing_ok: bool = False) -> bool:
    components = _components(path)
    for index, component in enumerate(components):
        try:
            mode = component.lstat().st_mode
        except FileNotFoundError:
            if missing_ok and index == len(components) - 1:
                return False
            raise V109CacheSeedError("unsafe_source_path") from None
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise V109CacheSeedError("unsafe_source_path")
    return True


def _require_secure_directory_tree_or_missing(path: Path, *, code: str) -> bool:
    """Return false at the first absent suffix while still rejecting symlinks."""

    for component in _components(path):
        try:
            mode = component.lstat().st_mode
        except FileNotFoundError:
            return False
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise V109CacheSeedError(code)
    return True


def _identity(value: os.stat_result) -> FileIdentity:
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _open_regular(path: Path, *, code: str) -> int:
    _require_secure_directory(path.parent)
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        raise V109CacheSeedError(code) from None
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise V109CacheSeedError(code)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise V109CacheSeedError(code) from exc
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise V109CacheSeedError(code)
    return descriptor


def _hash_descriptor(descriptor: int) -> str:
    digest = hashlib.sha256()
    os.lseek(descriptor, 0, os.SEEK_SET)
    while block := os.read(descriptor, 1024 * 1024):
        digest.update(block)
    os.lseek(descriptor, 0, os.SEEK_SET)
    return digest.hexdigest()


def _parse_timestamp(value: object, *, optional: bool = False) -> datetime | None:
    if value is None:
        if optional:
            return None
        raise V109CacheSeedError("invalid_source_metadata")
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise V109CacheSeedError("invalid_source_metadata") from exc
    if parsed.tzinfo is None:
        raise V109CacheSeedError("invalid_source_metadata")
    return parsed.astimezone(UTC)


def _required_text(value: object, *, maximum: int = 4096, empty_ok: bool = False) -> str:
    if not isinstance(value, str):
        raise V109CacheSeedError("invalid_source_metadata")
    if value != value.strip() or len(value) > maximum or _CONTROL.search(value):
        raise V109CacheSeedError("invalid_source_metadata")
    if not value and not empty_ok:
        raise V109CacheSeedError("invalid_source_metadata")
    return value


def _optional_header(value: object) -> str | None:
    if value is None:
        return None
    return _required_text(value, maximum=2048)


def _https_url(value: object) -> str:
    url = _required_text(value)
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
    ):
        raise V109CacheSeedError("invalid_source_metadata")
    return url


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _table_columns(connection: sqlite3.Connection, table: str) -> tuple[str, ...]:
    safe_tables = {
        "run",
        "pdf_cache_object",
        "pdf_cache_source",
        "pdf_cache_source_revision",
    }
    if table not in safe_tables:  # pragma: no cover - internal invariant
        raise AssertionError("unexpected table")
    return tuple(str(row["name"]) for row in connection.execute(f"PRAGMA table_info({table})"))


def _read_database(
    database_path: Path,
) -> tuple[
    FileIdentity,
    str,
    str | None,
    str | None,
    tuple[sqlite3.Row, ...],
    tuple[sqlite3.Row, ...],
    tuple[sqlite3.Row, ...],
]:
    for suffix in _SIDECAR_SUFFIXES:
        sidecar = Path(f"{database_path}{suffix}")
        try:
            sidecar.lstat()
        except FileNotFoundError:
            continue
        raise V109CacheSeedError("source_database_not_checkpointed")

    descriptor = _open_regular(database_path, code="source_database_missing_or_unsafe")
    before = _identity(os.fstat(descriptor))
    if before[2] <= 0 or before[2] > MAX_DATABASE_BYTES:
        os.close(descriptor)
        raise V109CacheSeedError("source_database_size_invalid")
    database_sha256 = _hash_descriptor(descriptor)
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            f"file:/proc/self/fd/{descriptor}?mode=ro&immutable=1",
            uri=True,
            timeout=5,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or str(integrity[0]) != "ok":
            raise V109CacheSeedError("source_database_integrity_failed")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise V109CacheSeedError("source_database_foreign_key_failed")
        expected = {
            "run": _RUN_COLUMNS,
            "pdf_cache_object": _OBJECT_COLUMNS,
            "pdf_cache_source": _SOURCE_COLUMNS,
            "pdf_cache_source_revision": _REVISION_COLUMNS,
        }
        for table, columns in expected.items():
            if _table_columns(connection, table) != columns:
                raise V109CacheSeedError("source_database_schema_mismatch")

        runs = tuple(
            connection.execute(
                "SELECT * FROM run ORDER BY started_at DESC,run_id DESC LIMIT 10001"
            ).fetchall()
        )
        if len(runs) > 10_000:
            raise V109CacheSeedError("source_database_limit_exceeded")
        latest_run_id: str | None = None
        latest_run_status: str | None = None
        for index, row in enumerate(runs):
            run_id = _required_text(row["run_id"], maximum=128)
            status = _required_text(row["status"], maximum=32)
            started = _parse_timestamp(row["started_at"])
            finished = _parse_timestamp(row["finished_at"], optional=True)
            if started is None:  # pragma: no cover - parser contract
                raise AssertionError("required timestamp returned None")
            if index == 0:
                latest_run_id, latest_run_status = run_id, status
            if status == "running":
                raise V109CacheSeedError("source_run_active")
            if status not in _TERMINAL_RUN_STATUSES or finished is None or finished < started:
                raise V109CacheSeedError("source_run_not_terminal")

        objects = tuple(connection.execute("SELECT * FROM pdf_cache_object ORDER BY pdf_sha256").fetchall())
        sources = tuple(connection.execute("SELECT * FROM pdf_cache_source ORDER BY source_id").fetchall())
        revisions = tuple(
            connection.execute(
                "SELECT * FROM pdf_cache_source_revision ORDER BY source_id,revision_id"
            ).fetchall()
        )
        if len(objects) > MAX_OBJECTS or len(sources) > MAX_SOURCES or len(revisions) > MAX_REVISIONS:
            raise V109CacheSeedError("source_database_limit_exceeded")
    except sqlite3.Error as exc:
        raise V109CacheSeedError("source_database_read_failed") from exc
    finally:
        if connection is not None:
            connection.close()
        after = _identity(os.fstat(descriptor))
        os.close(descriptor)
    if after != before:
        raise V109CacheSeedError("source_database_changed")
    return (
        before,
        database_sha256,
        latest_run_id,
        latest_run_status,
        objects,
        sources,
        revisions,
    )


def _scan_cas(objects_root: Path) -> frozenset[str]:
    if not _require_secure_directory_tree_or_missing(objects_root, code="unsafe_source_cas"):
        return frozenset()
    hashes: set[str] = set()
    try:
        prefixes = sorted(objects_root.iterdir(), key=lambda path: path.name)
    except OSError as exc:
        raise V109CacheSeedError("unsafe_source_cas") from exc
    for prefix in prefixes:
        try:
            prefix_mode = prefix.lstat().st_mode
        except OSError as exc:
            raise V109CacheSeedError("unsafe_source_cas") from exc
        if not _PREFIX.fullmatch(prefix.name) or stat.S_ISLNK(prefix_mode) or not stat.S_ISDIR(prefix_mode):
            raise V109CacheSeedError("unsafe_source_cas")
        try:
            leaves = sorted(prefix.iterdir(), key=lambda path: path.name)
        except OSError as exc:
            raise V109CacheSeedError("unsafe_source_cas") from exc
        for leaf in leaves:
            try:
                leaf_stat = leaf.lstat()
            except OSError as exc:
                raise V109CacheSeedError("unsafe_source_cas") from exc
            if (
                not _SHA256.fullmatch(leaf.name)
                or not leaf.name.startswith(prefix.name)
                or stat.S_ISLNK(leaf_stat.st_mode)
                or not stat.S_ISREG(leaf_stat.st_mode)
            ):
                raise V109CacheSeedError("unsafe_source_cas")
            if leaf.name in hashes:
                raise V109CacheSeedError("unsafe_source_cas")
            hashes.add(leaf.name)
            if len(hashes) > MAX_OBJECTS:
                raise V109CacheSeedError("source_database_limit_exceeded")
    return frozenset(hashes)


def _validate_object_rows(rows: tuple[sqlite3.Row, ...], objects_root: Path) -> tuple[V109SeedObject, ...]:
    actual_hashes = _scan_cas(objects_root)
    database_hashes: set[str] = set()
    result: list[V109SeedObject] = []
    for row in rows:
        pdf_sha256 = _required_text(row["pdf_sha256"], maximum=64)
        if not _SHA256.fullmatch(pdf_sha256) or pdf_sha256 in database_hashes:
            raise V109CacheSeedError("invalid_source_metadata")
        database_hashes.add(pdf_sha256)
        size_bytes = int(row["size_bytes"])
        page_count = int(row["page_count"])
        if size_bytes <= 0 or size_bytes > MAX_PDF_BYTES or page_count <= 0:
            raise V109CacheSeedError("invalid_source_metadata")
        expected_relative = f"objects/sha256/{pdf_sha256[:2]}/{pdf_sha256}"
        relative_path = _required_text(row["relative_path"], maximum=256)
        if relative_path != expected_relative:
            raise V109CacheSeedError("source_cas_path_mismatch")
        _parse_timestamp(row["created_at"])
        _parse_timestamp(row["last_verified_at"])
        source_path = objects_root.parent.parent / relative_path
        if pdf_sha256 not in actual_hashes:
            result.append(
                V109SeedObject(
                    pdf_sha256,
                    size_bytes,
                    page_count,
                    relative_path,
                    source_path,
                    "missing",
                    "cas_object_missing",
                    None,
                )
            )
            continue
        descriptor = _open_regular(source_path, code="unsafe_source_cas")
        before = _identity(os.fstat(descriptor))
        try:
            try:
                digest, actual_size, actual_pages = validate_pdf(
                    Path(f"/proc/self/fd/{descriptor}"), expected_sha256=pdf_sha256
                )
            except (OSError, PDFValidationError) as exc:
                raise V109CacheSeedError("source_pdf_validation_failed") from exc
            after = _identity(os.fstat(descriptor))
        finally:
            os.close(descriptor)
        if before != after:
            raise V109CacheSeedError("source_pdf_changed")
        if (digest, actual_size, actual_pages) != (pdf_sha256, size_bytes, page_count):
            raise V109CacheSeedError("source_pdf_metadata_mismatch")
        result.append(
            V109SeedObject(
                pdf_sha256,
                size_bytes,
                page_count,
                relative_path,
                source_path,
                "accepted",
                None,
                before,
            )
        )
    if actual_hashes != database_hashes.intersection(actual_hashes):
        raise V109CacheSeedError("source_cas_orphan_object")
    return tuple(result)


def _source_rows(rows: tuple[sqlite3.Row, ...]) -> tuple[V109SeedSource, ...]:
    result: list[V109SeedSource] = []
    seen: set[str] = set()
    for row in rows:
        source_id = _required_text(row["source_id"], maximum=71)
        discovery_sha256 = _required_text(row["discovery_sha256"], maximum=64)
        if (
            not _SOURCE_ID.fullmatch(source_id)
            or not _SHA256.fullmatch(discovery_sha256)
            or source_id != f"source_{discovery_sha256}"
            or source_id in seen
        ):
            raise V109CacheSeedError("invalid_source_metadata")
        seen.add(source_id)
        first = _parse_timestamp(row["first_observed_at"])
        last = _parse_timestamp(row["last_observed_at"])
        verified = _parse_timestamp(row["last_verified_at"])
        superseded = _parse_timestamp(row["superseded_at"], optional=True)
        if first is None or last is None or verified is None or last < first:
            raise V109CacheSeedError("invalid_source_metadata")
        superseded_by_value = row["superseded_by_source_id"]
        superseded_by = (
            None if superseded_by_value is None else _required_text(superseded_by_value, maximum=71)
        )
        if (superseded_by is None) != (superseded is None):
            raise V109CacheSeedError("invalid_source_metadata")
        result.append(
            V109SeedSource(
                identity=PDFSourceIdentity(
                    source_id=source_id,
                    issuer=_required_text(row["issuer"], maximum=64),
                    product_code=_required_text(row["product_code"], maximum=512),
                    document_type=_required_text(row["document_type"], maximum=128),
                    source_url=_https_url(row["source_url"]),
                    source_version=_required_text(row["source_version"], maximum=512),
                    source_post_id=_required_text(row["source_post_id"], maximum=512, empty_ok=True),
                    discovery_sha256=discovery_sha256,
                ),
                first_observed_at=first,
                last_observed_at=last,
                last_verified_at=verified,
                superseded_by_source_id=superseded_by,
                superseded_at=superseded,
                status="accepted",
                missing_reason=None,
            )
        )
    by_id = {item.identity.source_id: item for item in result}
    active_by_product: defaultdict[tuple[str, str, str], list[str]] = defaultdict(list)
    for item in result:
        logical = (item.identity.issuer, item.identity.product_code, item.identity.document_type)
        if item.superseded_by_source_id is None:
            active_by_product[logical].append(item.identity.source_id)
            continue
        target = by_id.get(item.superseded_by_source_id)
        if target is None or target.identity.source_id == item.identity.source_id:
            raise V109CacheSeedError("invalid_source_lineage")
        target_logical = (target.identity.issuer, target.identity.product_code, target.identity.document_type)
        if (
            target_logical != logical
            or target.first_observed_at < item.first_observed_at
            or item.superseded_at != target.first_observed_at
        ):
            raise V109CacheSeedError("invalid_source_lineage")
    if any(len(active) != 1 for active in active_by_product.values()):
        raise V109CacheSeedError("invalid_source_lineage")
    for item in result:
        visited: set[str] = set()
        current = item
        while current.superseded_by_source_id is not None:
            if current.identity.source_id in visited:
                raise V109CacheSeedError("invalid_source_lineage")
            visited.add(current.identity.source_id)
            current = by_id[current.superseded_by_source_id]
    return tuple(result)


def _revision_rows(
    rows: tuple[sqlite3.Row, ...],
    sources: tuple[V109SeedSource, ...],
    objects: tuple[V109SeedObject, ...],
) -> tuple[tuple[V109SeedSource, ...], tuple[V109SeedRevision, ...]]:
    source_by_id = {item.identity.source_id: item for item in sources}
    object_by_hash = {item.pdf_sha256: item for item in objects}
    grouped: defaultdict[str, list[V109SeedRevision]] = defaultdict(list)
    seen_revision_ids: set[int] = set()
    for row in rows:
        revision_id = int(row["revision_id"])
        if revision_id <= 0 or revision_id in seen_revision_ids:
            raise V109CacheSeedError("invalid_revision_history")
        seen_revision_ids.add(revision_id)
        source_id = _required_text(row["source_id"], maximum=71)
        pdf_sha256 = _required_text(row["pdf_sha256"], maximum=64)
        source = source_by_id.get(source_id)
        cache_object = object_by_hash.get(pdf_sha256)
        if source is None or cache_object is None:
            raise V109CacheSeedError("invalid_revision_history")
        size_bytes = int(row["pdf_size_bytes"])
        page_count = int(row["page_count"])
        if (size_bytes, page_count) != (cache_object.size_bytes, cache_object.page_count):
            raise V109CacheSeedError("source_pdf_metadata_mismatch")
        first = _parse_timestamp(row["first_observed_at"])
        last = _parse_timestamp(row["last_observed_at"])
        verified = _parse_timestamp(row["verified_at"])
        superseded = _parse_timestamp(row["superseded_at"], optional=True)
        if first is None or last is None or verified is None or last < first:
            raise V109CacheSeedError("invalid_revision_history")
        previous_value = row["previous_revision_id"]
        previous_id = None if previous_value is None else int(previous_value)
        grouped[source_id].append(
            V109SeedRevision(
                revision_id=revision_id,
                previous_revision_id=previous_id,
                source_id=source_id,
                pdf_sha256=pdf_sha256,
                pdf_size_bytes=size_bytes,
                page_count=page_count,
                final_url=_https_url(row["final_url"]),
                etag=_optional_header(row["etag"]),
                last_modified=_optional_header(row["last_modified"]),
                first_observed_at=first,
                last_observed_at=last,
                verified_at=verified,
                superseded_at=superseded,
                status="accepted",
                missing_reason=None,
            )
        )

    final_sources: list[V109SeedSource] = []
    final_revisions: list[V109SeedRevision] = []
    for source in sources:
        history = sorted(grouped.get(source.identity.source_id, []), key=lambda item: item.revision_id)
        reason: str | None = None
        if not history:
            reason = "source_has_no_revision"
        else:
            prior_revision: V109SeedRevision | None = None
            for index, revision in enumerate(history):
                expected_previous = None if prior_revision is None else prior_revision.revision_id
                if revision.previous_revision_id != expected_previous:
                    raise V109CacheSeedError("invalid_revision_history")
                if index < len(history) - 1 and revision.superseded_at is None:
                    raise V109CacheSeedError("invalid_revision_history")
                if index == len(history) - 1 and revision.superseded_at is not None:
                    raise V109CacheSeedError("invalid_revision_history")
                if (
                    prior_revision is not None
                    and revision.first_observed_at < prior_revision.first_observed_at
                ):
                    raise V109CacheSeedError("invalid_revision_history")
                if prior_revision is not None and prior_revision.superseded_at != revision.first_observed_at:
                    raise V109CacheSeedError("invalid_revision_history")
                prior_revision = revision
            if (
                source.first_observed_at != history[0].first_observed_at
                or source.last_observed_at != max(item.last_observed_at for item in history)
                or source.last_verified_at != max(item.verified_at for item in history)
            ):
                raise V109CacheSeedError("invalid_revision_history")
            missing = next(
                (
                    object_by_hash[item.pdf_sha256]
                    for item in history
                    if object_by_hash[item.pdf_sha256].status == "missing"
                ),
                None,
            )
            if missing is not None:
                reason = "source_history_incomplete"
        if reason is None:
            final_sources.append(source)
            final_revisions.extend(history)
        else:
            final_sources.append(replace(source, status="missing", missing_reason=reason))
            for revision in history:
                cache_object = object_by_hash[revision.pdf_sha256]
                revision_reason = (
                    "cas_object_missing" if cache_object.status == "missing" else "source_history_incomplete"
                )
                final_revisions.append(replace(revision, status="missing", missing_reason=revision_reason))

    # A logical product's source identities form one ordered lineage.  If any
    # member is incomplete, replaying only the remaining members would make a
    # historical identity look current.  Keep its objects pinned but exclude
    # the entire logical source lineage from binding.
    incomplete_logical = {
        (item.identity.issuer, item.identity.product_code, item.identity.document_type)
        for item in final_sources
        if item.status == "missing"
    }
    if incomplete_logical:
        source_ids_to_block = {
            item.identity.source_id
            for item in final_sources
            if (item.identity.issuer, item.identity.product_code, item.identity.document_type)
            in incomplete_logical
        }
        final_sources = [
            replace(item, status="missing", missing_reason="logical_source_lineage_incomplete")
            if item.identity.source_id in source_ids_to_block and item.status == "accepted"
            else item
            for item in final_sources
        ]
        final_revisions = [
            replace(item, status="missing", missing_reason="logical_source_lineage_incomplete")
            if item.source_id in source_ids_to_block and item.status == "accepted"
            else item
            for item in final_revisions
        ]
    return tuple(final_sources), tuple(final_revisions)


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(UTC).isoformat()


def _ledger_payload(
    *,
    root_identity: tuple[int, int],
    root_path_sha256: str,
    database_identity: FileIdentity,
    database_sha256: str,
    objects: tuple[V109SeedObject, ...],
    sources: tuple[V109SeedSource, ...],
    revisions: tuple[V109SeedRevision, ...],
) -> dict[str, Any]:
    accepted_hashes = sorted(item.pdf_sha256 for item in objects if item.status == "accepted")
    return {
        "accepted_pdf_hashes": accepted_hashes,
        "counts": {
            "accepted_objects": len(accepted_hashes),
            "accepted_revisions": sum(item.status == "accepted" for item in revisions),
            "accepted_sources": sum(item.status == "accepted" for item in sources),
            "missing_objects": sum(item.status == "missing" for item in objects),
            "missing_revisions": sum(item.status == "missing" for item in revisions),
            "missing_sources": sum(item.status == "missing" for item in sources),
            "objects": len(objects),
            "revisions": len(revisions),
            "sources": len(sources),
        },
        "objects": [
            {
                "missing_reason": item.missing_reason,
                "page_count": item.page_count,
                "pdf_sha256": item.pdf_sha256,
                "relative_path": item.relative_path,
                "size_bytes": item.size_bytes,
                "status": item.status,
            }
            for item in objects
        ],
        "pin_status": PIN_STATUS,
        "revisions": [
            {
                "final_url_sha256": _sha256(item.final_url),
                "first_observed_at": _iso(item.first_observed_at),
                "last_observed_at": _iso(item.last_observed_at),
                "missing_reason": item.missing_reason,
                "page_count": item.page_count,
                "pdf_sha256": item.pdf_sha256,
                "pdf_size_bytes": item.pdf_size_bytes,
                "previous_revision_id": item.previous_revision_id,
                "revision_id": item.revision_id,
                "source_id": item.source_id,
                "status": item.status,
                "superseded_at": _iso(item.superseded_at),
                "verified_at": _iso(item.verified_at),
            }
            for item in revisions
        ],
        "schema_version": LEDGER_SCHEMA_VERSION,
        "source_database": {
            "device": database_identity[0],
            "inode": database_identity[1],
            "sha256": database_sha256,
            "size_bytes": database_identity[2],
        },
        "source_root": {
            "device": root_identity[0],
            "inode": root_identity[1],
            "path_sha256": root_path_sha256,
        },
        "sources": [
            {
                "discovery_sha256": item.identity.discovery_sha256,
                "document_type": item.identity.document_type,
                "first_observed_at": _iso(item.first_observed_at),
                "issuer": item.identity.issuer,
                "last_observed_at": _iso(item.last_observed_at),
                "missing_reason": item.missing_reason,
                "product_code": item.identity.product_code,
                "source_id": item.identity.source_id,
                "source_post_id_sha256": _sha256(item.identity.source_post_id),
                "source_url_sha256": _sha256(item.identity.source_url),
                "source_version": item.identity.source_version,
                "status": item.status,
                "superseded_at": _iso(item.superseded_at),
                "superseded_by_source_id": item.superseded_by_source_id,
            }
            for item in sources
        ],
        "status": "applied",
    }


def build_v109_cache_seed_plan(source_root: Path) -> V109CacheSeedPlan:
    """Build a deterministic plan without writing to the v1.0.9 source."""

    root = _absolute_without_resolving(source_root)
    if not source_root.is_absolute() or root == Path(root.anchor) or len(os.fspath(root)) > 4096:
        raise V109CacheSeedError("unsafe_source_path")
    _require_secure_directory(root)
    root_stat = root.lstat()
    root_identity = (root_stat.st_dev, root_stat.st_ino)
    database_path = root / _DATABASE_NAME
    (
        database_identity,
        database_sha256,
        latest_run_id,
        latest_run_status,
        object_rows,
        source_rows,
        revision_rows,
    ) = _read_database(database_path)
    objects = _validate_object_rows(object_rows, root / "pdf-cache" / "objects" / "sha256")
    sources = _source_rows(source_rows)
    sources, revisions = _revision_rows(revision_rows, sources, objects)
    ledger = _ledger_payload(
        root_identity=root_identity,
        root_path_sha256=_sha256(os.fspath(root)),
        database_identity=database_identity,
        database_sha256=database_sha256,
        objects=objects,
        sources=sources,
        revisions=revisions,
    )
    ledger_bytes = canonical_json_bytes(ledger)
    if len(ledger_bytes) > MAX_LEDGER_BYTES:
        raise V109CacheSeedError("seed_ledger_too_large")
    return V109CacheSeedPlan(
        source_root=root,
        database_path=database_path,
        source_root_identity=root_identity,
        database_identity=database_identity,
        source_database_sha256=database_sha256,
        latest_run_id=latest_run_id,
        latest_run_status=latest_run_status,
        objects=objects,
        sources=sources,
        revisions=revisions,
        ledger_bytes=ledger_bytes,
        ledger_sha256=hashlib.sha256(ledger_bytes).hexdigest(),
    )


def paths_overlap(first: Path, second: Path) -> bool:
    """Compare absolute paths without following a potentially unsafe symlink."""

    left = _absolute_without_resolving(first)
    right = _absolute_without_resolving(second)
    return left == right or left in right.parents or right in left.parents


def _assert_plan_source_unchanged(plan: V109CacheSeedPlan) -> None:
    root_stat = plan.source_root.lstat()
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise V109CacheSeedError("source_changed_after_plan")
    if (root_stat.st_dev, root_stat.st_ino) != plan.source_root_identity:
        raise V109CacheSeedError("source_changed_after_plan")
    for suffix in _SIDECAR_SUFFIXES:
        try:
            Path(f"{plan.database_path}{suffix}").lstat()
        except FileNotFoundError:
            continue
        raise V109CacheSeedError("source_changed_after_plan")
    descriptor = _open_regular(plan.database_path, code="source_changed_after_plan")
    try:
        if _identity(os.fstat(descriptor)) != plan.database_identity:
            raise V109CacheSeedError("source_changed_after_plan")
        if _hash_descriptor(descriptor) != plan.source_database_sha256:
            raise V109CacheSeedError("source_changed_after_plan")
    finally:
        os.close(descriptor)
    actual_hashes = _scan_cas(plan.source_root / "pdf-cache" / "objects" / "sha256")
    if actual_hashes != plan.accepted_pdf_hashes:
        raise V109CacheSeedError("source_changed_after_plan")
    for item in plan.objects:
        if item.status == "missing":
            try:
                item.source_path.lstat()
            except FileNotFoundError:
                continue
            raise V109CacheSeedError("source_changed_after_plan")
        descriptor = _open_regular(item.source_path, code="source_changed_after_plan")
        try:
            if _identity(os.fstat(descriptor)) != item.file_identity:
                raise V109CacheSeedError("source_changed_after_plan")
        finally:
            os.close(descriptor)


def _open_directory(path: Path, *, code: str) -> int:
    try:
        listed = path.lstat()
    except FileNotFoundError:
        raise V109CacheSeedError(code) from None
    if stat.S_ISLNK(listed.st_mode) or not stat.S_ISDIR(listed.st_mode):
        raise V109CacheSeedError(code)
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise V109CacheSeedError(code) from exc
    opened = os.fstat(descriptor)
    if not stat.S_ISDIR(opened.st_mode) or (listed.st_dev, listed.st_ino) != (
        opened.st_dev,
        opened.st_ino,
    ):
        os.close(descriptor)
        raise V109CacheSeedError(code)
    return descriptor


def _ensure_destination_ledger_directory(state_dir: Path) -> Path:
    root = _absolute_without_resolving(state_dir)
    if root == Path(root.anchor) or len(os.fspath(root)) > 4096:
        raise V109CacheSeedError("unsafe_destination_ledger_path")
    _require_secure_directory(root)
    current = root
    for part in _LEDGER_DIRECTORY.parts:
        current /= part
        created = False
        try:
            os.mkdir(current, mode=0o700)
            created = True
        except FileExistsError:
            pass
        except OSError as exc:
            raise V109CacheSeedError("seed_ledger_write_failed") from exc
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            raise V109CacheSeedError("unsafe_destination_ledger_path") from None
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise V109CacheSeedError("unsafe_destination_ledger_path")
        if created:
            parent_fd = _open_directory(current.parent, code="unsafe_destination_ledger_path")
            try:
                os.fsync(parent_fd)
            except OSError as exc:
                raise V109CacheSeedError("seed_ledger_write_failed") from exc
            finally:
                os.close(parent_fd)
    return current


def _read_descriptor_bytes(descriptor: int, *, maximum: int) -> bytes:
    size = os.fstat(descriptor).st_size
    if size <= 0 or size > maximum:
        raise V109CacheSeedError("invalid_seed_ledger")
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        block = os.read(descriptor, min(1024 * 1024, remaining))
        if not block:
            raise V109CacheSeedError("invalid_seed_ledger")
        chunks.append(block)
        remaining -= len(block)
    if os.read(descriptor, 1):
        raise V109CacheSeedError("invalid_seed_ledger")
    return b"".join(chunks)


def _persist_ledger(state_dir: Path, plan: V109CacheSeedPlan) -> Path:
    ledger_directory = _ensure_destination_ledger_directory(state_dir)
    directory_fd = _open_directory(ledger_directory, code="unsafe_destination_ledger_path")
    final_name = f"{plan.ledger_sha256}.json"
    temporary_name = f".{plan.ledger_sha256}.{uuid.uuid4().hex}.tmp"
    temporary_fd = -1
    try:
        flags = (
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
        temporary_fd = os.open(temporary_name, flags, 0o600, dir_fd=directory_fd)
        view = memoryview(plan.ledger_bytes)
        while view:
            written = os.write(temporary_fd, view)
            if written < 1:  # pragma: no cover - regular file write invariant
                raise OSError("seed ledger write made no progress")
            view = view[written:]
        os.fsync(temporary_fd)
        os.close(temporary_fd)
        temporary_fd = -1
        try:
            os.link(
                temporary_name,
                final_name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except FileExistsError:
            try:
                listed = os.stat(final_name, dir_fd=directory_fd, follow_symlinks=False)
            except OSError as exc:
                raise V109CacheSeedError("seed_ledger_conflict") from exc
            if (
                stat.S_ISLNK(listed.st_mode)
                or not stat.S_ISREG(listed.st_mode)
                or listed.st_nlink != 1
                or listed.st_size != len(plan.ledger_bytes)
            ):
                raise V109CacheSeedError("seed_ledger_conflict") from None
            flags = (
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0)
            )
            try:
                existing_fd = os.open(final_name, flags, dir_fd=directory_fd)
            except OSError as exc:
                raise V109CacheSeedError("seed_ledger_conflict") from exc
            try:
                opened = os.fstat(existing_fd)
                if not stat.S_ISREG(opened.st_mode) or (listed.st_dev, listed.st_ino) != (
                    opened.st_dev,
                    opened.st_ino,
                ):
                    raise V109CacheSeedError("seed_ledger_conflict") from None
                existing = _read_descriptor_bytes(existing_fd, maximum=MAX_LEDGER_BYTES)
            finally:
                os.close(existing_fd)
            if existing != plan.ledger_bytes:
                raise V109CacheSeedError("seed_ledger_conflict") from None
        os.unlink(temporary_name, dir_fd=directory_fd)
        os.fsync(directory_fd)
    except OSError as exc:
        raise V109CacheSeedError("seed_ledger_write_failed") from exc
    finally:
        if temporary_fd >= 0:
            os.close(temporary_fd)
        with suppress(FileNotFoundError):
            os.unlink(temporary_name, dir_fd=directory_fd)
        os.close(directory_fd)
    return ledger_directory / final_name


def _source_replay_order(sources: dict[str, V109SeedSource]) -> tuple[V109SeedSource, ...]:
    indegree = {source_id: 0 for source_id in sources}
    successors: defaultdict[str, list[str]] = defaultdict(list)
    for source in sources.values():
        target = source.superseded_by_source_id
        if target is None:
            continue
        if target not in sources:
            raise V109CacheSeedError("incomplete_source_replay_lineage")
        successors[source.identity.source_id].append(target)
        indegree[target] += 1
    ready = sorted(
        (sources[source_id] for source_id, count in indegree.items() if count == 0),
        key=lambda item: (item.first_observed_at, item.identity.source_id),
    )
    ordered: list[V109SeedSource] = []
    while ready:
        current = ready.pop(0)
        ordered.append(current)
        for target in sorted(successors[current.identity.source_id]):
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(sources[target])
                ready.sort(key=lambda item: (item.first_observed_at, item.identity.source_id))
    if len(ordered) != len(sources):
        raise V109CacheSeedError("incomplete_source_replay_lineage")
    return tuple(ordered)


def _destination_timestamp_matches(value: str | None, expected: datetime | None) -> bool:
    try:
        return _parse_timestamp(value, optional=True) == expected
    except V109CacheSeedError:
        return False


def _verify_destination_history(
    cache: PDFCache,
    sources: dict[str, V109SeedSource],
    histories: dict[str, list[V109SeedRevision]],
) -> None:
    for source in sources.values():
        desired = sorted(histories[source.identity.source_id], key=lambda item: item.revision_id)
        persisted = cache.state.pdf_cache_source_history(source.identity.source_id)
        if len(persisted) != len(desired):
            raise V109CacheSeedError("destination_revision_conflict")
        for index, (row, expected) in enumerate(zip(persisted, desired, strict=True)):
            expected_previous_id = None if index == 0 else persisted[index - 1].revision_id
            if (
                row.previous_revision_id != expected_previous_id
                or row.issuer != source.identity.issuer
                or row.product_code != source.identity.product_code
                or row.document_type != source.identity.document_type
                or row.source_url != source.identity.source_url
                or row.source_version != source.identity.source_version
                or row.source_post_id != source.identity.source_post_id
                or row.discovery_sha256 != source.identity.discovery_sha256
                or row.pdf_sha256 != expected.pdf_sha256
                or row.pdf_size_bytes != expected.pdf_size_bytes
                or row.page_count != expected.page_count
                or row.relative_path != f"objects/sha256/{expected.pdf_sha256[:2]}/{expected.pdf_sha256}"
                or row.final_url != expected.final_url
                or row.etag != expected.etag
                or row.last_modified != expected.last_modified
                or not _destination_timestamp_matches(row.source_first_observed_at, source.first_observed_at)
                or not _destination_timestamp_matches(row.source_last_observed_at, source.last_observed_at)
                or not _destination_timestamp_matches(
                    row.revision_first_observed_at, expected.first_observed_at
                )
                or not _destination_timestamp_matches(
                    row.revision_last_observed_at, expected.last_observed_at
                )
                or not _destination_timestamp_matches(row.verified_at, expected.verified_at)
                or not _destination_timestamp_matches(row.superseded_at, expected.superseded_at)
                or row.superseded_by_source_id != source.superseded_by_source_id
                or not _destination_timestamp_matches(row.source_superseded_at, source.superseded_at)
            ):
                raise V109CacheSeedError("destination_revision_conflict")


def apply_v109_cache_seed(plan: V109CacheSeedPlan, cache: PDFCache) -> dict[str, Any]:
    """Import a verified plan into a distinct v1.0.11 :class:`PDFCache`."""

    destination = _absolute_without_resolving(cache.state_dir)
    if paths_overlap(plan.source_root, destination):
        raise V109CacheSeedError("source_destination_overlap")
    if (
        not _SHA256.fullmatch(plan.ledger_sha256)
        or len(plan.ledger_bytes) > MAX_LEDGER_BYTES
        or hashlib.sha256(plan.ledger_bytes).hexdigest() != plan.ledger_sha256
    ):
        raise V109CacheSeedError("invalid_seed_ledger_identity")
    _assert_plan_source_unchanged(plan)
    _ensure_destination_ledger_directory(destination)

    sources = {item.identity.source_id: item for item in plan.sources if item.status == "accepted"}
    histories: defaultdict[str, list[V109SeedRevision]] = defaultdict(list)
    for revision in plan.revisions:
        if revision.status == "accepted":
            histories[revision.source_id].append(revision)

    ordered_sources = _source_replay_order(sources)
    objects = {item.pdf_sha256: item for item in plan.objects}
    imported_objects = 0
    reused_objects = 0
    imported_revisions = 0
    reused_revisions = 0
    try:
        for item in plan.objects:
            if item.status != "accepted":
                continue
            existed = cache.state.pdf_cache_object(item.pdf_sha256) is not None
            cache.ingest(
                item.source_path,
                expected_sha256=item.pdf_sha256,
                expected_size_bytes=item.size_bytes,
                expected_page_count=item.page_count,
            )
            if existed:
                reused_objects += 1
            else:
                imported_objects += 1

        for source in ordered_sources:
            desired = sorted(histories[source.identity.source_id], key=lambda item: item.revision_id)
            existing = cache.state.pdf_cache_source_history(source.identity.source_id)
            if len(existing) > len(desired):
                raise V109CacheSeedError("destination_revision_conflict")
            for index, persisted in enumerate(existing):
                expected = desired[index]
                if (
                    persisted.issuer != source.identity.issuer
                    or persisted.product_code != source.identity.product_code
                    or persisted.document_type != source.identity.document_type
                    or persisted.source_url != source.identity.source_url
                    or persisted.source_version != source.identity.source_version
                    or persisted.source_post_id != source.identity.source_post_id
                    or persisted.discovery_sha256 != source.identity.discovery_sha256
                    or persisted.pdf_sha256 != expected.pdf_sha256
                    or persisted.pdf_size_bytes != expected.pdf_size_bytes
                    or persisted.page_count != expected.page_count
                    or persisted.final_url != expected.final_url
                    or persisted.etag != expected.etag
                    or persisted.last_modified != expected.last_modified
                ):
                    raise V109CacheSeedError("destination_revision_conflict")
            reused_revisions += len(existing)
            for revision in desired[len(existing) :]:
                item = objects[revision.pdf_sha256]
                cache.ingest_and_bind(
                    source.identity,
                    item.source_path,
                    final_url=revision.final_url,
                    expected_sha256=revision.pdf_sha256,
                    expected_size_bytes=revision.pdf_size_bytes,
                    expected_page_count=revision.page_count,
                    etag=revision.etag,
                    last_modified=revision.last_modified,
                    replace_validators=True,
                    observed_at=revision.first_observed_at,
                    verified_at=revision.verified_at,
                )
                if revision.last_observed_at != revision.first_observed_at:
                    cache.ingest_and_bind(
                        source.identity,
                        item.source_path,
                        final_url=revision.final_url,
                        expected_sha256=revision.pdf_sha256,
                        expected_size_bytes=revision.pdf_size_bytes,
                        expected_page_count=revision.page_count,
                        etag=revision.etag,
                        last_modified=revision.last_modified,
                        replace_validators=True,
                        observed_at=revision.last_observed_at,
                        verified_at=revision.verified_at,
                    )
                imported_revisions += 1
        _verify_destination_history(cache, sources, histories)
    except V109CacheSeedError:
        raise
    except Exception as exc:
        raise V109CacheSeedError("cache_seed_apply_failed") from exc

    _assert_plan_source_unchanged(plan)
    ledger_path = _persist_ledger(destination, plan)
    return plan.report(
        applied=True,
        imported_pdf_objects=imported_objects,
        reused_pdf_objects=reused_objects,
        imported_revisions=imported_revisions,
        reused_revisions=reused_revisions,
        ledger_path=str(ledger_path.relative_to(destination)),
    )


def _validated_ledger_pins(data: bytes, *, expected_sha256: str) -> frozenset[str]:
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        raise V109CacheSeedError("invalid_seed_ledger")
    try:
        payload = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise V109CacheSeedError("invalid_seed_ledger") from exc
    if not isinstance(payload, dict) or canonical_json_bytes(payload) != data:
        raise V109CacheSeedError("invalid_seed_ledger")
    if (
        payload.get("schema_version") != LEDGER_SCHEMA_VERSION
        or payload.get("status") != "applied"
        or payload.get("pin_status") != PIN_STATUS
    ):
        raise V109CacheSeedError("invalid_seed_ledger")
    hashes = payload.get("accepted_pdf_hashes")
    objects = payload.get("objects")
    revisions = payload.get("revisions")
    counts = payload.get("counts")
    if not isinstance(hashes, list) or not isinstance(objects, list) or not isinstance(revisions, list):
        raise V109CacheSeedError("invalid_seed_ledger")
    if not isinstance(counts, dict):
        raise V109CacheSeedError("invalid_seed_ledger")
    if hashes != sorted(set(hashes)) or any(
        not isinstance(item, str) or not _SHA256.fullmatch(item) for item in hashes
    ):
        raise V109CacheSeedError("invalid_seed_ledger")
    accepted_objects = {
        item.get("pdf_sha256")
        for item in objects
        if isinstance(item, dict) and item.get("status") == "accepted"
    }
    if accepted_objects != set(hashes) or counts.get("accepted_objects") != len(hashes):
        raise V109CacheSeedError("invalid_seed_ledger")
    for revision in revisions:
        if not isinstance(revision, dict):
            raise V109CacheSeedError("invalid_seed_ledger")
        if revision.get("status") == "accepted" and revision.get("pdf_sha256") not in accepted_objects:
            raise V109CacheSeedError("invalid_seed_ledger")
    return frozenset(hashes)


def load_v109_seed_pins(state_dir: Path) -> frozenset[str]:
    """Load active seed pins, rejecting every symlink or altered ledger."""

    root = _absolute_without_resolving(state_dir)
    _require_secure_directory(root)
    ledger_directory = root / _LEDGER_DIRECTORY
    if not _require_secure_directory_tree_or_missing(ledger_directory, code="invalid_seed_ledger"):
        return frozenset()
    pins: set[str] = set()
    try:
        entries = sorted(ledger_directory.iterdir(), key=lambda path: path.name)
    except OSError as exc:
        raise V109CacheSeedError("invalid_seed_ledger") from exc
    for entry in entries:
        if not entry.name.endswith(".json") or not _SHA256.fullmatch(entry.name[:-5]):
            raise V109CacheSeedError("invalid_seed_ledger")
        descriptor = _open_regular(entry, code="invalid_seed_ledger")
        try:
            data = _read_descriptor_bytes(descriptor, maximum=MAX_LEDGER_BYTES)
        finally:
            os.close(descriptor)
        pins.update(_validated_ledger_pins(data, expected_sha256=entry.name[:-5]))
    return frozenset(pins)
