"""Read-only v1.0.8 run inventory and safe PDF cache seeding."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .contracts import SourceRecord, canonical_json_bytes, canonical_sha256
from .downloader import PDFValidationError, validate_pdf
from .pdf_cache import PDFCache, PDFSourceIdentity

REPORT_SCHEMA_VERSION = "cardrag.cache-seed-report.v1"
LEDGER_SCHEMA_VERSION = "cardrag.cache-seed-ledger.v1"
REPORT_SAMPLE_LIMIT = 50
MISSING_SAMPLE_LIMIT = 50
MAX_DATABASE_BYTES = 1024 * 1024 * 1024
MAX_RUNS = 10_000
MAX_SNAPSHOTS = 50_000
MAX_SOURCE_OCCURRENCES = 500_000
MAX_CANDIDATES = 100_000
MAX_SNAPSHOT_PAYLOAD_BYTES = 32 * 1024 * 1024
MAX_PDF_BYTES = 100 * 1024 * 1024
MAX_TOTAL_PDF_BYTES = 100 * 1024 * 1024 * 1024
MAX_LEDGER_BYTES = 256 * 1024 * 1024

_LEDGER_DIRECTORY = Path("audit-reports/cache-seed")

_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SOURCE_FILE = re.compile(r"^(source_[0-9a-f]{64})\.pdf$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_ID = re.compile(r"^source_[0-9a-f]{64}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_RUN_STATUSES = frozenset({"succeeded", "failed", "no_change"})
_DISCOVERY_KEYS = frozenset(
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
_RUN_COLUMNS = (
    "run_id",
    "started_at",
    "finished_at",
    "status",
    "corpus_sha256",
    "contract_sha256",
    "error",
)
_SNAPSHOT_COLUMNS = (
    "snapshot_id",
    "run_id",
    "issuer",
    "observed_at",
    "source_sha256",
    "record_count",
    "payload_json",
)
_SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


class CacheSeedError(RuntimeError):
    """A safe, bounded failure category suitable for a CLI report."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class _Run:
    run_id: str
    started_at: datetime
    finished_at: datetime
    status: str


@dataclass(frozen=True, slots=True)
class _Occurrence:
    run_id: str
    observed_at: datetime
    source: SourceRecord


@dataclass(frozen=True, slots=True)
class CacheSeedCandidate:
    run_id: str
    directory: str
    observed_at: datetime
    source: SourceRecord
    source_path: Path
    pdf_sha256: str
    size_bytes: int
    page_count: int

    @property
    def sort_key(self) -> tuple[datetime, int, str, str]:
        directory_order = 0 if self.directory == "downloads" else 1
        return (self.observed_at, directory_order, self.run_id, self.source.source_id)


@dataclass(frozen=True, slots=True)
class CacheSeedMissingSource:
    run_id: str
    observed_at: datetime
    source: SourceRecord

    @property
    def sort_key(self) -> tuple[str, str]:
        return (self.run_id, self.source.source_id)


@dataclass(frozen=True, slots=True)
class CacheSeedPlan:
    legacy_root: Path
    database_path: Path
    database_identity: tuple[int, int, int, int, int]
    skipped_stale_run_ids: frozenset[str]
    run_count: int
    snapshot_count: int
    source_occurrence_count: int
    missing_source_files: int
    candidates: tuple[CacheSeedCandidate, ...]
    missing_sources: tuple[CacheSeedMissingSource, ...]
    legacy_database_sha256: str
    ledger_bytes: bytes
    ledger_sha256: str

    def report(
        self,
        *,
        applied: bool,
        applied_candidates: int = 0,
        reused_candidates: int = 0,
        created_pdf_objects: int = 0,
        created_revisions: int = 0,
        ledger_path: str | None = None,
    ) -> dict[str, Any]:
        candidates = self.candidates
        unique_objects = {candidate.pdf_sha256: candidate.size_bytes for candidate in candidates}
        sample = [
            {
                "directory": candidate.directory,
                "issuer": candidate.source.issuer,
                "observed_at": candidate.observed_at.isoformat(),
                "pdf_sha256": candidate.pdf_sha256,
                "product_code": candidate.source.product_code,
                "run_id": candidate.run_id,
                "size_bytes": candidate.size_bytes,
                "source_id": candidate.source.source_id,
            }
            for candidate in candidates[:REPORT_SAMPLE_LIMIT]
        ]
        missing_sample = [
            {
                "issuer": missing.source.issuer,
                "product_code": missing.source.product_code,
                "reason": "no_exact_legacy_pdf",
                "run_id": missing.run_id,
                "source_id": missing.source.source_id,
            }
            for missing in self.missing_sources[:MISSING_SAMPLE_LIMIT]
        ]
        return {
            "applied_candidates": applied_candidates,
            "candidate_bytes": sum(candidate.size_bytes for candidate in candidates),
            "candidate_files": len(candidates),
            "created_pdf_objects": created_pdf_objects,
            "created_revisions": created_revisions,
            "dry_run": not applied,
            "ledger_accepted_candidates": len(candidates),
            "ledger_missing_sources": len(self.missing_sources),
            "ledger_path": ledger_path,
            "ledger_sha256": self.ledger_sha256,
            "ledger_size_bytes": len(self.ledger_bytes),
            "ledger_skipped_stale_runs": len(self.skipped_stale_run_ids),
            "ledger_unique_pdf_hashes": len(unique_objects),
            "legacy_root": str(self.legacy_root),
            "legacy_database_sha256": self.legacy_database_sha256,
            "missing_source_files": self.missing_source_files,
            "missing_sample": missing_sample,
            "missing_sample_truncated": max(0, len(self.missing_sources) - len(missing_sample)),
            "run_count": self.run_count,
            "reused_candidates": reused_candidates,
            "sample": sample,
            "sample_truncated": max(0, len(candidates) - len(sample)),
            "schema_version": REPORT_SCHEMA_VERSION,
            "snapshot_count": self.snapshot_count,
            "source_occurrence_count": self.source_occurrence_count,
            "skipped_stale_runs": len(self.skipped_stale_run_ids),
            "status": "applied" if applied else "verified",
            "unique_pdf_bytes": sum(unique_objects.values()),
            "unique_pdf_objects": len(unique_objects),
            "unique_sources": len({candidate.source.source_id for candidate in candidates}),
        }


def _absolute_without_resolving(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _path_components(path: Path) -> tuple[Path, ...]:
    absolute = _absolute_without_resolving(path)
    current = Path(absolute.anchor)
    result = [current]
    for part in absolute.parts[1:]:
        current = current / part
        result.append(current)
    return tuple(result)


def _require_secure_directory(path: Path, *, missing_ok: bool = False) -> bool:
    components = _path_components(path)
    for index, component in enumerate(components):
        try:
            mode = component.lstat().st_mode
        except FileNotFoundError:
            if missing_ok and index == len(components) - 1:
                return False
            raise CacheSeedError("unsafe_legacy_path") from None
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise CacheSeedError("unsafe_legacy_path")
    return True


def _open_regular(path: Path, *, error_code: str) -> int:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        raise CacheSeedError(error_code) from None
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise CacheSeedError(error_code)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise CacheSeedError(error_code) from exc
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise CacheSeedError(error_code)
    return descriptor


def _descriptor_identity(descriptor: int) -> tuple[int, int, int, int, int]:
    current = os.fstat(descriptor)
    return (
        current.st_dev,
        current.st_ino,
        current.st_size,
        current.st_mtime_ns,
        current.st_ctime_ns,
    )


def _descriptor_sha256(descriptor: int) -> str:
    digest = hashlib.sha256()
    os.lseek(descriptor, 0, os.SEEK_SET)
    while block := os.read(descriptor, 1024 * 1024):
        digest.update(block)
    os.lseek(descriptor, 0, os.SEEK_SET)
    return digest.hexdigest()


def _parse_timestamp(raw: object, *, error_code: str) -> datetime:
    if not isinstance(raw, str) or len(raw) > 64:
        raise CacheSeedError(error_code)
    try:
        value = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise CacheSeedError(error_code) from exc
    if value.tzinfo is None or value.utcoffset() is None:
        raise CacheSeedError(error_code)
    return value.astimezone(UTC)


def _sidecar_paths(database_path: Path) -> tuple[Path, ...]:
    return tuple(Path(str(database_path) + suffix) for suffix in _SQLITE_SIDECAR_SUFFIXES)


def _require_no_sqlite_sidecars(database_path: Path) -> None:
    for sidecar in _sidecar_paths(database_path):
        try:
            sidecar.lstat()
        except FileNotFoundError:
            continue
        raise CacheSeedError("sqlite_sidecar_present")


def _validate_table_columns(connection: sqlite3.Connection, table: str, expected: tuple[str, ...]) -> None:
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    if tuple(str(row["name"]) for row in rows) != expected:
        raise CacheSeedError("invalid_legacy_database")


def _restore_source(raw: object, *, issuer: str, observed_at: datetime) -> SourceRecord:
    if not isinstance(raw, dict) or set(raw) != _DISCOVERY_KEYS or not isinstance(raw.get("metadata"), dict):
        raise CacheSeedError("invalid_snapshot")
    string_fields = (
        "category",
        "document_type",
        "effective_date",
        "file_name",
        "issuer",
        "product_code",
        "product_name",
        "source_post_id",
        "source_url",
        "source_version",
    )
    if any(not isinstance(raw.get(field), str) for field in string_fields):
        raise CacheSeedError("invalid_snapshot")
    try:
        source = SourceRecord(
            issuer=raw["issuer"],
            product_code=raw["product_code"],
            product_name=raw["product_name"],
            effective_date=date.fromisoformat(raw["effective_date"]),
            source_version=raw["source_version"],
            source_url=raw["source_url"],
            source_post_id=raw["source_post_id"],
            file_name=raw["file_name"],
            category=raw["category"],
            discovered_at=observed_at,
            metadata=raw["metadata"],
            document_type=raw["document_type"],
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise CacheSeedError("invalid_snapshot") from exc
    parsed_url = urlsplit(source.source_url)
    if (
        source.issuer != issuer
        or source.discovery_payload != raw
        or not _SOURCE_ID.fullmatch(source.source_id)
        or source.issuer != source.issuer.strip()
        or source.product_code != source.product_code.strip()
        or source.source_version != source.source_version.strip()
        or source.source_post_id != source.source_post_id.strip()
        or source.source_url != source.source_url.strip()
        or _CONTROL.search(source.source_url)
        or len(source.issuer) > 64
        or len(source.product_code) > 512
        or len(source.source_version) > 512
        or len(source.source_post_id) > 512
        or len(source.source_url) > 4096
        or parsed_url.scheme != "https"
        or not parsed_url.hostname
        or parsed_url.username is not None
        or parsed_url.password is not None
        or parsed_url.fragment
    ):
        raise CacheSeedError("invalid_snapshot")
    return source


def _parse_snapshot_row(row: sqlite3.Row) -> tuple[_Occurrence, ...]:
    observed_at = _parse_timestamp(row["observed_at"], error_code="invalid_snapshot")
    payload_json = row["payload_json"]
    if not isinstance(payload_json, str) or len(payload_json.encode("utf-8")) > MAX_SNAPSHOT_PAYLOAD_BYTES:
        raise CacheSeedError("snapshot_limit_exceeded")
    try:
        payload = json.loads(payload_json)
    except (TypeError, ValueError) as exc:
        raise CacheSeedError("invalid_snapshot") from exc
    issuer = row["issuer"]
    if (
        not isinstance(payload, dict)
        or set(payload)
        != {
            "contract_version",
            "issuer",
            "parser_version",
            "records",
            "source_url",
        }
        or payload.get("contract_version") != "cardrag.source-snapshot.v1"
        or not isinstance(issuer, str)
        or payload.get("issuer") != issuer
        or not isinstance(payload.get("parser_version"), str)
        or not payload["parser_version"]
        or not isinstance(payload.get("source_url"), str)
        or not isinstance(payload.get("records"), list)
    ):
        raise CacheSeedError("invalid_snapshot")
    snapshot_id = row["snapshot_id"]
    source_sha256 = row["source_sha256"]
    try:
        computed_snapshot_id = canonical_sha256(payload)
    except (TypeError, ValueError) as exc:
        raise CacheSeedError("invalid_snapshot") from exc
    if (
        not isinstance(snapshot_id, str)
        or not isinstance(source_sha256, str)
        or not _SHA256.fullmatch(snapshot_id)
        or source_sha256 != snapshot_id
        or computed_snapshot_id != snapshot_id
        or not isinstance(row["record_count"], int)
        or row["record_count"] != len(payload["records"])
    ):
        raise CacheSeedError("invalid_snapshot")

    run_id = row["run_id"]
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id) or run_id in {".", ".."}:
        raise CacheSeedError("invalid_legacy_database")
    occurrences: list[_Occurrence] = []
    seen: set[str] = set()
    for raw in payload["records"]:
        source = _restore_source(raw, issuer=issuer, observed_at=observed_at)
        if source.source_id in seen:
            raise CacheSeedError("invalid_snapshot")
        seen.add(source.source_id)
        occurrences.append(_Occurrence(run_id=run_id, observed_at=observed_at, source=source))
    return tuple(occurrences)


def _read_legacy_state(
    database_path: Path,
) -> tuple[
    dict[str, _Run],
    tuple[_Occurrence, ...],
    int,
    tuple[int, int, int, int, int],
    frozenset[str],
    str,
]:
    _require_no_sqlite_sidecars(database_path)
    descriptor = _open_regular(database_path, error_code="invalid_legacy_database")
    before = _descriptor_identity(descriptor)
    if before[2] < 1 or before[2] > MAX_DATABASE_BYTES:
        os.close(descriptor)
        raise CacheSeedError("database_limit_exceeded")
    connection: sqlite3.Connection | None = None
    try:
        database_sha256 = _descriptor_sha256(descriptor)
        connection = sqlite3.connect(
            f"file:/proc/self/fd/{descriptor}?mode=ro&immutable=1",
            uri=True,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        integrity = connection.execute("PRAGMA integrity_check(1)").fetchone()
        if integrity is None or str(integrity[0]) != "ok":
            raise CacheSeedError("invalid_legacy_database")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise CacheSeedError("invalid_legacy_database")
        table_rows = connection.execute(
            "SELECT name,sql FROM sqlite_schema WHERE type='table' ORDER BY name"
        ).fetchall()
        tables = {str(row["name"]): str(row["sql"] or "") for row in table_rows}
        if (
            "run" not in tables
            or "snapshot" not in tables
            or any(name.startswith("pdf_cache_") for name in tables)
        ):
            raise CacheSeedError("invalid_legacy_database")
        if "'interrupted'" in tables["run"]:
            raise CacheSeedError("invalid_legacy_database")
        _validate_table_columns(connection, "run", _RUN_COLUMNS)
        _validate_table_columns(connection, "snapshot", _SNAPSHOT_COLUMNS)

        run_rows = connection.execute(
            "SELECT run_id,started_at,finished_at,status FROM run ORDER BY started_at,run_id LIMIT ?",
            (MAX_RUNS + 1,),
        ).fetchall()
        if len(run_rows) > MAX_RUNS:
            raise CacheSeedError("run_limit_exceeded")
        parsed_runs: list[tuple[sqlite3.Row, str, str, datetime]] = []
        latest_terminal_start: datetime | None = None
        for row in run_rows:
            run_id = row["run_id"]
            status = row["status"]
            if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id) or run_id in {".", ".."}:
                raise CacheSeedError("invalid_legacy_database")
            if not isinstance(status, str) or status not in _RUN_STATUSES | {"running"}:
                raise CacheSeedError("invalid_legacy_database")
            started_at = _parse_timestamp(row["started_at"], error_code="invalid_legacy_database")
            parsed_runs.append((row, run_id, status, started_at))
            if status in _RUN_STATUSES and (
                latest_terminal_start is None or started_at > latest_terminal_start
            ):
                latest_terminal_start = started_at

        runs: dict[str, _Run] = {}
        stale_run_ids: set[str] = set()
        for row, run_id, status, started_at in parsed_runs:
            if status == "running":
                if row["finished_at"] is not None:
                    raise CacheSeedError("invalid_legacy_database")
                if latest_terminal_start is None or latest_terminal_start <= started_at:
                    raise CacheSeedError("legacy_run_active")
                stale_run_ids.add(run_id)
                continue
            finished_at = _parse_timestamp(row["finished_at"], error_code="invalid_legacy_database")
            if finished_at < started_at:
                raise CacheSeedError("invalid_legacy_database")
            runs[run_id] = _Run(
                run_id=run_id,
                started_at=started_at,
                finished_at=finished_at,
                status=status,
            )

        snapshot_rows = connection.execute(
            """SELECT snapshot_id,run_id,issuer,observed_at,source_sha256,record_count,payload_json
               FROM snapshot ORDER BY observed_at,run_id,issuer,snapshot_id LIMIT ?""",
            (MAX_SNAPSHOTS + 1,),
        ).fetchall()
        if len(snapshot_rows) > MAX_SNAPSHOTS:
            raise CacheSeedError("snapshot_limit_exceeded")
        occurrences: list[_Occurrence] = []
        canonical_sources: dict[str, dict[str, Any]] = {}
        eligible_snapshot_count = 0
        for row in snapshot_rows:
            if row["run_id"] in stale_run_ids:
                continue
            run = runs.get(row["run_id"])
            if run is None:
                raise CacheSeedError("invalid_legacy_database")
            eligible_snapshot_count += 1
            for occurrence in _parse_snapshot_row(row):
                if not run.started_at <= occurrence.observed_at <= run.finished_at:
                    raise CacheSeedError("invalid_snapshot")
                prior = canonical_sources.setdefault(
                    occurrence.source.source_id,
                    occurrence.source.discovery_payload,
                )
                if prior != occurrence.source.discovery_payload:
                    raise CacheSeedError("source_identity_mismatch")
                occurrences.append(occurrence)
                if len(occurrences) > MAX_SOURCE_OCCURRENCES:
                    raise CacheSeedError("source_limit_exceeded")
        return (
            runs,
            tuple(occurrences),
            eligible_snapshot_count,
            before,
            frozenset(stale_run_ids),
            database_sha256,
        )
    except CacheSeedError:
        raise
    except (json.JSONDecodeError, OSError, sqlite3.Error, TypeError, ValueError) as exc:
        raise CacheSeedError("invalid_legacy_database") from exc
    finally:
        if connection is not None:
            connection.close()
        after = _descriptor_identity(descriptor)
        os.close(descriptor)
        _require_no_sqlite_sidecars(database_path)
        if after != before:
            raise CacheSeedError("legacy_database_changed")


def _validated_pdf(path: Path) -> tuple[str, int, int]:
    descriptor = _open_regular(path, error_code="unsafe_legacy_pdf")
    before = _descriptor_identity(descriptor)
    if before[2] < 1 or before[2] > MAX_PDF_BYTES:
        os.close(descriptor)
        raise CacheSeedError("pdf_limit_exceeded")
    try:
        try:
            result = validate_pdf(Path(f"/proc/self/fd/{descriptor}"))
        except (OSError, PDFValidationError) as exc:
            raise CacheSeedError("invalid_legacy_pdf") from exc
        if _descriptor_identity(descriptor) != before:
            raise CacheSeedError("legacy_pdf_changed")
        return result
    finally:
        os.close(descriptor)


def _candidate_for_entry(
    entry: Path,
    *,
    run_id: str,
    directory: str,
    occurrences: tuple[_Occurrence, ...],
) -> CacheSeedCandidate:
    match = _SOURCE_FILE.fullmatch(entry.name)
    if match is None:
        raise CacheSeedError("unbound_legacy_download")
    source_id = match.group(1)
    if not occurrences:
        raise CacheSeedError("unbound_legacy_download")
    payloads = {canonical_sha256(item.source.discovery_payload) for item in occurrences}
    if payloads != {source_id.removeprefix("source_")}:
        raise CacheSeedError("source_identity_mismatch")
    selected = min(occurrences, key=lambda item: item.observed_at)
    if directory == "resume-downloads":
        selected = max(occurrences, key=lambda item: item.observed_at)
    digest, size_bytes, page_count = _validated_pdf(entry)
    return CacheSeedCandidate(
        run_id=run_id,
        directory=directory,
        observed_at=selected.observed_at,
        source=selected.source,
        source_path=entry,
        pdf_sha256=digest,
        size_bytes=size_bytes,
        page_count=page_count,
    )


def _build_ledger_bytes(
    *,
    legacy_root: Path,
    database_identity: tuple[int, int, int, int, int],
    database_sha256: str,
    candidates: tuple[CacheSeedCandidate, ...],
    missing_sources: tuple[CacheSeedMissingSource, ...],
    skipped_stale_run_ids: frozenset[str],
) -> bytes:
    accepted_entries: list[dict[str, Any]] = []
    unique_objects: dict[str, tuple[int, int]] = {}
    for candidate in candidates:
        expected_relative = (
            Path("runs") / candidate.run_id / candidate.directory / f"{candidate.source.source_id}.pdf"
        )
        try:
            actual_relative = candidate.source_path.relative_to(legacy_root)
        except ValueError as exc:  # pragma: no cover - constructed path invariant
            raise CacheSeedError("legacy_path_mismatch") from exc
        if actual_relative != expected_relative:
            raise CacheSeedError("legacy_path_mismatch")
        metadata = (candidate.size_bytes, candidate.page_count)
        prior = unique_objects.setdefault(candidate.pdf_sha256, metadata)
        if prior != metadata:
            raise CacheSeedError("pdf_metadata_mismatch")
        accepted_entries.append(
            {
                "directory": candidate.directory,
                "issuer": candidate.source.issuer,
                "legacy_path": expected_relative.as_posix(),
                "observed_at": candidate.observed_at.isoformat(),
                "page_count": candidate.page_count,
                "pdf_sha256": candidate.pdf_sha256,
                "product_code": candidate.source.product_code,
                "product_name": candidate.source.product_name,
                "run_id": candidate.run_id,
                "size_bytes": candidate.size_bytes,
                "source_id": candidate.source.source_id,
            }
        )
    missing_entries = [
        {
            "issuer": missing.source.issuer,
            "observed_at": missing.observed_at.isoformat(),
            "product_code": missing.source.product_code,
            "product_name": missing.source.product_name,
            "reason": "no_exact_legacy_pdf",
            "run_id": missing.run_id,
            "source_id": missing.source.source_id,
        }
        for missing in missing_sources
    ]
    payload = {
        "accepted_candidates": accepted_entries,
        "counts": {
            "accepted_candidates": len(accepted_entries),
            "missing_sources": len(missing_entries),
            "skipped_stale_runs": len(skipped_stale_run_ids),
            "unique_accepted_pdf_hashes": len(unique_objects),
        },
        "legacy_database": {
            "path": "worker-state.sqlite3",
            "sha256": database_sha256,
            "size_bytes": database_identity[2],
        },
        "missing_sources": missing_entries,
        "schema_version": LEDGER_SCHEMA_VERSION,
        "skipped_stale_run_ids": sorted(skipped_stale_run_ids),
        "unique_accepted_pdf_sha256": sorted(unique_objects),
    }
    body = canonical_json_bytes(payload)
    if len(body) > MAX_LEDGER_BYTES:
        raise CacheSeedError("ledger_limit_exceeded")
    return body


def build_cache_seed_plan(legacy_root: Path) -> CacheSeedPlan:
    root = _absolute_without_resolving(legacy_root)
    if not legacy_root.is_absolute() or root == Path(root.anchor) or len(str(root)) > 4096:
        raise CacheSeedError("invalid_legacy_root")
    _require_secure_directory(root)
    database_path = root / "worker-state.sqlite3"
    (
        runs,
        occurrences,
        snapshot_count,
        database_identity,
        skipped_stale_run_ids,
        legacy_database_sha256,
    ) = _read_legacy_state(database_path)
    by_run_source: dict[tuple[str, str], list[_Occurrence]] = defaultdict(list)
    for occurrence in occurrences:
        by_run_source[(occurrence.run_id, occurrence.source.source_id)].append(occurrence)

    runs_root = root / "runs"
    preliminary: list[CacheSeedCandidate] = []
    matched_run_sources: set[tuple[str, str]] = set()
    if _require_secure_directory(runs_root, missing_ok=True):
        for run_directory in sorted(runs_root.iterdir(), key=lambda path: path.name):
            try:
                mode = run_directory.lstat().st_mode
            except FileNotFoundError:
                raise CacheSeedError("unsafe_legacy_path") from None
            if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode) or run_directory.parent != runs_root:
                raise CacheSeedError("unsafe_legacy_path")
            if run_directory.name in skipped_stale_run_ids:
                continue
            if run_directory.name not in runs:
                raise CacheSeedError("unsafe_legacy_path")
            for directory_name in ("downloads", "resume-downloads"):
                download_directory = run_directory / directory_name
                if not _require_secure_directory(download_directory, missing_ok=True):
                    continue
                for entry in sorted(download_directory.iterdir(), key=lambda path: path.name):
                    match = _SOURCE_FILE.fullmatch(entry.name)
                    source_id = match.group(1) if match else ""
                    key = (run_directory.name, source_id)
                    candidate = _candidate_for_entry(
                        entry,
                        run_id=run_directory.name,
                        directory=directory_name,
                        occurrences=tuple(by_run_source.get(key, ())),
                    )
                    preliminary.append(candidate)
                    matched_run_sources.add(key)
                    if len(preliminary) > MAX_CANDIDATES:
                        raise CacheSeedError("candidate_limit_exceeded")

    candidates = tuple(sorted(preliminary, key=lambda item: item.sort_key))
    total_bytes = sum(candidate.size_bytes for candidate in candidates)
    if total_bytes > MAX_TOTAL_PDF_BYTES:
        raise CacheSeedError("pdf_limit_exceeded")
    missing: list[CacheSeedMissingSource] = []
    for run_id, source_id in sorted(set(by_run_source).difference(matched_run_sources)):
        selected = min(
            by_run_source[(run_id, source_id)],
            key=lambda item: item.observed_at,
        )
        missing.append(
            CacheSeedMissingSource(
                run_id=run_id,
                observed_at=selected.observed_at,
                source=selected.source,
            )
        )
    missing_sources = tuple(missing)
    ledger_bytes = _build_ledger_bytes(
        legacy_root=root,
        database_identity=database_identity,
        database_sha256=legacy_database_sha256,
        candidates=candidates,
        missing_sources=missing_sources,
        skipped_stale_run_ids=skipped_stale_run_ids,
    )
    ledger_sha256 = hashlib.sha256(ledger_bytes).hexdigest()
    _require_no_sqlite_sidecars(database_path)
    return CacheSeedPlan(
        legacy_root=root,
        database_path=database_path,
        database_identity=database_identity,
        skipped_stale_run_ids=skipped_stale_run_ids,
        run_count=len(runs),
        snapshot_count=snapshot_count,
        source_occurrence_count=len(occurrences),
        missing_source_files=len(missing_sources),
        candidates=candidates,
        missing_sources=missing_sources,
        legacy_database_sha256=legacy_database_sha256,
        ledger_bytes=ledger_bytes,
        ledger_sha256=ledger_sha256,
    )


def _require_unchanged_database(plan: CacheSeedPlan) -> None:
    _require_no_sqlite_sidecars(plan.database_path)
    descriptor = _open_regular(plan.database_path, error_code="invalid_legacy_database")
    try:
        try:
            if (
                _descriptor_identity(descriptor) != plan.database_identity
                or _descriptor_sha256(descriptor) != plan.legacy_database_sha256
                or _descriptor_identity(descriptor) != plan.database_identity
            ):
                raise CacheSeedError("legacy_database_changed")
        except OSError as exc:
            raise CacheSeedError("legacy_database_changed") from exc
    finally:
        os.close(descriptor)
    _require_no_sqlite_sidecars(plan.database_path)


def _ledger_relative_path(plan: CacheSeedPlan) -> str:
    return (_LEDGER_DIRECTORY / f"{plan.ledger_sha256}.json").as_posix()


def _require_ledger_identity(plan: CacheSeedPlan) -> None:
    if (
        len(plan.ledger_bytes) > MAX_LEDGER_BYTES
        or not _SHA256.fullmatch(plan.ledger_sha256)
        or hashlib.sha256(plan.ledger_bytes).hexdigest() != plan.ledger_sha256
    ):
        raise CacheSeedError("invalid_ledger_identity")


def _ensure_ledger_directory(state_dir: Path) -> Path:
    root = _absolute_without_resolving(state_dir)
    if root == Path(root.anchor) or len(str(root)) > 4096:
        raise CacheSeedError("unsafe_audit_path")
    for component in _path_components(root):
        try:
            mode = component.lstat().st_mode
        except FileNotFoundError:
            raise CacheSeedError("unsafe_audit_path") from None
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise CacheSeedError("unsafe_audit_path")
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
            raise CacheSeedError("audit_ledger_write_failed") from exc
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            raise CacheSeedError("unsafe_audit_path") from None
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise CacheSeedError("unsafe_audit_path")
        if created:
            parent_descriptor = _open_ledger_directory(current.parent)
            try:
                os.fsync(parent_descriptor)
            except OSError as exc:
                raise CacheSeedError("audit_ledger_write_failed") from exc
            finally:
                os.close(parent_descriptor)
    return current


def _open_ledger_directory(path: Path) -> int:
    try:
        before = path.lstat()
    except FileNotFoundError:
        raise CacheSeedError("unsafe_audit_path") from None
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
        raise CacheSeedError("unsafe_audit_path")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise CacheSeedError("unsafe_audit_path") from exc
    after = os.fstat(descriptor)
    if not stat.S_ISDIR(after.st_mode) or (before.st_dev, before.st_ino) != (
        after.st_dev,
        after.st_ino,
    ):
        os.close(descriptor)
        raise CacheSeedError("unsafe_audit_path")
    return descriptor


def _existing_ledger_matches(
    directory_descriptor: int,
    file_name: str,
    expected: bytes,
) -> bool:
    try:
        listed = os.stat(file_name, dir_fd=directory_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise CacheSeedError("unsafe_audit_ledger") from exc
    if not stat.S_ISREG(listed.st_mode) or listed.st_nlink != 1:
        raise CacheSeedError("unsafe_audit_ledger")
    if listed.st_size > MAX_LEDGER_BYTES or listed.st_size != len(expected):
        raise CacheSeedError("audit_ledger_conflict")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(file_name, flags, dir_fd=directory_descriptor)
    except OSError as exc:
        raise CacheSeedError("unsafe_audit_ledger") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (listed.st_dev, listed.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise CacheSeedError("unsafe_audit_ledger")
        before = _descriptor_identity(descriptor)
        offset = 0
        while block := os.read(descriptor, 1024 * 1024):
            if expected[offset : offset + len(block)] != block:
                raise CacheSeedError("audit_ledger_conflict")
            offset += len(block)
        if offset != len(expected):
            raise CacheSeedError("audit_ledger_conflict")
        if _descriptor_identity(descriptor) != before:
            raise CacheSeedError("audit_ledger_changed")
    finally:
        os.close(descriptor)
    return True


def _prepare_ledger_destination(plan: CacheSeedPlan, state_dir: Path) -> str:
    _require_ledger_identity(plan)
    directory = _ensure_ledger_directory(state_dir)
    descriptor = _open_ledger_directory(directory)
    try:
        _existing_ledger_matches(
            descriptor,
            f"{plan.ledger_sha256}.json",
            plan.ledger_bytes,
        )
    finally:
        os.close(descriptor)
    return _ledger_relative_path(plan)


def _persist_ledger(plan: CacheSeedPlan, state_dir: Path) -> str:
    _require_ledger_identity(plan)
    directory = _ensure_ledger_directory(state_dir)
    directory_descriptor = _open_ledger_directory(directory)
    file_name = f"{plan.ledger_sha256}.json"
    temporary_name = f".{plan.ledger_sha256}.{uuid.uuid4().hex}.tmp"
    temporary_descriptor = -1
    temporary_created = False
    try:
        if _existing_ledger_matches(directory_descriptor, file_name, plan.ledger_bytes):
            return _ledger_relative_path(plan)
        flags = (
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
        temporary_descriptor = os.open(
            temporary_name,
            flags,
            0o600,
            dir_fd=directory_descriptor,
        )
        temporary_created = True
        view = memoryview(plan.ledger_bytes)
        while view:
            written = os.write(temporary_descriptor, view)
            if written < 1:  # pragma: no cover - regular file write invariant
                raise OSError("ledger write made no progress")
            view = view[written:]
        os.fsync(temporary_descriptor)
        os.close(temporary_descriptor)
        temporary_descriptor = -1
        try:
            os.link(
                temporary_name,
                file_name,
                src_dir_fd=directory_descriptor,
                dst_dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
        except FileExistsError:
            if not _existing_ledger_matches(
                directory_descriptor,
                file_name,
                plan.ledger_bytes,
            ):  # pragma: no cover - false return means absent, impossible after EEXIST
                raise CacheSeedError("audit_ledger_conflict") from None
        os.unlink(temporary_name, dir_fd=directory_descriptor)
        temporary_created = False
        os.fsync(directory_descriptor)
        if not _existing_ledger_matches(
            directory_descriptor,
            file_name,
            plan.ledger_bytes,
        ):  # pragma: no cover - successful link invariant
            raise CacheSeedError("audit_ledger_write_failed")
        return _ledger_relative_path(plan)
    except CacheSeedError:
        raise
    except OSError as exc:
        raise CacheSeedError("audit_ledger_write_failed") from exc
    finally:
        if temporary_descriptor >= 0:
            os.close(temporary_descriptor)
        if temporary_created:
            try:
                os.unlink(temporary_name, dir_fd=directory_descriptor)
                os.fsync(directory_descriptor)
            except OSError:
                pass
        os.close(directory_descriptor)


def apply_cache_seed(plan: CacheSeedPlan, cache: PDFCache) -> dict[str, Any]:
    _require_secure_directory(plan.legacy_root)
    _require_unchanged_database(plan)
    ledger_path = _prepare_ledger_destination(plan, cache.state_dir)
    transition_indexes: list[int] = []
    desired_revisions: dict[str, list[str]] = defaultdict(list)
    last_pdf_by_source: dict[str, str] = {}
    for candidate in plan.candidates:
        source_id = candidate.source.source_id
        if last_pdf_by_source.get(source_id) != candidate.pdf_sha256:
            desired_revisions[source_id].append(candidate.pdf_sha256)
            last_pdf_by_source[source_id] = candidate.pdf_sha256
        transition_indexes.append(len(desired_revisions[source_id]) - 1)

    covered_revisions: dict[str, int] = {}
    for source_id, desired in desired_revisions.items():
        existing = [row.pdf_sha256 for row in cache.state.pdf_cache_source_history(source_id)]
        shared_length = min(len(existing), len(desired))
        if existing[:shared_length] != desired[:shared_length]:
            raise CacheSeedError("destination_history_conflict")
        covered_revisions[source_id] = shared_length

    applied_candidates = 0
    reused_candidates = 0
    created_pdf_objects: set[str] = set()
    created_revisions = 0
    try:
        for candidate, transition_index in zip(plan.candidates, transition_indexes, strict=True):
            object_existed = cache.state.pdf_cache_object(candidate.pdf_sha256) is not None
            if transition_index < covered_revisions[candidate.source.source_id]:
                # Revalidate/repair the CAS object without replaying an already
                # represented revision and disturbing the current binding.
                cache.ingest(
                    candidate.source_path,
                    expected_sha256=candidate.pdf_sha256,
                    expected_size_bytes=candidate.size_bytes,
                    expected_page_count=candidate.page_count,
                )
                reused_candidates += 1
            else:
                history_before = len(cache.state.pdf_cache_source_history(candidate.source.source_id))
                cache.ingest_and_bind(
                    PDFSourceIdentity.from_source_record(candidate.source),
                    candidate.source_path,
                    final_url=candidate.source.source_url,
                    expected_sha256=candidate.pdf_sha256,
                    expected_size_bytes=candidate.size_bytes,
                    expected_page_count=candidate.page_count,
                    observed_at=candidate.observed_at,
                    verified_at=candidate.observed_at,
                )
                history_after = len(cache.state.pdf_cache_source_history(candidate.source.source_id))
                if history_after not in {history_before, history_before + 1}:
                    raise CacheSeedError("cache_apply_failed")
                created_revisions += history_after - history_before
                applied_candidates += 1
            if not object_existed and cache.state.pdf_cache_object(candidate.pdf_sha256) is not None:
                created_pdf_objects.add(candidate.pdf_sha256)
    except CacheSeedError:
        raise
    except Exception as exc:
        raise CacheSeedError("cache_apply_failed") from exc
    _require_unchanged_database(plan)
    persisted_ledger_path = _persist_ledger(plan, cache.state_dir)
    if persisted_ledger_path != ledger_path:  # pragma: no cover - deterministic path invariant
        raise CacheSeedError("audit_ledger_write_failed")
    return plan.report(
        applied=True,
        applied_candidates=applied_candidates,
        reused_candidates=reused_candidates,
        created_pdf_objects=len(created_pdf_objects),
        created_revisions=created_revisions,
        ledger_path=persisted_ledger_path,
    )


def paths_overlap(first: Path, second: Path) -> bool:
    first_absolute = _absolute_without_resolving(first)
    second_absolute = _absolute_without_resolving(second)
    return (
        first_absolute == second_absolute
        or first_absolute.is_relative_to(second_absolute)
        or second_absolute.is_relative_to(first_absolute)
    )
