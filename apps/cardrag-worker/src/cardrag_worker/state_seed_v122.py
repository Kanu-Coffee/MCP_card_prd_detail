"""Fail-closed v1.0.28 state seed and lineage/OCR cache recovery for v1.0.29.

The source is an immutable, read-only v1.0.28 worker state directory. This
module never opens the source database for writing and never creates files
below the source root. A successful apply imports PDF CAS, source and revision
lineage, terminal run identity, and discovery snapshots, and materializes
verified immutable OCR seed artifacts under destination ``ocr-seed/`` while
committing one content-addressed audit ledger below destination
``audit-reports/state-seed/``.
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
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from cardrag_core.canonical import canonical_json_bytes
from cardrag_core.manifests import (
    OCRArtifactManifest,
)

from .contracts import SourceRecord
from .downloader import PDFValidationError, validate_pdf
from .pdf_cache import PDFCache, PDFSourceIdentity
from .state import WorkerState
from .webdav import WebDAVClient

LEDGER_SCHEMA_VERSION = "cardrag.state-seed-ledger.v2"
REPORT_SCHEMA_VERSION = "cardrag.state-seed-report.v1"
PIN_STATUS = "active_v122_state_seed"

MAX_DATABASE_BYTES = 16 * 1024 * 1024 * 1024  # 16 GiB for 6.7GB DB
MAX_PDF_BYTES = 100 * 1024 * 1024
MAX_OBJECTS = 100_000
MAX_SOURCES = 100_000
MAX_REVISIONS = 500_000
MAX_LEDGER_BYTES = 256 * 1024 * 1024

_LEDGER_DIRECTORY = Path("audit-reports/state-seed")
_OCR_SEED_DIRECTORY = Path("ocr-seed")
_DATABASE_NAME = "worker-state.sqlite3"
_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_ID = re.compile(r"^source_[0-9a-f]{64}$")
_PREFIX = re.compile(r"^[0-9a-f]{2}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_TERMINAL_RUN_STATUSES = frozenset({"succeeded", "failed", "no_change", "interrupted"})

type FileIdentity = tuple[int, int, int, int, int]
type EntryStatus = Literal["accepted", "missing"]


class StateSeedError(RuntimeError):
    """A bounded error code safe to expose in release audit output."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class StateSeedCASObject:
    pdf_sha256: str
    size_bytes: int
    page_count: int
    relative_path: str
    source_path: Path
    file_identity: FileIdentity


@dataclass(frozen=True, slots=True)
class StateSeedSource:
    identity: PDFSourceIdentity
    first_observed_at: datetime
    last_observed_at: datetime
    last_verified_at: datetime
    superseded_by_source_id: str | None
    superseded_at: datetime | None


@dataclass(frozen=True, slots=True)
class StateSeedRevision:
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


@dataclass(frozen=True, slots=True)
class StateSeedSnapshot:
    snapshot_id: str
    run_id: str
    issuer: str
    observed_at: str
    source_sha256: str
    record_count: int
    payload_json: str


@dataclass(frozen=True, slots=True)
class StateSeedTerminalRun:
    run_id: str
    started_at: str
    finished_at: str | None
    status: str
    corpus_sha256: str | None
    contract_sha256: str | None
    error: str | None


@dataclass(frozen=True, slots=True)
class StateSeedOCREntry:
    document_id: str
    issuer: str
    source_id: str
    pdf_sha256: str
    pdf_size_bytes: int
    page_count: int
    ocr_sha256: str
    ocr_size_bytes: int
    kind: Literal["native", "adopted"]
    reuse_key: str
    source_ocr_path: Path
    source_manifest_path: Path | None
    manifest_bytes: bytes | None
    ready_bytes: bytes | None
    manifest_sha256: str | None
    ready_sha256: str | None
    model: str = "PaddleOCR-VL-1.6"


@dataclass(frozen=True, slots=True)
class StateSeedV122Plan:
    source_root: Path
    database_path: Path
    source_root_identity: tuple[int, int]
    database_identity: FileIdentity
    source_database_sha256: str
    generation_id: str
    run_id: str
    terminal_run: StateSeedTerminalRun
    snapshots: tuple[StateSeedSnapshot, ...]
    cas_objects: tuple[StateSeedCASObject, ...]
    sources: tuple[StateSeedSource, ...]
    revisions: tuple[StateSeedRevision, ...]
    ocr_entries: tuple[StateSeedOCREntry, ...]
    prior_current_doc_ids: tuple[str, ...]
    prior_historical_doc_ids: tuple[str, ...]
    source_records: tuple[SourceRecord, ...]
    ledger_bytes: bytes
    ledger_sha256: str

    def report(
        self,
        *,
        applied: bool,
        imported_pdf_objects: int = 0,
        reused_pdf_objects: int = 0,
        imported_revisions: int = 0,
        reused_revisions: int = 0,
        imported_ocr_files: int = 0,
        reused_ocr_files: int = 0,
        ledger_path: str | None = None,
    ) -> dict[str, Any]:
        return {
            "accepted_ocr_documents": len(self.ocr_entries),
            "accepted_pdf_objects": len(self.cas_objects),
            "accepted_revisions": len(self.revisions),
            "accepted_sources": len(self.sources),
            "applied": applied,
            "dry_run": not applied,
            "generation_id": self.generation_id,
            "imported_ocr_files": imported_ocr_files,
            "imported_pdf_objects": imported_pdf_objects,
            "imported_revisions": imported_revisions,
            "ledger_path": ledger_path,
            "ledger_sha256": self.ledger_sha256,
            "ledger_size_bytes": len(self.ledger_bytes),
            "native_ocr_count": sum(1 for e in self.ocr_entries if e.kind == "native"),
            "adopted_ocr_count": sum(1 for e in self.ocr_entries if e.kind == "adopted"),
            "prior_current_count": len(self.prior_current_doc_ids),
            "prior_historical_count": len(self.prior_historical_doc_ids),
            "reused_ocr_files": reused_ocr_files,
            "reused_pdf_objects": reused_pdf_objects,
            "reused_revisions": reused_revisions,
            "run_id": self.run_id,
            "schema_version": REPORT_SCHEMA_VERSION,
            "source_database_sha256": self.source_database_sha256,
            "status": "applied" if applied else "verified",
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
            raise StateSeedError("unsafe_source_path") from None
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise StateSeedError("unsafe_source_path")
    return True


def _require_secure_directory_tree_or_missing(path: Path, *, code: str) -> bool:
    for component in _components(path):
        try:
            mode = component.lstat().st_mode
        except FileNotFoundError:
            return False
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise StateSeedError(code)
    return True


def _identity(value: os.stat_result) -> FileIdentity:
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _open_regular(path: Path, *, code: str) -> int:
    _require_secure_directory(path.parent)
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        raise StateSeedError(code) from None
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise StateSeedError(code)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise StateSeedError(code) from exc
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise StateSeedError(code)
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
        raise StateSeedError("invalid_source_metadata")
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise StateSeedError("invalid_source_metadata") from exc
    if parsed.tzinfo is None:
        raise StateSeedError("invalid_source_metadata")
    return parsed.astimezone(UTC)


def _required_text(value: object, *, maximum: int = 4096, empty_ok: bool = False) -> str:
    if not isinstance(value, str):
        raise StateSeedError("invalid_source_metadata")
    if value != value.strip() or len(value) > maximum or _CONTROL.search(value):
        raise StateSeedError("invalid_source_metadata")
    if not value and not empty_ok:
        raise StateSeedError("invalid_source_metadata")
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
        raise StateSeedError("invalid_source_metadata")
    return url


def _read_descriptor_bytes(descriptor: int, *, maximum: int) -> bytes:
    size = os.fstat(descriptor).st_size
    if size <= 0 or size > maximum:
        raise StateSeedError("invalid_seed_ledger")
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        block = os.read(descriptor, min(1024 * 1024, remaining))
        if not block:
            raise StateSeedError("invalid_seed_ledger")
        chunks.append(block)
        remaining -= len(block)
    if os.read(descriptor, 1):
        raise StateSeedError("invalid_seed_ledger")
    return b"".join(chunks)


def _open_directory(path: Path, *, code: str) -> int:
    try:
        listed = path.lstat()
    except FileNotFoundError:
        raise StateSeedError(code) from None
    if stat.S_ISLNK(listed.st_mode) or not stat.S_ISDIR(listed.st_mode):
        raise StateSeedError(code)
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise StateSeedError(code) from exc
    opened = os.fstat(descriptor)
    if not stat.S_ISDIR(opened.st_mode) or (listed.st_dev, listed.st_ino) != (
        opened.st_dev,
        opened.st_ino,
    ):
        os.close(descriptor)
        raise StateSeedError(code)
    return descriptor


def paths_overlap(first: Path, second: Path) -> bool:
    """Compare absolute paths without following a potentially unsafe symlink."""
    left = _absolute_without_resolving(first)
    right = _absolute_without_resolving(second)
    return left == right or left in right.parents or right in left.parents


def _source_replay_order(sources: dict[str, StateSeedSource]) -> tuple[StateSeedSource, ...]:
    indegree = {source_id: 0 for source_id in sources}
    successors: defaultdict[str, list[str]] = defaultdict(list)
    for source in sources.values():
        target = source.superseded_by_source_id
        if target is None:
            continue
        if target not in sources:
            raise StateSeedError("incomplete_source_replay_lineage")
        successors[source.identity.source_id].append(target)
        indegree[target] += 1
    ready = sorted(
        (sources[source_id] for source_id, count in indegree.items() if count == 0),
        key=lambda item: (item.first_observed_at, item.identity.source_id),
    )
    ordered: list[StateSeedSource] = []
    while ready:
        current = ready.pop(0)
        ordered.append(current)
        for target in sorted(successors[current.identity.source_id]):
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(sources[target])
                ready.sort(key=lambda item: (item.first_observed_at, item.identity.source_id))
    if len(ordered) != len(sources):
        raise StateSeedError("incomplete_source_replay_lineage")
    return tuple(ordered)


def build_state_seed_v122_plan(
    source_root: Path,
    generation_id: str,
    webdav: WebDAVClient | None = None,
    *,
    expected_documents: int | None = None,
) -> StateSeedV122Plan:
    """Build a deterministic, validated state and OCR seed recovery plan."""
    root = _absolute_without_resolving(source_root)
    if not source_root.is_absolute() or root == Path(root.anchor) or len(os.fspath(root)) > 4096:
        raise StateSeedError("unsafe_source_path")
    _require_secure_directory(root)
    root_stat = root.lstat()
    root_identity = (root_stat.st_dev, root_stat.st_ino)
    database_path = root / _DATABASE_NAME

    for suffix in _SIDECAR_SUFFIXES:
        sidecar = Path(f"{database_path}{suffix}")
        try:
            sidecar.lstat()
        except FileNotFoundError:
            continue
        raise StateSeedError("source_has_sidecars")

    descriptor = _open_regular(database_path, code="source_database_missing_or_unsafe")
    before_stat = _identity(os.fstat(descriptor))
    if before_stat[2] <= 0 or before_stat[2] > MAX_DATABASE_BYTES:
        os.close(descriptor)
        raise StateSeedError("source_database_size_invalid")
    database_sha256 = _hash_descriptor(descriptor)

    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            f"file:/proc/self/fd/{descriptor}?mode=ro&immutable=1",
            uri=True,
            timeout=10,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")

        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or str(integrity[0]) != "ok":
            raise StateSeedError("source_database_integrity_failed")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise StateSeedError("source_database_foreign_key_failed")

        # Verify writer is terminated: no run is running
        active_count = connection.execute("SELECT count(*) FROM run WHERE status='running'").fetchone()[0]
        if active_count > 0:
            raise StateSeedError("source_run_active")

        # Find publish row for generation_id
        publish_row = connection.execute(
            "SELECT * FROM publish WHERE generation_id=?", (generation_id,)
        ).fetchone()
        if publish_row is None or str(publish_row["status"]) != "ready":
            raise StateSeedError("generation_not_found_or_not_ready")

        run_id = str(publish_row["run_id"])
        corpus_sha256 = str(publish_row["corpus_sha256"])
        contract_sha256 = str(publish_row["contract_sha256"])

        # Verify run row
        run_row = connection.execute("SELECT * FROM run WHERE run_id=?", (run_id,)).fetchone()
        if run_row is None or str(run_row["status"]) != "succeeded":
            raise StateSeedError("source_run_not_succeeded")

        terminal_run = StateSeedTerminalRun(
            run_id=run_id,
            started_at=str(run_row["started_at"]),
            finished_at=str(run_row["finished_at"]) if run_row["finished_at"] is not None else None,
            status=str(run_row["status"]),
            corpus_sha256=str(run_row["corpus_sha256"]) if run_row["corpus_sha256"] is not None else None,
            contract_sha256=str(run_row["contract_sha256"]) if run_row["contract_sha256"] is not None else None,
            error=str(run_row["error"]) if run_row["error"] is not None else None,
        )

        # Snapshots
        snapshot_rows = connection.execute(
            "SELECT * FROM snapshot WHERE run_id=? ORDER BY observed_at, snapshot_id",
            (run_id,),
        ).fetchall()
        snapshots = tuple(
            StateSeedSnapshot(
                snapshot_id=str(r["snapshot_id"]),
                run_id=str(r["run_id"]),
                issuer=str(r["issuer"]),
                observed_at=str(r["observed_at"]),
                source_sha256=str(r["source_sha256"]),
                record_count=int(r["record_count"]),
                payload_json=str(r["payload_json"]),
            )
            for r in snapshot_rows
        )

        all_snapshot_rows = connection.execute(
            "SELECT snapshot_id, run_id, issuer, observed_at, payload_json FROM snapshot ORDER BY observed_at ASC, snapshot_id ASC"
        ).fetchall()

        # PDF Sources and Revisions
        source_db_rows = connection.execute(
            "SELECT * FROM pdf_cache_source ORDER BY source_id"
        ).fetchall()
        revision_db_rows = connection.execute(
            "SELECT * FROM pdf_cache_source_revision ORDER BY source_id, revision_id"
        ).fetchall()
        object_db_rows = connection.execute(
            "SELECT * FROM pdf_cache_object ORDER BY pdf_sha256"
        ).fetchall()

        if (
            len(source_db_rows) > MAX_SOURCES
            or len(revision_db_rows) > MAX_REVISIONS
            or len(object_db_rows) > MAX_OBJECTS
        ):
            raise StateSeedError("source_database_limit_exceeded")

    except sqlite3.Error as exc:
        raise StateSeedError("source_database_read_failed") from exc
    finally:
        if connection is not None:
            connection.close()
        after_stat = _identity(os.fstat(descriptor))
        os.close(descriptor)

    if after_stat != before_stat:
        raise StateSeedError("source_database_changed")

    # Load and validate publish seal
    seal_path = root / "runs" / run_id / "sealed" / "publish.json"
    seal_desc = _open_regular(seal_path, code="source_seal_missing_or_unsafe")
    try:
        seal_bytes = _read_descriptor_bytes(seal_desc, maximum=MAX_LEDGER_BYTES)
    finally:
        os.close(seal_desc)

    try:
        seal_json = json.loads(seal_bytes)
    except Exception as exc:
        raise StateSeedError("source_seal_invalid_json") from exc

    if (
        seal_json.get("schema_version") != "cardrag.worker-seal.v1"
        or seal_json.get("generation_id") != generation_id
        or seal_json.get("run_id") != run_id
        or seal_json.get("corpus_sha256") != corpus_sha256
        or seal_json.get("contract_sha256") != contract_sha256
    ):
        raise StateSeedError("source_seal_identity_mismatch")

    manifest = seal_json.get("manifest")
    if not isinstance(manifest, dict) or manifest.get("schema_version") not in {
        "cardrag.worker-manifest.v1",
        "cardrag.generation.v6",
    }:
        raise StateSeedError("source_seal_manifest_invalid")

    manifest_docs = manifest.get("documents")
    if not isinstance(manifest_docs, list) or len(manifest_docs) < 1:
        raise StateSeedError("source_seal_manifest_invalid")
    if expected_documents is not None and len(manifest_docs) != expected_documents:
        raise StateSeedError(
            f"expected {expected_documents} manifest documents, got {len(manifest_docs)}"
        )

    # Search complete snapshot history for canonical SourceRecords
    all_snapshot_sources: dict[str, SourceRecord] = {}
    for r in all_snapshot_rows:
        obs_at = _parse_timestamp(r["observed_at"])
        assert obs_at is not None
        try:
            snap_payload = json.loads(str(r["payload_json"]))
        except Exception as exc:
            raise StateSeedError("source_snapshot_invalid_json") from exc
        for raw in snap_payload.get("records", []):
            if not isinstance(raw, dict):
                raise StateSeedError("invalid_source_metadata")
            try:
                s_eff_date = date.fromisoformat(str(raw["effective_date"]))
            except Exception as exc:
                raise StateSeedError("invalid_source_metadata") from exc
            rec = SourceRecord(
                issuer=str(raw["issuer"]),
                product_code=str(raw["product_code"]),
                product_name=str(raw["product_name"]),
                effective_date=s_eff_date,
                source_version=str(raw["source_version"]),
                source_url=str(raw["source_url"]),
                source_post_id=str(raw.get("source_post_id") or ""),
                file_name=str(raw["file_name"]),
                category=str(raw.get("category", "credit")),
                discovered_at=obs_at,
                metadata=raw.get("metadata", {}),
                document_type=str(raw.get("document_type", "product_description")),
            )
            existing = all_snapshot_sources.get(rec.source_id)
            if existing is not None and existing.discovery_payload != rec.discovery_payload:
                raise StateSeedError("conflicting_snapshot_source_metadata")
            all_snapshot_sources[rec.source_id] = rec

    # Acquisition checkpoint for prior current/historical distinction and exact source binding
    acquisition_path = root / "runs" / run_id / "checkpoints" / "acquisition.v1.json"
    prior_current_set: set[str] = set()
    prior_historical_set: set[str] = set()
    doc_to_source_id: dict[str, str] = {}

    if acquisition_path.is_file():
        acq_desc = _open_regular(acquisition_path, code="source_acquisition_missing_or_unsafe")
        try:
            acq_bytes = _read_descriptor_bytes(acq_desc, maximum=MAX_LEDGER_BYTES)
        finally:
            os.close(acq_desc)
        try:
            acq_json = json.loads(acq_bytes)
        except Exception as exc:
            raise StateSeedError("source_acquisition_invalid_json") from exc

        if acq_json.get("schema_version") != "cardrag.pdf-acquisition.v1":
            raise StateSeedError("source_acquisition_schema_mismatch")
        if acq_json.get("run_id") != run_id:
            raise StateSeedError("source_acquisition_run_id_mismatch")
        if "contract_sha256" in acq_json and acq_json["contract_sha256"] != contract_sha256:
            raise StateSeedError("source_acquisition_contract_mismatch")

        acq_docs = acq_json.get("documents")
        if not isinstance(acq_docs, list) or len(acq_docs) != len(manifest_docs):
            raise StateSeedError("source_acquisition_count_mismatch")

        acq_by_doc_id: dict[str, dict[str, Any]] = {}
        for adoc in acq_docs:
            adid = adoc.get("document_id")
            if not adid or not isinstance(adid, str) or adid in acq_by_doc_id:
                raise StateSeedError("source_acquisition_duplicate_doc_id")
            acq_by_doc_id[adid] = adoc
            if adoc.get("is_historical"):
                prior_historical_set.add(adid)
            else:
                prior_current_set.add(adid)

        for mdoc in manifest_docs:
            doc_id = _required_text(mdoc["document_id"])
            if doc_id not in acq_by_doc_id:
                raise StateSeedError(f"acquisition_document_missing: {doc_id}")
            adoc = acq_by_doc_id[doc_id]
            pdf_ref = mdoc.get("pdf", {})
            if (
                adoc.get("pdf_sha256") != pdf_ref.get("sha256")
                or int(adoc.get("pdf_size_bytes", -1)) != int(pdf_ref.get("size_bytes", -2))
                or int(adoc.get("page_count", -1)) != int(mdoc.get("page_count", -2))
                or (
                    "is_historical" in mdoc
                    and bool(adoc.get("is_historical", False)) != bool(mdoc.get("is_historical", False))
                )
            ):
                raise StateSeedError(f"acquisition_manifest_mismatch: {doc_id}")
            src_id = adoc.get("source_id")
            if not src_id or not isinstance(src_id, str) or not _SOURCE_ID.fullmatch(src_id):
                raise StateSeedError(f"acquisition_source_id_invalid: {doc_id}")
            doc_to_source_id[doc_id] = src_id
    else:
        for mdoc in manifest_docs:
            doc_id = _required_text(mdoc["document_id"])
            pdf_sha = _required_text(mdoc["pdf"]["sha256"])
            if mdoc.get("is_historical"):
                prior_historical_set.add(doc_id)
            else:
                prior_current_set.add(doc_id)
            matching_sources = [
                s for s in all_snapshot_sources.values()
                if s.document_id(pdf_sha) == doc_id
            ]
            if len(matching_sources) == 0:
                raise StateSeedError(f"source_metadata_missing: {doc_id}")
            if len(matching_sources) > 1:
                raise StateSeedError(f"ambiguous_source_binding: {doc_id}")
            doc_to_source_id[doc_id] = matching_sources[0].source_id

    # Validate CAS objects
    cas_objects_dict: dict[str, StateSeedCASObject] = {}
    for obj_row in object_db_rows:
        pdf_sha = str(obj_row["pdf_sha256"])
        size_bytes = int(obj_row["size_bytes"])
        page_count = int(obj_row["page_count"])
        rel_path = f"objects/sha256/{pdf_sha[:2]}/{pdf_sha}"
        source_path = root / "pdf-cache" / rel_path
        if not source_path.is_file():
            continue
        desc = _open_regular(source_path, code="source_pdf_missing_or_unsafe")
        obj_stat = _identity(os.fstat(desc))
        os.close(desc)

        try:
            val_sha, val_size, val_pages = validate_pdf(source_path, expected_sha256=pdf_sha)
        except (OSError, PDFValidationError) as exc:
            raise StateSeedError("source_pdf_validation_failed") from exc

        if (val_sha, val_size, val_pages) != (pdf_sha, size_bytes, page_count):
            raise StateSeedError("source_pdf_metadata_mismatch")

        cas_objects_dict[pdf_sha] = StateSeedCASObject(
            pdf_sha256=pdf_sha,
            size_bytes=size_bytes,
            page_count=page_count,
            relative_path=rel_path,
            source_path=source_path,
            file_identity=obj_stat,
        )

    # Fail closed if any document in manifest_docs does not have its CAS PDF
    for doc in manifest_docs:
        doc_pdf_sha = doc.get("pdf", {}).get("sha256")
        if doc_pdf_sha and doc_pdf_sha not in cas_objects_dict:
            raise StateSeedError(f"seed_document_pdf_missing for {doc.get('document_id')}")

    # Validate Sources & Revisions
    sources_dict: dict[str, StateSeedSource] = {}
    for s_row in source_db_rows:
        src_id = str(s_row["source_id"])
        disc_sha = str(s_row["discovery_sha256"])
        sources_dict[src_id] = StateSeedSource(
            identity=PDFSourceIdentity(
                source_id=src_id,
                issuer=str(s_row["issuer"]),
                product_code=str(s_row["product_code"]),
                document_type=str(s_row["document_type"]),
                source_url=_https_url(s_row["source_url"]),
                source_version=str(s_row["source_version"]),
                source_post_id=str(s_row["source_post_id"] or ""),
                discovery_sha256=disc_sha,
            ),
            first_observed_at=_parse_timestamp(s_row["first_observed_at"]),  # type: ignore[arg-type]
            last_observed_at=_parse_timestamp(s_row["last_observed_at"]),  # type: ignore[arg-type]
            last_verified_at=_parse_timestamp(s_row["last_verified_at"]),  # type: ignore[arg-type]
            superseded_by_source_id=str(s_row["superseded_by_source_id"]) if s_row["superseded_by_source_id"] is not None else None,
            superseded_at=_parse_timestamp(s_row["superseded_at"], optional=True),
        )

    revisions_list: list[StateSeedRevision] = []
    for r_row in revision_db_rows:
        revisions_list.append(
            StateSeedRevision(
                revision_id=int(r_row["revision_id"]),
                previous_revision_id=int(r_row["previous_revision_id"]) if r_row["previous_revision_id"] is not None else None,
                source_id=str(r_row["source_id"]),
                pdf_sha256=str(r_row["pdf_sha256"]),
                pdf_size_bytes=int(r_row["pdf_size_bytes"]),
                page_count=int(r_row["page_count"]),
                final_url=_https_url(r_row["final_url"]),
                etag=_optional_header(r_row["etag"]),
                last_modified=_optional_header(r_row["last_modified"]),
                first_observed_at=_parse_timestamp(r_row["first_observed_at"]),  # type: ignore[arg-type]
                last_observed_at=_parse_timestamp(r_row["last_observed_at"]),  # type: ignore[arg-type]
                verified_at=_parse_timestamp(r_row["verified_at"]),  # type: ignore[arg-type]
                superseded_at=_parse_timestamp(r_row["superseded_at"], optional=True),
            )
        )

    # Validate 5,192 OCR documents
    runs_dir = root / "runs" / run_id / "documents"
    ocr_entries: list[StateSeedOCREntry] = []

    for doc in manifest_docs:
        doc_id = _required_text(doc["document_id"])
        issuer = _required_text(doc["issuer"])
        pdf_ref = doc["pdf"]
        ocr_ref = doc["ocr"]
        pdf_sha = _required_text(pdf_ref["sha256"])
        pdf_size = int(pdf_ref["size_bytes"])
        page_count = int(doc["page_count"])
        ocr_sha = _required_text(ocr_ref["sha256"])
        ocr_size = int(ocr_ref["size_bytes"])
        cache_kind = doc.get("ocr_cache_kind")
        reuse_key = doc.get("ocr_reuse_key")

        doc_ocr_dir = runs_dir / doc_id / "ocr"
        ocr_path = doc_ocr_dir / "ocr.md"
        ocr_desc = _open_regular(ocr_path, code="source_ocr_missing_or_unsafe")
        try:
            ocr_bytes = _read_descriptor_bytes(ocr_desc, maximum=MAX_LEDGER_BYTES)
        finally:
            os.close(ocr_desc)

        if len(ocr_bytes) != ocr_size or hashlib.sha256(ocr_bytes).hexdigest() != ocr_sha:
            raise StateSeedError("source_ocr_corrupt")

        if cache_kind == "adopted":
            if not reuse_key:
                raise StateSeedError(f"adopted document {doc_id} missing reuse_key")
            # Adopted: local ocr.md is verified against seal ocr sha
            ocr_entries.append(
                StateSeedOCREntry(
                    document_id=doc_id,
                    issuer=issuer,
                    source_id="",  # derived during binding
                    pdf_sha256=pdf_sha,
                    pdf_size_bytes=pdf_size,
                    page_count=page_count,
                    ocr_sha256=ocr_sha,
                    ocr_size_bytes=ocr_size,
                    kind="adopted",
                    reuse_key=str(reuse_key),
                    source_ocr_path=ocr_path,
                    source_manifest_path=None,
                    manifest_bytes=None,
                    ready_bytes=None,
                    manifest_sha256=None,
                    ready_sha256=None,
                )
            )
        else:
            # Native: must have native-manifest.json
            manifest_path = doc_ocr_dir / "native-manifest.json"
            man_desc = _open_regular(manifest_path, code="source_native_manifest_missing")
            try:
                man_bytes = _read_descriptor_bytes(man_desc, maximum=MAX_LEDGER_BYTES)
            finally:
                os.close(man_desc)

            try:
                manifest_model = OCRArtifactManifest.model_validate_json(man_bytes)
            except Exception as exc:
                raise StateSeedError(f"native_manifest_invalid for {doc_id}") from exc

            if manifest_model.canonical_bytes() != man_bytes:
                raise StateSeedError(f"native_manifest_not_canonical for {doc_id}")
            if manifest_model.output.sha256 != ocr_sha or manifest_model.output.size_bytes != ocr_size:
                raise StateSeedError(f"native_manifest_output_mismatch for {doc_id}")
            if manifest_model.source.pdf_sha256 != pdf_sha:
                raise StateSeedError(f"native_manifest_pdf_mismatch for {doc_id}")

            ocr_entries.append(
                StateSeedOCREntry(
                    document_id=doc_id,
                    issuer=issuer,
                    source_id="",
                    pdf_sha256=pdf_sha,
                    pdf_size_bytes=pdf_size,
                    page_count=page_count,
                    ocr_sha256=ocr_sha,
                    ocr_size_bytes=ocr_size,
                    kind="native",
                    reuse_key=manifest_model.reuse_key,
                    source_ocr_path=ocr_path,
                    source_manifest_path=manifest_path,
                    manifest_bytes=man_bytes,
                    ready_bytes=None,
                    manifest_sha256=hashlib.sha256(man_bytes).hexdigest(),
                    ready_sha256=None,
                    model=manifest_model.contract.model,
                )
            )

    # Validate exact source binding against snapshot sources and durable lineage
    durable_revisions = {(r.source_id, r.pdf_sha256) for r in revisions_list}
    required_sources: dict[str, SourceRecord] = {}

    for doc in manifest_docs:
        doc_id = _required_text(doc["document_id"])
        pdf_sha = _required_text(doc["pdf"]["sha256"])
        source_id = doc_to_source_id[doc_id]
        if source_id not in all_snapshot_sources:
            raise StateSeedError(f"source_record_missing: {source_id}")
        rec = all_snapshot_sources[source_id]
        if rec.document_id(pdf_sha) != doc_id:
            raise StateSeedError(f"document_identity_mismatch: {doc_id}")
        if source_id not in sources_dict:
            raise StateSeedError(f"durable_source_missing: {source_id}")
        durable_source = sources_dict[source_id]
        if (
            durable_source.identity.issuer != rec.issuer
            or durable_source.identity.product_code != rec.product_code
            or durable_source.identity.document_type != rec.document_type
            or durable_source.identity.source_url != rec.source_url
            or durable_source.identity.source_version != rec.source_version
            or durable_source.identity.source_post_id != rec.source_post_id
        ):
            raise StateSeedError(f"durable_source_mismatch: {source_id}")
        if (source_id, pdf_sha) not in durable_revisions:
            raise StateSeedError(f"durable_revision_missing: {source_id} {pdf_sha}")
        required_sources[source_id] = rec

    final_ocr_entries: list[StateSeedOCREntry] = []
    for entry in ocr_entries:
        sid = doc_to_source_id[entry.document_id]
        final_ocr_entries.append(replace(entry, source_id=sid))

    # Assemble ledger payload
    ledger_payload = {
        "accepted_pdf_hashes": sorted(cas_objects_dict.keys()),
        "contract_sha256": contract_sha256,
        "corpus_sha256": corpus_sha256,
        "counts": {
            "accepted_objects": len(cas_objects_dict),
            "accepted_revisions": len(revisions_list),
            "accepted_sources": len(sources_dict),
            "ocr_documents": len(final_ocr_entries),
            "native_ocr": sum(1 for e in final_ocr_entries if e.kind == "native"),
            "adopted_ocr": sum(1 for e in final_ocr_entries if e.kind == "adopted"),
            "prior_current": len(prior_current_set),
            "prior_historical": len(prior_historical_set),
            "source_records": len(required_sources),
        },
        "generation_id": generation_id,
        "ocr_documents": [
            {
                "document_id": e.document_id,
                "issuer": e.issuer,
                "kind": e.kind,
                "manifest_sha256": e.manifest_sha256,
                "model": e.model,
                "ocr_sha256": e.ocr_sha256,
                "ocr_size_bytes": e.ocr_size_bytes,
                "page_count": e.page_count,
                "pdf_sha256": e.pdf_sha256,
                "pdf_size_bytes": e.pdf_size_bytes,
                "reuse_key": e.reuse_key,
                "source_id": e.source_id,
            }
            for e in final_ocr_entries
        ],
        "pin_status": PIN_STATUS,
        "prior_current_doc_ids": sorted(prior_current_set),
        "prior_historical_doc_ids": sorted(prior_historical_set),
        "run_id": run_id,
        "schema_version": LEDGER_SCHEMA_VERSION,
        "source_database": {
            "device": before_stat[0],
            "inode": before_stat[1],
            "sha256": database_sha256,
            "size_bytes": before_stat[2],
        },
        "source_records": [
            {
                "category": s.category,
                "discovered_at": s.discovered_at.isoformat(),
                "document_type": s.document_type,
                "effective_date": s.effective_date.isoformat(),
                "file_name": s.file_name,
                "issuer": s.issuer,
                "metadata": dict(s.metadata),
                "product_code": s.product_code,
                "product_name": s.product_name,
                "source_id": s.source_id,
                "source_post_id": s.source_post_id,
                "source_url": s.source_url,
                "source_version": s.source_version,
            }
            for s in sorted(required_sources.values(), key=lambda r: r.source_id)
        ],
        "source_root": {
            "device": root_identity[0],
            "inode": root_identity[1],
            "path_sha256": hashlib.sha256(os.fspath(root).encode("utf-8")).hexdigest(),
        },
        "status": "applied",
    }
    ledger_bytes = canonical_json_bytes(ledger_payload)
    if len(ledger_bytes) > MAX_LEDGER_BYTES:
        raise StateSeedError("seed_ledger_too_large")
    ledger_sha256 = hashlib.sha256(ledger_bytes).hexdigest()

    return StateSeedV122Plan(
        source_root=root,
        database_path=database_path,
        source_root_identity=root_identity,
        database_identity=before_stat,
        source_database_sha256=database_sha256,
        generation_id=generation_id,
        run_id=run_id,
        terminal_run=terminal_run,
        snapshots=snapshots,
        cas_objects=tuple(cas_objects_dict.values()),
        sources=tuple(sources_dict.values()),
        revisions=tuple(revisions_list),
        ocr_entries=tuple(final_ocr_entries),
        prior_current_doc_ids=tuple(sorted(prior_current_set)),
        prior_historical_doc_ids=tuple(sorted(prior_historical_set)),
        source_records=tuple(sorted(required_sources.values(), key=lambda r: r.source_id)),
        ledger_bytes=ledger_bytes,
        ledger_sha256=ledger_sha256,
    )


def _ensure_destination_dir(state_dir: Path, relative_dir: Path) -> Path:
    root = _absolute_without_resolving(state_dir)
    _require_secure_directory(root)
    current = root
    for part in relative_dir.parts:
        current /= part
        created = False
        try:
            os.mkdir(current, mode=0o700)
            created = True
        except FileExistsError:
            pass
        except OSError as exc:
            raise StateSeedError("destination_directory_creation_failed") from exc
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            raise StateSeedError("unsafe_destination_path") from None
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise StateSeedError("unsafe_destination_path")
        if created:
            pfd = _open_directory(current.parent, code="unsafe_destination_path")
            try:
                os.fsync(pfd)
            except OSError as exc:
                raise StateSeedError("destination_directory_creation_failed") from exc
            finally:
                os.close(pfd)
    return current


def _persist_ledger(state_dir: Path, plan: StateSeedV122Plan) -> Path:
    ledger_dir = _ensure_destination_dir(state_dir, _LEDGER_DIRECTORY)
    dir_fd = _open_directory(ledger_dir, code="unsafe_destination_ledger_path")
    final_name = f"{plan.ledger_sha256}.json"
    temp_name = f".{plan.ledger_sha256}.{uuid.uuid4().hex}.tmp"
    temp_fd = -1
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        temp_fd = os.open(temp_name, flags, 0o600, dir_fd=dir_fd)
        view = memoryview(plan.ledger_bytes)
        while view:
            written = os.write(temp_fd, view)
            if written < 1:
                raise OSError("seed ledger write stalled")
            view = view[written:]
        os.fsync(temp_fd)
        os.close(temp_fd)
        temp_fd = -1
        try:
            os.link(temp_name, final_name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd, follow_symlinks=False)
        except FileExistsError:
            listed = os.stat(final_name, dir_fd=dir_fd, follow_symlinks=False)
            if stat.S_ISLNK(listed.st_mode) or not stat.S_ISREG(listed.st_mode) or listed.st_size != len(plan.ledger_bytes):
                raise StateSeedError("seed_ledger_conflict") from None
        os.unlink(temp_name, dir_fd=dir_fd)
        os.fsync(dir_fd)
    except OSError as exc:
        raise StateSeedError("seed_ledger_write_failed") from exc
    finally:
        if temp_fd >= 0:
            os.close(temp_fd)
        with suppress(FileNotFoundError):
            os.unlink(temp_name, dir_fd=dir_fd)
        os.close(dir_fd)
    return ledger_dir / final_name


def apply_state_seed_v122(
    plan: StateSeedV122Plan,
    state: WorkerState,
    destination_state_dir: Path,
    pdf_cache: PDFCache | None = None,
) -> dict[str, Any]:
    """Import verified state and OCR seed idempotently into destination candidate."""
    destination = _absolute_without_resolving(destination_state_dir)
    if paths_overlap(plan.source_root, destination):
        raise StateSeedError("source_destination_overlap")

    _ensure_destination_dir(destination, _LEDGER_DIRECTORY)
    _ensure_destination_dir(destination, _OCR_SEED_DIRECTORY)

    if pdf_cache is None:
        pdf_cache = PDFCache(destination, state)

    # 1. Materialize PDF CAS
    imported_objects = 0
    reused_objects = 0
    cas_by_hash = {obj.pdf_sha256: obj for obj in plan.cas_objects}
    for obj in plan.cas_objects:
        existed = state.pdf_cache_object(obj.pdf_sha256) is not None
        pdf_cache.ingest(
            obj.source_path,
            expected_sha256=obj.pdf_sha256,
            expected_size_bytes=obj.size_bytes,
            expected_page_count=obj.page_count,
        )
        if existed:
            reused_objects += 1
        else:
            imported_objects += 1

    # 2. Replay Lineage
    sources = {s.identity.source_id: s for s in plan.sources}
    histories: defaultdict[str, list[StateSeedRevision]] = defaultdict(list)
    for rev in plan.revisions:
        histories[rev.source_id].append(rev)

    ordered_sources = _source_replay_order(sources)
    imported_revisions = 0
    reused_revisions = 0

    for source in ordered_sources:
        desired = sorted(histories[source.identity.source_id], key=lambda item: item.revision_id)
        existing = state.pdf_cache_source_history(source.identity.source_id)
        if len(existing) > len(desired):
            raise StateSeedError("destination_revision_conflict")
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
                raise StateSeedError("destination_revision_conflict")
        reused_revisions += len(existing)
        for revision in desired[len(existing) :]:
            item = cas_by_hash.get(revision.pdf_sha256)
            if item is not None:
                pdf_cache.ingest_and_bind(
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
                    pdf_cache.ingest_and_bind(
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
            else:
                state.record_pdf_cache_object(
                    pdf_sha256=revision.pdf_sha256,
                    size_bytes=revision.pdf_size_bytes,
                    page_count=revision.page_count,
                    relative_path=f"objects/sha256/{revision.pdf_sha256[:2]}/{revision.pdf_sha256}",
                    verified_at=revision.verified_at,
                )
                first_obs = revision.first_observed_at.isoformat()
                last_obs = revision.last_observed_at.isoformat()
                ver_at = revision.verified_at.isoformat()
                with state.transaction() as conn:
                    src = conn.execute(
                        "SELECT source_id FROM pdf_cache_source WHERE source_id=?",
                        (source.identity.source_id,),
                    ).fetchone()
                    if src is None:
                        conn.execute(
                            """INSERT INTO pdf_cache_source(
                                source_id, issuer, product_code, document_type,
                                source_url, source_version, source_post_id,
                                discovery_sha256, first_observed_at, last_observed_at,
                                last_verified_at, superseded_by_source_id, superseded_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (
                                source.identity.source_id,
                                source.identity.issuer,
                                source.identity.product_code,
                                source.identity.document_type,
                                source.identity.source_url,
                                source.identity.source_version,
                                source.identity.source_post_id,
                                source.identity.discovery_sha256,
                                first_obs,
                                last_obs,
                                ver_at,
                                None,
                                None,
                            ),
                        )
                    conn.execute(
                        """INSERT INTO pdf_cache_source_revision(
                            source_id, pdf_sha256, pdf_size_bytes, page_count,
                            final_url, etag, last_modified,
                            first_observed_at, last_observed_at, verified_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            source.identity.source_id,
                            revision.pdf_sha256,
                            revision.pdf_size_bytes,
                            revision.page_count,
                            revision.final_url,
                            revision.etag,
                            revision.last_modified,
                            first_obs,
                            last_obs,
                            ver_at,
                        ),
                    )
            imported_revisions += 1

    # Update superseded sources after all sources have been inserted
    with state.transaction() as conn:
        for source in ordered_sources:
            if source.superseded_by_source_id:
                conn.execute(
                    "UPDATE pdf_cache_source SET superseded_by_source_id=?, superseded_at=? WHERE source_id=?",
                    (
                        source.superseded_by_source_id,
                        source.superseded_at.isoformat() if source.superseded_at else None,
                        source.identity.source_id,
                    ),
                )

    # 3. Terminal Run row & Snapshot rows
    with state.transaction() as conn:
        existing_run = conn.execute("SELECT status FROM run WHERE run_id=?", (plan.run_id,)).fetchone()
        if existing_run is None:
            conn.execute(
                """INSERT INTO run(run_id, started_at, finished_at, status, corpus_sha256, contract_sha256, error)
                   VALUES(?, ?, ?, ?, ?, ?, ?)""",
                (
                    plan.terminal_run.run_id,
                    plan.terminal_run.started_at,
                    plan.terminal_run.finished_at,
                    plan.terminal_run.status,
                    plan.terminal_run.corpus_sha256,
                    plan.terminal_run.contract_sha256,
                    plan.terminal_run.error,
                ),
            )
        for snap in plan.snapshots:
            existing_snap = conn.execute(
                "SELECT snapshot_id FROM snapshot WHERE run_id=? AND snapshot_id=?",
                (snap.run_id, snap.snapshot_id),
            ).fetchone()
            if existing_snap is None:
                conn.execute(
                    """INSERT INTO snapshot(snapshot_id, run_id, issuer, observed_at, source_sha256, record_count, payload_json)
                       VALUES(?, ?, ?, ?, ?, ?, ?)""",
                    (
                        snap.snapshot_id,
                        snap.run_id,
                        snap.issuer,
                        snap.observed_at,
                        snap.source_sha256,
                        snap.record_count,
                        snap.payload_json,
                    ),
                )

    # 4. Materialize OCR seed artifacts
    ocr_seed_root = destination / _OCR_SEED_DIRECTORY
    imported_ocr_files = 0
    reused_ocr_files = 0

    for entry in plan.ocr_entries:
        doc_dir = ocr_seed_root / entry.document_id
        doc_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

        target_ocr = doc_dir / "ocr.md"
        if target_ocr.is_file():
            if target_ocr.stat().st_size == entry.ocr_size_bytes and hashlib.sha256(target_ocr.read_bytes()).hexdigest() == entry.ocr_sha256:
                reused_ocr_files += 1
            else:
                raise StateSeedError(f"destination_ocr_conflict for {entry.document_id}")
        else:
            ocr_content = entry.source_ocr_path.read_bytes()
            if hashlib.sha256(ocr_content).hexdigest() != entry.ocr_sha256:
                raise StateSeedError(f"source_ocr_corrupt for {entry.document_id}")
            tmp_ocr = doc_dir / f".ocr.{uuid.uuid4().hex}.tmp"
            tmp_ocr.write_bytes(ocr_content)
            os.chmod(tmp_ocr, 0o600)
            tmp_ocr.replace(target_ocr)
            imported_ocr_files += 1

        if entry.manifest_bytes is not None:
            target_man = doc_dir / "manifest.json"
            if target_man.is_file():
                if hashlib.sha256(target_man.read_bytes()).hexdigest() == entry.manifest_sha256:
                    reused_ocr_files += 1
                else:
                    raise StateSeedError(f"destination_manifest_conflict for {entry.document_id}")
            else:
                tmp_man = doc_dir / f".man.{uuid.uuid4().hex}.tmp"
                tmp_man.write_bytes(entry.manifest_bytes)
                os.chmod(tmp_man, 0o600)
                tmp_man.replace(target_man)
                imported_ocr_files += 1

    # 5. Persist ledger
    ledger_path = _persist_ledger(destination, plan)

    return plan.report(
        applied=True,
        imported_pdf_objects=imported_objects,
        reused_pdf_objects=reused_objects,
        imported_revisions=imported_revisions,
        reused_revisions=reused_revisions,
        imported_ocr_files=imported_ocr_files,
        reused_ocr_files=reused_ocr_files,
        ledger_path=str(ledger_path.relative_to(destination)),
    )


@dataclass(frozen=True, slots=True)
class StateSeedLedger:
    generation_id: str
    run_id: str
    corpus_sha256: str
    contract_sha256: str
    entries_by_doc_id: dict[str, StateSeedOCREntry]
    seed_pdf_shas: frozenset[str]
    seed_doc_ids: frozenset[str]
    prior_current_doc_ids: frozenset[str]
    prior_historical_doc_ids: frozenset[str]
    source_records: dict[str, SourceRecord] = field(default_factory=dict)


def load_state_seed_ledger(state_dir: Path) -> StateSeedLedger | None:
    """Load and validate the state seed ledger if present."""
    root = _absolute_without_resolving(state_dir)
    ledger_dir = root / _LEDGER_DIRECTORY
    if not ledger_dir.is_dir() or ledger_dir.is_symlink():
        return None

    entries = sorted(ledger_dir.glob("*.json"))
    if not entries:
        return None

    # Load latest ledger
    ledger_file = entries[-1]
    expected_sha = ledger_file.stem
    if not _SHA256.fullmatch(expected_sha):
        raise StateSeedError("invalid_seed_ledger_filename")
    if ledger_file.is_symlink() or not ledger_file.is_file():
        raise StateSeedError("invalid_seed_ledger_file")

    content = ledger_file.read_bytes()
    if hashlib.sha256(content).hexdigest() != expected_sha:
        raise StateSeedError("seed_ledger_hash_mismatch")

    try:
        data = json.loads(content)
    except Exception as exc:
        raise StateSeedError("seed_ledger_invalid_json") from exc

    if data.get("schema_version") != LEDGER_SCHEMA_VERSION or data.get("status") != "applied":
        raise StateSeedError("seed_ledger_schema_mismatch")

    source_records_list = data.get("source_records")
    if not isinstance(source_records_list, list) or not source_records_list:
        raise StateSeedError("seed_ledger_source_records_missing")

    source_records: dict[str, SourceRecord] = {}
    for item in source_records_list:
        if not isinstance(item, dict):
            raise StateSeedError("seed_ledger_invalid_source_record")
        sid = item.get("source_id")
        if not sid or not isinstance(sid, str) or not _SOURCE_ID.fullmatch(sid):
            raise StateSeedError("seed_ledger_invalid_source_record")
        disc_at = _parse_timestamp(item.get("discovered_at"))
        assert disc_at is not None
        try:
            s_eff_date = date.fromisoformat(str(item["effective_date"]))
        except Exception as exc:
            raise StateSeedError("seed_ledger_invalid_source_record") from exc
        rec = SourceRecord(
            issuer=str(item["issuer"]),
            product_code=str(item["product_code"]),
            product_name=str(item["product_name"]),
            effective_date=s_eff_date,
            source_version=str(item["source_version"]),
            source_url=str(item["source_url"]),
            source_post_id=str(item.get("source_post_id") or ""),
            file_name=str(item["file_name"]),
            category=str(item.get("category", "credit")),
            discovered_at=disc_at,
            metadata=item.get("metadata", {}),
            document_type=str(item.get("document_type", "product_description")),
        )
        if rec.source_id != sid:
            raise StateSeedError("seed_ledger_source_id_mismatch")
        source_records[sid] = rec

    ocr_entries_by_id: dict[str, StateSeedOCREntry] = {}
    ocr_seed_root = root / _OCR_SEED_DIRECTORY
    seed_pdf_shas: set[str] = set()
    seed_doc_ids: set[str] = set()

    for item in data.get("ocr_documents", []):
        doc_id = item["document_id"]
        pdf_sha = item["pdf_sha256"]
        sid = item.get("source_id")
        if not sid or sid not in source_records:
            raise StateSeedError(f"seed_ledger_source_missing: {doc_id}")
        rec = source_records[sid]
        if rec.document_id(pdf_sha) != doc_id:
            raise StateSeedError(f"seed_ledger_source_binding_mismatch: {doc_id}")

        seed_pdf_shas.add(pdf_sha)
        seed_doc_ids.add(doc_id)
        ocr_path = ocr_seed_root / doc_id / "ocr.md"
        man_path = ocr_seed_root / doc_id / "manifest.json"
        ocr_entries_by_id[doc_id] = StateSeedOCREntry(
            document_id=doc_id,
            issuer=item["issuer"],
            source_id=sid,
            pdf_sha256=pdf_sha,
            pdf_size_bytes=int(item["pdf_size_bytes"]),
            page_count=int(item["page_count"]),
            ocr_sha256=item["ocr_sha256"],
            ocr_size_bytes=int(item["ocr_size_bytes"]),
            kind=item["kind"],
            reuse_key=item["reuse_key"],
            source_ocr_path=ocr_path,
            source_manifest_path=man_path if man_path.is_file() else None,
            manifest_bytes=None,
            ready_bytes=None,
            manifest_sha256=item.get("manifest_sha256"),
            ready_sha256=item.get("ready_sha256"),
            model=item.get("model", "PaddleOCR-VL-1.6"),
        )

    return StateSeedLedger(
        generation_id=data["generation_id"],
        run_id=data["run_id"],
        corpus_sha256=data["corpus_sha256"],
        contract_sha256=data["contract_sha256"],
        entries_by_doc_id=ocr_entries_by_id,
        seed_pdf_shas=frozenset(seed_pdf_shas),
        seed_doc_ids=frozenset(seed_doc_ids),
        prior_current_doc_ids=frozenset(data.get("prior_current_doc_ids", [])),
        prior_historical_doc_ids=frozenset(data.get("prior_historical_doc_ids", [])),
        source_records=source_records,
    )
