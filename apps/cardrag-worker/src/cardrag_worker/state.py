"""Durable, resumable finite-run state. This database is never served by MCP."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import struct
import sys
import threading
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast

RunStatus = Literal["running", "succeeded", "failed", "no_change", "interrupted"]
StageStatus = Literal["pending", "running", "retry", "succeeded", "failed", "skipped"]

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_ID = re.compile(r"^source_[0-9a-f]{64}$")
_INTERRUPTED_ERROR = "worker process ended before the run reached a terminal state"
WORKER_STATE_WAL_AUTOCHECKPOINT_PAGES = 1000
WORKER_STATE_SQLITE_PAGE_BYTES = 4096
WORKER_STATE_USE_UNIX_EXCL = sys.platform == "linux"
_ACTIVE_WORKER_STATE_REGISTRY_LOCK = threading.Lock()
_ACTIVE_WORKER_STATE_KEYS: set[tuple[object, ...]] = set()


class AlreadyRunning(RuntimeError):
    pass


class WorkerStateWALCapacityError(RuntimeError):
    """The embedding-cache WAL cannot be kept inside its predicted hard bound."""


def _worker_state_registry_keys(path: Path) -> set[tuple[object, ...]]:
    absolute = os.path.normcase(os.path.abspath(os.fspath(path)))
    keys: set[tuple[object, ...]] = {("path", absolute)}
    try:
        observed = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return keys
    except OSError as exc:
        raise RuntimeError("Worker state file path identity is unavailable") from exc
    keys.add(("inode", observed.st_dev, observed.st_ino))
    return keys


def _reserve_worker_state(path: Path) -> set[tuple[object, ...]]:
    keys = _worker_state_registry_keys(path)
    with _ACTIVE_WORKER_STATE_REGISTRY_LOCK:
        if not _ACTIVE_WORKER_STATE_KEYS.isdisjoint(keys):
            raise RuntimeError("Worker state database is already open in this process")
        _ACTIVE_WORKER_STATE_KEYS.update(keys)
    return keys


def _record_worker_state_identity(
    keys: set[tuple[object, ...]],
    observed: os.stat_result,
) -> None:
    identity = ("inode", observed.st_dev, observed.st_ino)
    with _ACTIVE_WORKER_STATE_REGISTRY_LOCK:
        if identity in _ACTIVE_WORKER_STATE_KEYS and identity not in keys:
            # Do not close the just-opened descriptor on this impossible-under-
            # contract race: close(2) could invalidate the incumbent SQLite
            # connection's POSIX locks. Process exit will reclaim the descriptor.
            raise RuntimeError("Worker state database is already open in this process")
        keys.add(identity)
        _ACTIVE_WORKER_STATE_KEYS.add(identity)


def _release_worker_state(keys: set[tuple[object, ...]]) -> None:
    with _ACTIVE_WORKER_STATE_REGISTRY_LOCK:
        _ACTIVE_WORKER_STATE_KEYS.difference_update(keys)


def _now() -> datetime:
    return datetime.now(UTC)


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _open_nofollow_regular(
    path: Path,
    *,
    create: bool,
    retain_descriptor_in: list[int] | None = None,
) -> tuple[int, os.stat_result]:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise RuntimeError("Worker state requires no-follow file descriptors")
    flags = os.O_RDWR | nofollow | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags | (os.O_CREAT | os.O_EXCL if create else 0), 0o600)
    except FileExistsError:
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise RuntimeError("Worker state file is unavailable or not a regular file") from exc
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise RuntimeError("Worker state file is unavailable or not a regular file") from exc
    if retain_descriptor_in is not None:
        # A close(2) of any descriptor for an inode releases every traditional
        # POSIX record lock this process owns for that inode.  Callers that run
        # after sqlite3.connect() therefore retain even failed verification
        # opens until the SQLite connection itself has closed.
        retain_descriptor_in.append(descriptor)
    try:
        observed = os.fstat(descriptor)
        try:
            linked = os.stat(path, follow_symlinks=False)
        except OSError as exc:
            raise RuntimeError("Worker state file path identity is unavailable") from exc
        if (
            not stat.S_ISREG(observed.st_mode)
            or not stat.S_ISREG(linked.st_mode)
            or (observed.st_dev, observed.st_ino) != (linked.st_dev, linked.st_ino)
        ):
            raise RuntimeError("Worker state file is not a regular file")
        return descriptor, observed
    except BaseException:
        if retain_descriptor_in is None:
            os.close(descriptor)
        raise


def _require_nofollow_regular_identity(
    path: Path,
    retained_descriptors: list[int],
    *,
    expected: tuple[int, int] | None = None,
) -> tuple[int, int]:
    _descriptor, observed = _open_nofollow_regular(
        path,
        create=False,
        retain_descriptor_in=retained_descriptors,
    )
    identity = (observed.st_dev, observed.st_ino)
    if expected is not None and identity != expected:
        raise RuntimeError("Worker state file identity changed while opening SQLite")
    return identity


def _revalidate_sqlite_sidecars(
    paths: Sequence[Path],
    identities: dict[Path, tuple[int, int] | None],
    retained_descriptors: list[int],
    *,
    require_present: bool,
) -> None:
    for path in paths:
        try:
            identity = _require_nofollow_regular_identity(
                path,
                retained_descriptors,
                expected=identities[path],
            )
        except FileNotFoundError:
            if require_present:
                raise RuntimeError("Worker state WAL/SHM file was not created as a regular file") from None
            continue
        identities[path] = identity


@contextmanager
def worker_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, _observed = _open_nofollow_regular(path, create=True)
    with os.fdopen(descriptor, "a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AlreadyRunning(f"another worker owns {path}") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@dataclass(frozen=True, slots=True)
class StageRow:
    run_id: str
    document_id: str
    stage_name: str
    status: str
    attempt_count: int
    max_attempts: int
    available_at: str
    last_error: str | None


@dataclass(frozen=True, slots=True)
class WorkerStateWALObservation:
    exists: bool
    device: int | None
    inode: int | None
    size_bytes: int
    mtime_ns: int | None
    ctime_ns: int | None


@dataclass(frozen=True, slots=True)
class WorkerStateWALCapacitySnapshot:
    baseline_wal_size_bytes: int
    wal_size_bytes: int
    maximum_wal_bytes: int


@dataclass(frozen=True, slots=True)
class PDFCacheObjectRow:
    pdf_sha256: str
    size_bytes: int
    page_count: int
    relative_path: str
    created_at: str
    last_verified_at: str


@dataclass(frozen=True, slots=True)
class PDFSourceRevisionRow:
    revision_id: int
    previous_revision_id: int | None
    source_id: str
    issuer: str
    product_code: str
    document_type: str
    source_url: str
    source_version: str
    source_post_id: str
    discovery_sha256: str
    pdf_sha256: str
    pdf_size_bytes: int
    page_count: int
    relative_path: str
    final_url: str
    etag: str | None
    last_modified: str | None
    source_first_observed_at: str
    source_last_observed_at: str
    revision_first_observed_at: str
    revision_last_observed_at: str
    verified_at: str
    superseded_at: str | None
    superseded_by_source_id: str | None
    source_superseded_at: str | None


@dataclass(frozen=True, slots=True)
class EmbeddingCacheV5Row:
    cache_key: str
    profile_id: str
    input_sha256: str
    dimension: int
    dtype: str
    normalization: str
    embedding: bytes
    created_at: str


SCHEMA = """
CREATE TABLE IF NOT EXISTS run (
  run_id TEXT PRIMARY KEY,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL CHECK(status IN ('running','succeeded','failed','no_change','interrupted')),
  corpus_sha256 TEXT,
  contract_sha256 TEXT,
  error TEXT
) STRICT;

CREATE TABLE IF NOT EXISTS snapshot (
  snapshot_id TEXT NOT NULL,
  run_id TEXT NOT NULL REFERENCES run(run_id),
  issuer TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  source_sha256 TEXT NOT NULL,
  record_count INTEGER NOT NULL CHECK(record_count >= 0),
  payload_json TEXT NOT NULL,
  PRIMARY KEY (run_id, snapshot_id)
) STRICT;
CREATE INDEX IF NOT EXISTS snapshot_run_idx ON snapshot(run_id, issuer);

CREATE TABLE IF NOT EXISTS stage (
  run_id TEXT NOT NULL REFERENCES run(run_id),
  document_id TEXT NOT NULL,
  stage_name TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('pending','running','retry','succeeded','failed','skipped')),
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
  max_attempts INTEGER NOT NULL CHECK(max_attempts > 0),
  available_at TEXT NOT NULL,
  last_error TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (run_id, document_id, stage_name)
) STRICT;
CREATE INDEX IF NOT EXISTS stage_due_idx ON stage(run_id, status, available_at);

CREATE TABLE IF NOT EXISTS checkpoint (
  run_id TEXT NOT NULL REFERENCES run(run_id),
  document_id TEXT NOT NULL,
  stage_name TEXT NOT NULL,
  chunk_index INTEGER NOT NULL CHECK(chunk_index >= 0),
  input_sha256 TEXT NOT NULL,
  output_sha256 TEXT NOT NULL,
  artifact_path TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('succeeded','adopted')),
  updated_at TEXT NOT NULL,
  PRIMARY KEY (run_id, document_id, stage_name, chunk_index)
) STRICT;

CREATE TABLE IF NOT EXISTS publish (
  generation_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES run(run_id),
  corpus_sha256 TEXT NOT NULL,
  contract_sha256 TEXT NOT NULL,
  serving_sha256 TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('ready','failed')),
  published_at TEXT NOT NULL,
  details_json TEXT NOT NULL
) STRICT;
DROP INDEX IF EXISTS publish_identity_ready_idx;
CREATE INDEX IF NOT EXISTS publish_identity_idx
  ON publish(corpus_sha256, contract_sha256, published_at) WHERE status = 'ready';

CREATE TABLE IF NOT EXISTS embedding_cache (
  cache_key TEXT PRIMARY KEY,
  contract_sha256 TEXT NOT NULL,
  text_sha256 TEXT NOT NULL,
  embedding BLOB NOT NULL CHECK(length(embedding)=6144),
  created_at TEXT NOT NULL
) STRICT, WITHOUT ROWID;

-- v5 embeddings are deliberately isolated from the legacy 1,536D cache.  A
-- cache identity binds the complete provider-specific profile as well as the
-- exact formatted input, and the dynamic length CHECK prevents a row from
-- claiming one dimension while storing another.
CREATE TABLE IF NOT EXISTS embedding_cache_v5 (
  cache_key TEXT PRIMARY KEY CHECK(length(cache_key)=64),
  profile_id TEXT NOT NULL CHECK(length(profile_id) BETWEEN 1 AND 512),
  input_sha256 TEXT NOT NULL CHECK(length(input_sha256)=64),
  dimension INTEGER NOT NULL CHECK(dimension > 0),
  dtype TEXT NOT NULL CHECK(dtype='float32'),
  normalization TEXT NOT NULL CHECK(normalization='l2'),
  embedding BLOB NOT NULL CHECK(length(embedding)=dimension*4),
  created_at TEXT NOT NULL
) STRICT, WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS embedding_cache_v5_profile_input_idx
  ON embedding_cache_v5(profile_id,input_sha256);

CREATE TABLE IF NOT EXISTS gc_unreferenced (
  remote_path TEXT PRIMARY KEY,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL
) STRICT, WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS pdf_cache_object (
  pdf_sha256 TEXT PRIMARY KEY CHECK(length(pdf_sha256)=64),
  size_bytes INTEGER NOT NULL CHECK(size_bytes > 0),
  page_count INTEGER NOT NULL CHECK(page_count > 0),
  relative_path TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL,
  last_verified_at TEXT NOT NULL
) STRICT, WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS pdf_cache_source (
  source_id TEXT PRIMARY KEY CHECK(length(source_id)=71),
  issuer TEXT NOT NULL,
  product_code TEXT NOT NULL,
  document_type TEXT NOT NULL,
  source_url TEXT NOT NULL,
  source_version TEXT NOT NULL,
  source_post_id TEXT NOT NULL,
  discovery_sha256 TEXT NOT NULL CHECK(length(discovery_sha256)=64),
  first_observed_at TEXT NOT NULL,
  last_observed_at TEXT NOT NULL,
  last_verified_at TEXT NOT NULL,
  superseded_by_source_id TEXT REFERENCES pdf_cache_source(source_id),
  superseded_at TEXT
) STRICT, WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS pdf_cache_source_product_idx
  ON pdf_cache_source(issuer, product_code, document_type, last_observed_at);
CREATE INDEX IF NOT EXISTS pdf_cache_source_url_idx
  ON pdf_cache_source(source_url, last_observed_at);

CREATE TABLE IF NOT EXISTS pdf_cache_source_revision (
  revision_id INTEGER PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES pdf_cache_source(source_id),
  pdf_sha256 TEXT NOT NULL REFERENCES pdf_cache_object(pdf_sha256),
  pdf_size_bytes INTEGER NOT NULL CHECK(pdf_size_bytes > 0),
  page_count INTEGER NOT NULL CHECK(page_count > 0),
  final_url TEXT NOT NULL,
  etag TEXT,
  last_modified TEXT,
  first_observed_at TEXT NOT NULL,
  last_observed_at TEXT NOT NULL,
  verified_at TEXT NOT NULL,
  superseded_at TEXT,
  previous_revision_id INTEGER REFERENCES pdf_cache_source_revision(revision_id)
) STRICT;
CREATE UNIQUE INDEX IF NOT EXISTS pdf_cache_source_revision_active_idx
  ON pdf_cache_source_revision(source_id) WHERE superseded_at IS NULL;
CREATE INDEX IF NOT EXISTS pdf_cache_source_revision_history_idx
  ON pdf_cache_source_revision(source_id, revision_id);
"""


_RUN_SCHEMA = """
CREATE TABLE run (
  run_id TEXT PRIMARY KEY,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL CHECK(status IN ('running','succeeded','failed','no_change','interrupted')),
  corpus_sha256 TEXT,
  contract_sha256 TEXT,
  error TEXT
) STRICT
"""


def _utc_iso(value: datetime | None = None) -> str:
    timestamp = value or _now()
    if timestamp.tzinfo is None:
        raise ValueError("cache timestamps must be timezone-aware")
    return timestamp.astimezone(UTC).isoformat()


def _encode_embedding_cache_v5(values: Sequence[float], *, dimension: int) -> bytes:
    """Return a finite, L2-normalized little-endian float32 cache value."""

    if isinstance(dimension, bool) or not isinstance(dimension, int) or dimension <= 0:
        raise ValueError("embedding dimension must be a positive integer")
    if len(values) != dimension:
        raise ValueError(f"embedding dimension {len(values)} does not equal {dimension}")
    converted = tuple(float(value) for value in values)
    if not all(math.isfinite(value) for value in converted):
        raise ValueError("embedding contains a non-finite value")
    norm = math.sqrt(sum(value * value for value in converted))
    if not math.isfinite(norm) or norm <= 1e-12:
        raise ValueError("embedding must have a finite non-zero norm")
    packed = struct.pack(f"<{dimension}f", *(value / norm for value in converted))
    stored = struct.unpack(f"<{dimension}f", packed)
    stored_norm_squared = sum(value * value for value in stored)
    if not math.isclose(stored_norm_squared, 1.0, rel_tol=2e-5, abs_tol=2e-5):
        raise ValueError("float32 embedding failed L2 normalization")
    return packed


def _validate_embedding_cache_v5_blob(blob: bytes, *, dimension: int) -> None:
    if len(blob) != dimension * 4:
        raise RuntimeError("v5 embedding cache blob length does not match its dimension")
    values = struct.unpack(f"<{dimension}f", blob)
    if not all(math.isfinite(value) for value in values):
        raise RuntimeError("v5 embedding cache contains a non-finite value")
    norm_squared = sum(value * value for value in values)
    if not math.isclose(norm_squared, 1.0, rel_tol=2e-5, abs_tol=2e-5):
        raise RuntimeError("v5 embedding cache contains a non-normalized vector")


def _required_text(value: str, *, field: str, maximum: int = 4096) -> str:
    if not value or value != value.strip() or len(value) > maximum or "\x00" in value:
        raise ValueError(f"{field} must be non-empty, trimmed, and bounded")
    return value


def _optional_header(value: str | None, *, field: str) -> str | None:
    if value is None:
        return None
    if (
        not value
        or value != value.strip()
        or len(value) > 2048
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"{field} must be a bounded single-line value")
    return value


def _bounded_text(value: str, *, field: str, maximum: int = 4096) -> str:
    if value != value.strip() or len(value) > maximum or "\x00" in value:
        raise ValueError(f"{field} must be trimmed and bounded")
    return value


_PDF_REVISION_SELECT = """
SELECT
  r.revision_id,
  r.previous_revision_id,
  s.source_id,
  s.issuer,
  s.product_code,
  s.document_type,
  s.source_url,
  s.source_version,
  s.source_post_id,
  s.discovery_sha256,
  r.pdf_sha256,
  r.pdf_size_bytes,
  r.page_count,
  o.relative_path,
  r.final_url,
  r.etag,
  r.last_modified,
  s.first_observed_at AS source_first_observed_at,
  s.last_observed_at AS source_last_observed_at,
  r.first_observed_at AS revision_first_observed_at,
  r.last_observed_at AS revision_last_observed_at,
  r.verified_at,
  r.superseded_at,
  s.superseded_by_source_id,
  s.superseded_at AS source_superseded_at
FROM pdf_cache_source_revision r
JOIN pdf_cache_source s ON s.source_id=r.source_id
JOIN pdf_cache_object o ON o.pdf_sha256=r.pdf_sha256
"""


class WorkerState:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        # OCR resolvers sharing this state object coordinate native reuse by
        # current run and exact contract. Values are deliberately runtime-only;
        # document-local sealed artifacts are the durable restart boundary.
        self._ocr_native_run_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._ocr_run_local_manifest_indexes: dict[tuple[str, str], dict[str, tuple[Path, ...]]] = {}
        self._ocr_run_local_manifest_index_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._sqlite_registry_keys = _reserve_worker_state(path)
        try:
            descriptor, sealed_database = _open_nofollow_regular(path, create=True)
            _record_worker_state_identity(self._sqlite_registry_keys, sealed_database)
            os.close(descriptor)
            sidecar_paths = (
                path.with_name(f"{path.name}-wal"),
                path.with_name(f"{path.name}-shm"),
            )
            wal_path, shm_path = sidecar_paths
            sidecar_identities: dict[Path, tuple[int, int] | None] = {}
            for sidecar in sidecar_paths:
                try:
                    sidecar_descriptor, sidecar_stat = _open_nofollow_regular(sidecar, create=False)
                except FileNotFoundError:
                    sidecar_identities[sidecar] = None
                else:
                    os.close(sidecar_descriptor)
                    sidecar_identities[sidecar] = (sidecar_stat.st_dev, sidecar_stat.st_ino)
        except BaseException:
            registry_keys = self._sqlite_registry_keys
            self._sqlite_registry_keys = set()
            _release_worker_state(registry_keys)
            raise
        self._sqlite_verification_descriptors: list[int] = []
        use_unix_excl = WORKER_STATE_USE_UNIX_EXCL
        connection_target = (
            f"{path.absolute().as_uri()}?mode=rw&vfs=unix-excl" if use_unix_excl else os.fspath(path)
        )
        try:
            self.connection = sqlite3.connect(
                connection_target,
                timeout=30,
                isolation_level=None,
                uri=use_unix_excl,
            )
        except BaseException:
            registry_keys = self._sqlite_registry_keys
            self._sqlite_registry_keys = set()
            _release_worker_state(registry_keys)
            raise
        try:
            try:
                _require_nofollow_regular_identity(
                    path,
                    self._sqlite_verification_descriptors,
                    expected=(sealed_database.st_dev, sealed_database.st_ino),
                )
            except (OSError, RuntimeError):
                raise RuntimeError("Worker state database identity is unavailable") from None
            self.connection.row_factory = sqlite3.Row
            self.connection.execute(f"PRAGMA page_size={WORKER_STATE_SQLITE_PAGE_BYTES}")
            page_size = self.connection.execute("PRAGMA page_size").fetchone()
            if page_size is None or int(page_size[0]) != WORKER_STATE_SQLITE_PAGE_BYTES:
                raise RuntimeError("Worker state SQLite page size is not the sealed 4096-byte contract")
            journal_mode = self.connection.execute("PRAGMA journal_mode=WAL").fetchone()
            if journal_mode is None or str(journal_mode[0]).casefold() != "wal":
                raise RuntimeError("Worker state SQLite journal mode could not be sealed as WAL")
            try:
                _revalidate_sqlite_sidecars(
                    sidecar_paths,
                    sidecar_identities,
                    self._sqlite_verification_descriptors,
                    require_present=False,
                )
            except (OSError, RuntimeError):
                raise RuntimeError("Worker state WAL/SHM identity changed while enabling WAL") from None
            wal_checkpoint = self.connection.execute(
                f"PRAGMA wal_autocheckpoint={WORKER_STATE_WAL_AUTOCHECKPOINT_PAGES}"
            ).fetchone()
            if wal_checkpoint is None or int(wal_checkpoint[0]) != WORKER_STATE_WAL_AUTOCHECKPOINT_PAGES:
                raise RuntimeError("Worker state WAL checkpoint policy could not be sealed")
            self.connection.execute("PRAGMA synchronous=FULL")
            self.connection.execute("PRAGMA busy_timeout=30000")
            # Keep foreign-key rewriting disabled while upgrading the v1.0.8 run
            # CHECK constraint.  ``legacy_alter_table`` preserves every existing
            # child table's reference to ``run`` during the table replacement.
            self.connection.execute("PRAGMA foreign_keys=OFF")
            self.connection.executescript(SCHEMA)
            self._migrate_run_status_constraint()
            self.connection.execute("PRAGMA foreign_keys=ON")
            violation = self.connection.execute("PRAGMA foreign_key_check").fetchone()
            if violation is not None:
                raise RuntimeError("worker state failed its foreign-key integrity check")
            try:
                _require_nofollow_regular_identity(
                    path,
                    self._sqlite_verification_descriptors,
                    expected=(sealed_database.st_dev, sealed_database.st_ino),
                )
                _revalidate_sqlite_sidecars(
                    (wal_path,),
                    sidecar_identities,
                    self._sqlite_verification_descriptors,
                    require_present=True,
                )
                _revalidate_sqlite_sidecars(
                    (shm_path,),
                    sidecar_identities,
                    self._sqlite_verification_descriptors,
                    # unix-excl keeps the WAL index in heap memory.  It must
                    # tolerate an identity-checked stale regular SHM left by
                    # the stock unix VFS, but never requires a new SHM file.
                    require_present=(not use_unix_excl or sidecar_identities[shm_path] is not None),
                )
            except (OSError, RuntimeError):
                raise RuntimeError("Worker state SQLite files changed during initialization") from None
        except BaseException:
            with suppress(BaseException):
                self.close()
            # Preserve the initialization failure.  close() only releases
            # verification descriptors after SQLite has closed safely.
            raise

    def _migrate_run_status_constraint(self) -> None:
        row = self.connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type='table' AND name='run'"
        ).fetchone()
        if row is None:
            raise RuntimeError("worker state run table was not created")
        existing_sql = str(row["sql"])
        if "'interrupted'" in existing_sql:
            return
        self.connection.execute("PRAGMA legacy_alter_table=ON")
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            self.connection.execute("ALTER TABLE run RENAME TO run_v108_status_migration")
            self.connection.execute(_RUN_SCHEMA)
            self.connection.execute(
                """INSERT INTO run
                   (run_id,started_at,finished_at,status,corpus_sha256,contract_sha256,error)
                   SELECT run_id,started_at,finished_at,status,corpus_sha256,contract_sha256,error
                   FROM run_v108_status_migration"""
            )
            self.connection.execute("DROP TABLE run_v108_status_migration")
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise
        finally:
            self.connection.execute("PRAGMA legacy_alter_table=OFF")

    def close(self) -> None:
        self.connection.close()
        descriptors = self._sqlite_verification_descriptors
        self._sqlite_verification_descriptors = []
        first_error: OSError | None = None
        for descriptor in descriptors:
            try:
                os.close(descriptor)
            except OSError as exc:
                if first_error is None:
                    first_error = exc
        registry_keys = self._sqlite_registry_keys
        self._sqlite_registry_keys = set()
        _release_worker_state(registry_keys)
        if first_error is not None:
            raise first_error

    def __enter__(self) -> WorkerState:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield self.connection
        except BaseException:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def start_run(self, *, run_id: str | None = None) -> str:
        identifier = run_id or uuid.uuid4().hex
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO run(run_id,started_at,status) VALUES(?,?, 'running')",
                (identifier, _now().isoformat()),
            )
        return identifier

    def mark_stale_running_runs_interrupted(
        self,
        *,
        exclude_run_id: str | None = None,
    ) -> tuple[str, ...]:
        """Atomically terminalize runs left ``running`` by an earlier process.

        The optional exclusion makes the method safe to call after a new run
        has already been allocated.  Returning the affected IDs gives callers
        an auditable result without requiring another racy query.
        """

        now = _now().isoformat()
        with self.transaction() as connection:
            if exclude_run_id is None:
                rows = connection.execute(
                    "SELECT run_id FROM run WHERE status='running' ORDER BY started_at,run_id"
                ).fetchall()
            else:
                rows = connection.execute(
                    """SELECT run_id FROM run
                       WHERE status='running' AND run_id<>? ORDER BY started_at,run_id""",
                    (exclude_run_id,),
                ).fetchall()
            identifiers = tuple(str(row["run_id"]) for row in rows)
            if identifiers:
                connection.executemany(
                    """UPDATE run SET status='interrupted',finished_at=?,error=?
                       WHERE run_id=? AND status='running'""",
                    ((now, _INTERRUPTED_ERROR[:4000], run_id) for run_id in identifiers),
                )
        return identifiers

    def assert_resumable(self, run_id: str) -> None:
        row = self.connection.execute("SELECT status FROM run WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown run: {run_id}")
        if row["status"] in {"succeeded", "no_change"}:
            raise ValueError(f"run {run_id} is already complete")
        now = _now().isoformat()
        with self.transaction() as connection:
            connection.execute(
                "UPDATE run SET status='running', finished_at=NULL, error=NULL WHERE run_id=?", (run_id,)
            )
            # Explicit resume starts one new finite attempt epoch but preserves
            # downloaded files and chunk checkpoints.
            connection.execute(
                """UPDATE stage SET status='retry',attempt_count=0,available_at=?,updated_at=?
                   WHERE run_id=? AND status IN ('failed','retry','running','skipped')""",
                (now, now, run_id),
            )
            # Latest-only serving requires every explicit resume to refresh
            # issuer discovery. Content-addressed download/OCR/embedding
            # checkpoints remain reusable after the fresh snapshot is proven.
            connection.execute(
                """UPDATE stage SET status='retry',attempt_count=0,available_at=?,
                          last_error=NULL,updated_at=?
                   WHERE run_id=? AND stage_name='discovery'""",
                (now, now, run_id),
            )

    def finish_run(
        self,
        run_id: str,
        status: RunStatus,
        *,
        corpus_sha256: str | None = None,
        contract_sha256: str | None = None,
        error: str | None = None,
    ) -> None:
        if status == "running":
            raise ValueError("finish_run cannot leave a run running")
        self.connection.execute(
            "UPDATE run SET status=?,finished_at=?,corpus_sha256=?,contract_sha256=?,error=? WHERE run_id=?",
            (status, _now().isoformat(), corpus_sha256, contract_sha256, error, run_id),
        )

    def finish_run_if_running(
        self,
        run_id: str,
        status: RunStatus,
        *,
        corpus_sha256: str | None = None,
        contract_sha256: str | None = None,
        error: str | None = None,
    ) -> bool:
        """Finish an active run without overwriting an existing terminal truth."""

        if status == "running":
            raise ValueError("finish_run_if_running cannot leave a run running")
        cursor = self.connection.execute(
            """UPDATE run SET status=?,finished_at=?,corpus_sha256=?,contract_sha256=?,error=?
               WHERE run_id=? AND status='running'""",
            (status, _now().isoformat(), corpus_sha256, contract_sha256, error, run_id),
        )
        return cursor.rowcount == 1

    def record_snapshot(
        self,
        *,
        run_id: str,
        snapshot_id: str,
        issuer: str,
        source_sha256: str,
        record_count: int,
        payload: Mapping[str, Any],
    ) -> None:
        self.connection.execute(
            """INSERT OR REPLACE INTO snapshot
               (snapshot_id,run_id,issuer,observed_at,source_sha256,record_count,payload_json)
               VALUES(?,?,?,?,?,?,?)""",
            (
                snapshot_id,
                run_id,
                issuer,
                _now().isoformat(),
                source_sha256,
                record_count,
                json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            ),
        )

    def run_snapshot(self, run_id: str, issuer: str) -> tuple[dict[str, Any], datetime] | None:
        row = self.connection.execute(
            """SELECT payload_json,observed_at FROM snapshot
               WHERE run_id=? AND issuer=? ORDER BY observed_at DESC LIMIT 1""",
            (run_id, issuer),
        ).fetchone()
        if row is None:
            return None
        payload = json.loads(str(row["payload_json"]))
        if not isinstance(payload, dict):
            raise RuntimeError("stored snapshot payload is not a JSON object")
        return payload, datetime.fromisoformat(str(row["observed_at"]))

    def snapshot_history(self, issuer: str) -> tuple[tuple[dict[str, Any], datetime], ...]:
        """Return every canonically bound discovery snapshot for one issuer."""

        _required_text(issuer, field="issuer", maximum=64)
        rows = self.connection.execute(
            """SELECT snapshot_id,source_sha256,observed_at,payload_json FROM snapshot
               WHERE issuer=? ORDER BY observed_at,run_id,snapshot_id""",
            (issuer,),
        ).fetchall()
        history: list[tuple[dict[str, Any], datetime]] = []
        for row in rows:
            raw = str(row["payload_json"])
            try:
                payload = json.loads(
                    raw,
                    parse_constant=_reject_json_constant,
                )
            except (TypeError, ValueError):
                raise RuntimeError("stored snapshot payload is invalid JSON") from None
            if not isinstance(payload, dict):
                raise RuntimeError("stored snapshot payload is not a JSON object")
            canonical = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            if raw != canonical or str(row["snapshot_id"]) != digest or str(row["source_sha256"]) != digest:
                raise RuntimeError("stored snapshot payload is not canonically hash-bound")
            observed_at = datetime.fromisoformat(str(row["observed_at"]))
            if observed_at.tzinfo is None:
                raise RuntimeError("stored snapshot observation is timezone-naive")
            history.append((payload, observed_at))
        return tuple(history)

    def ensure_stage(self, run_id: str, document_id: str, stage_name: str, *, max_attempts: int = 4) -> None:
        now = _now().isoformat()
        self.connection.execute(
            """INSERT OR IGNORE INTO stage
               (run_id,document_id,stage_name,status,attempt_count,max_attempts,available_at,updated_at)
               VALUES(?,?,?,'pending',0,?,?,?)""",
            (run_id, document_id, stage_name, max_attempts, now, now),
        )

    def stage_started(self, run_id: str, document_id: str, stage_name: str) -> int:
        now = _now().isoformat()
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT attempt_count,max_attempts FROM stage WHERE run_id=? AND document_id=? AND stage_name=?",
                (run_id, document_id, stage_name),
            ).fetchone()
            if row is None:
                raise KeyError("stage was not initialized")
            attempt = int(row["attempt_count"]) + 1
            if attempt > int(row["max_attempts"]):
                raise RuntimeError("stage exhausted its finite attempt budget")
            connection.execute(
                """UPDATE stage SET status='running',attempt_count=?,last_error=NULL,updated_at=?
                   WHERE run_id=? AND document_id=? AND stage_name=?""",
                (attempt, now, run_id, document_id, stage_name),
            )
        return attempt

    def stage_succeeded(self, run_id: str, document_id: str, stage_name: str) -> None:
        self.connection.execute(
            """UPDATE stage SET status='succeeded',last_error=NULL,updated_at=?
               WHERE run_id=? AND document_id=? AND stage_name=?""",
            (_now().isoformat(), run_id, document_id, stage_name),
        )

    def stage_skipped(
        self,
        run_id: str,
        document_id: str,
        stage_name: str,
        reason: str,
    ) -> None:
        self.connection.execute(
            """UPDATE stage SET status='skipped',last_error=?,updated_at=?
               WHERE run_id=? AND document_id=? AND stage_name=?""",
            (reason[:4000], _now().isoformat(), run_id, document_id, stage_name),
        )

    def stage_terminal_failed(
        self,
        run_id: str,
        document_id: str,
        stage_name: str,
        error: str,
    ) -> None:
        now = _now().isoformat()
        cursor = self.connection.execute(
            """UPDATE stage SET status='failed',available_at=?,last_error=?,updated_at=?
               WHERE run_id=? AND document_id=? AND stage_name=? AND status='running'""",
            (
                now,
                error[:4000],
                now,
                run_id,
                document_id,
                stage_name,
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("stage was not running")

    def stage_status_count(self, run_id: str, stage_name: str, status: StageStatus) -> int:
        row = self.connection.execute(
            """SELECT count(*) FROM stage
               WHERE run_id=? AND stage_name=? AND status=?""",
            (run_id, stage_name, status),
        ).fetchone()
        return int(row[0])

    def stage_failed(
        self,
        run_id: str,
        document_id: str,
        stage_name: str,
        error: BaseException | str,
        *,
        delay_seconds: float,
    ) -> str:
        row = self.connection.execute(
            "SELECT attempt_count,max_attempts FROM stage WHERE run_id=? AND document_id=? AND stage_name=?",
            (run_id, document_id, stage_name),
        ).fetchone()
        if row is None:
            raise KeyError("stage was not initialized")
        exhausted = int(row["attempt_count"]) >= int(row["max_attempts"])
        status = "failed" if exhausted else "retry"
        available = (_now() + timedelta(seconds=max(0.0, delay_seconds))).isoformat()
        self.connection.execute(
            """UPDATE stage SET status=?,available_at=?,last_error=?,updated_at=?
               WHERE run_id=? AND document_id=? AND stage_name=?""",
            (status, available, str(error)[:4000], _now().isoformat(), run_id, document_id, stage_name),
        )
        return status

    def due_stages(self, run_id: str) -> tuple[StageRow, ...]:
        rows = self.connection.execute(
            """SELECT run_id,document_id,stage_name,status,attempt_count,max_attempts,available_at,last_error
               FROM stage WHERE run_id=? AND status IN ('pending','retry') AND available_at<=?
               ORDER BY document_id,stage_name""",
            (run_id, _now().isoformat()),
        ).fetchall()
        return tuple(StageRow(**dict(row)) for row in rows)

    def get_stage(self, run_id: str, document_id: str, stage_name: str) -> StageRow | None:
        row = self.connection.execute(
            """SELECT run_id,document_id,stage_name,status,attempt_count,max_attempts,available_at,last_error
               FROM stage WHERE run_id=? AND document_id=? AND stage_name=?""",
            (run_id, document_id, stage_name),
        ).fetchone()
        return None if row is None else StageRow(**dict(row))

    def save_checkpoint(
        self,
        *,
        run_id: str,
        document_id: str,
        stage_name: str,
        chunk_index: int,
        input_sha256: str,
        output_sha256: str,
        artifact_path: Path,
        adopted: bool = False,
    ) -> None:
        self.connection.execute(
            """INSERT OR REPLACE INTO checkpoint
               (run_id,document_id,stage_name,chunk_index,input_sha256,output_sha256,artifact_path,status,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                run_id,
                document_id,
                stage_name,
                chunk_index,
                input_sha256,
                output_sha256,
                str(artifact_path),
                "adopted" if adopted else "succeeded",
                _now().isoformat(),
            ),
        )

    def checkpoint(
        self, run_id: str, document_id: str, stage_name: str, chunk_index: int
    ) -> sqlite3.Row | None:
        row = self.connection.execute(
            """SELECT * FROM checkpoint
               WHERE run_id=? AND document_id=? AND stage_name=? AND chunk_index=?""",
            (run_id, document_id, stage_name, chunk_index),
        ).fetchone()
        return cast(sqlite3.Row | None, row)

    def ready_publish(self, corpus_sha256: str, contract_sha256: str) -> sqlite3.Row | None:
        row = self.connection.execute(
            """SELECT * FROM publish
               WHERE corpus_sha256=? AND contract_sha256=? AND status='ready'
               ORDER BY published_at DESC LIMIT 1""",
            (corpus_sha256, contract_sha256),
        ).fetchone()
        return cast(sqlite3.Row | None, row)

    def record_publish(
        self,
        *,
        generation_id: str,
        run_id: str,
        corpus_sha256: str,
        contract_sha256: str,
        serving_sha256: str,
        status: Literal["ready", "failed"],
        details: Mapping[str, Any],
    ) -> None:
        details_json = json.dumps(details, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        identity = (
            run_id,
            corpus_sha256,
            contract_sha256,
            serving_sha256,
            status,
            details_json,
        )
        with self.transaction() as connection:
            existing = connection.execute(
                """SELECT run_id,corpus_sha256,contract_sha256,serving_sha256,status,details_json
                   FROM publish WHERE generation_id=?""",
                (generation_id,),
            ).fetchone()
            if existing is not None:
                persisted = tuple(str(existing[index]) for index in range(6))
                if persisted != identity:
                    raise RuntimeError("generation publication identity conflicts with durable state")
                return
            connection.execute(
                """INSERT INTO publish
                   (generation_id,run_id,corpus_sha256,contract_sha256,serving_sha256,status,published_at,details_json)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (
                    generation_id,
                    run_id,
                    corpus_sha256,
                    contract_sha256,
                    serving_sha256,
                    status,
                    _now().isoformat(),
                    details_json,
                ),
            )

    def last_successful_snapshot_count(self, issuer: str) -> int | None:
        row = self.connection.execute(
            """SELECT s.record_count FROM snapshot s
               JOIN run r ON r.run_id=s.run_id
               WHERE s.issuer=? AND r.status IN ('succeeded','no_change')
               ORDER BY COALESCE(r.finished_at,r.started_at) DESC LIMIT 1""",
            (issuer,),
        ).fetchone()
        return None if row is None else int(row["record_count"])

    def retained_publication_run_ids(self, *, limit: int = 2) -> tuple[str, ...]:
        if limit < 1:
            raise ValueError("publication retention limit must be positive")
        rows = self.connection.execute(
            """SELECT run_id FROM publish WHERE status='ready'
               ORDER BY published_at DESC, generation_id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return tuple(str(row["run_id"]) for row in rows)

    def completed_run_ids(self) -> tuple[str, ...]:
        rows = self.connection.execute(
            "SELECT run_id FROM run WHERE status IN ('succeeded','no_change') ORDER BY started_at"
        ).fetchall()
        return tuple(str(row["run_id"]) for row in rows)

    def prunable_incomplete_run_ids(self, *, keep: int = 2) -> tuple[str, ...]:
        if keep < 1:
            raise ValueError("incomplete run retention must be positive")
        rows = self.connection.execute(
            """SELECT run_id FROM run WHERE status IN ('failed','running','interrupted')
               ORDER BY started_at DESC,run_id DESC LIMIT -1 OFFSET ?""",
            (keep,),
        ).fetchall()
        return tuple(str(row["run_id"]) for row in rows)

    def record_pdf_cache_object(
        self,
        *,
        pdf_sha256: str,
        size_bytes: int,
        page_count: int,
        relative_path: str,
        verified_at: datetime | None = None,
    ) -> PDFCacheObjectRow:
        if not _SHA256.fullmatch(pdf_sha256):
            raise ValueError("PDF cache object sha256 is invalid")
        if size_bytes < 1 or page_count < 1:
            raise ValueError("PDF cache object size and page count must be positive")
        expected_path = f"objects/sha256/{pdf_sha256[:2]}/{pdf_sha256}"
        if relative_path != expected_path:
            raise ValueError("PDF cache object path does not match its content identity")
        timestamp = _utc_iso(verified_at)
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM pdf_cache_object WHERE pdf_sha256=?", (pdf_sha256,)
            ).fetchone()
            if existing is None:
                connection.execute(
                    """INSERT INTO pdf_cache_object
                       (pdf_sha256,size_bytes,page_count,relative_path,created_at,last_verified_at)
                       VALUES(?,?,?,?,?,?)""",
                    (pdf_sha256, size_bytes, page_count, relative_path, timestamp, timestamp),
                )
            else:
                identity = (
                    int(existing["size_bytes"]),
                    int(existing["page_count"]),
                    str(existing["relative_path"]),
                )
                if identity != (size_bytes, page_count, relative_path):
                    raise RuntimeError("PDF cache object identity conflicts with durable state")
                connection.execute(
                    """UPDATE pdf_cache_object
                       SET last_verified_at=CASE
                         WHEN last_verified_at < ? THEN ? ELSE last_verified_at END
                       WHERE pdf_sha256=?""",
                    (timestamp, timestamp, pdf_sha256),
                )
        row = self.connection.execute(
            "SELECT * FROM pdf_cache_object WHERE pdf_sha256=?", (pdf_sha256,)
        ).fetchone()
        if row is None:  # pragma: no cover - guarded by the transaction above
            raise RuntimeError("PDF cache object was not persisted")
        return PDFCacheObjectRow(**dict(row))

    def pdf_cache_object(self, pdf_sha256: str) -> PDFCacheObjectRow | None:
        if not _SHA256.fullmatch(pdf_sha256):
            raise ValueError("PDF cache object sha256 is invalid")
        row = self.connection.execute(
            "SELECT * FROM pdf_cache_object WHERE pdf_sha256=?", (pdf_sha256,)
        ).fetchone()
        return None if row is None else PDFCacheObjectRow(**dict(row))

    def bind_pdf_cache_source(
        self,
        *,
        source_id: str,
        issuer: str,
        product_code: str,
        document_type: str,
        source_url: str,
        source_version: str,
        source_post_id: str,
        discovery_sha256: str,
        pdf_sha256: str,
        pdf_size_bytes: int,
        page_count: int,
        final_url: str,
        etag: str | None = None,
        last_modified: str | None = None,
        replace_validators: bool = False,
        observed_at: datetime | None = None,
        verified_at: datetime | None = None,
    ) -> PDFSourceRevisionRow:
        if not _SOURCE_ID.fullmatch(source_id):
            raise ValueError("PDF cache source_id is invalid")
        if not _SHA256.fullmatch(discovery_sha256) or source_id != f"source_{discovery_sha256}":
            raise ValueError("PDF cache discovery identity does not match source_id")
        if not _SHA256.fullmatch(pdf_sha256):
            raise ValueError("PDF cache PDF sha256 is invalid")
        if pdf_size_bytes < 1 or page_count < 1:
            raise ValueError("PDF cache PDF size and page count must be positive")
        issuer = _required_text(issuer, field="issuer", maximum=64)
        product_code = _required_text(product_code, field="product_code", maximum=512)
        document_type = _required_text(document_type, field="document_type", maximum=128)
        source_version = _required_text(source_version, field="source_version", maximum=512)
        source_post_id = _bounded_text(source_post_id, field="source_post_id", maximum=512)
        source_url = _required_text(source_url, field="source_url")
        final_url = _required_text(final_url, field="final_url")
        if not source_url.startswith("https://") or not final_url.startswith("https://"):
            raise ValueError("PDF cache source URLs must use HTTPS")
        etag = _optional_header(etag, field="etag")
        last_modified = _optional_header(last_modified, field="last_modified")
        observed = _utc_iso(observed_at)
        verified = _utc_iso(verified_at)
        static_identity = (
            issuer,
            product_code,
            document_type,
            source_url,
            source_version,
            source_post_id,
            discovery_sha256,
        )

        with self.transaction() as connection:
            cache_object = connection.execute(
                "SELECT size_bytes,page_count FROM pdf_cache_object WHERE pdf_sha256=?",
                (pdf_sha256,),
            ).fetchone()
            if cache_object is None:
                raise KeyError("PDF cache object must be recorded before source binding")
            if (int(cache_object["size_bytes"]), int(cache_object["page_count"])) != (
                pdf_size_bytes,
                page_count,
            ):
                raise RuntimeError("PDF source binding conflicts with its cache object")

            source = connection.execute(
                "SELECT * FROM pdf_cache_source WHERE source_id=?", (source_id,)
            ).fetchone()
            if source is None:
                connection.execute(
                    """INSERT INTO pdf_cache_source
                       (source_id,issuer,product_code,document_type,source_url,source_version,
                        source_post_id,discovery_sha256,first_observed_at,last_observed_at,
                        last_verified_at,superseded_by_source_id,superseded_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL,NULL)""",
                    (
                        source_id,
                        *static_identity,
                        observed,
                        observed,
                        verified,
                    ),
                )
            else:
                persisted_identity = tuple(
                    str(source[field])
                    for field in (
                        "issuer",
                        "product_code",
                        "document_type",
                        "source_url",
                        "source_version",
                        "source_post_id",
                        "discovery_sha256",
                    )
                )
                if persisted_identity != static_identity:
                    raise RuntimeError("PDF cache source identity conflicts with durable state")

            current = connection.execute(
                """SELECT * FROM pdf_cache_source_revision
                   WHERE source_id=? AND superseded_at IS NULL""",
                (source_id,),
            ).fetchone()
            if current is not None and str(current["pdf_sha256"]) == pdf_sha256:
                if (int(current["pdf_size_bytes"]), int(current["page_count"])) != (
                    pdf_size_bytes,
                    page_count,
                ):
                    raise RuntimeError("PDF cache revision identity conflicts with durable state")
                revision_id = int(current["revision_id"])
                if replace_validators:
                    connection.execute(
                        """UPDATE pdf_cache_source_revision
                           SET final_url=?,etag=?,last_modified=?,
                               last_observed_at=CASE
                                 WHEN last_observed_at < ? THEN ? ELSE last_observed_at END,
                               verified_at=CASE WHEN verified_at < ? THEN ? ELSE verified_at END
                           WHERE revision_id=?""",
                        (
                            final_url,
                            etag,
                            last_modified,
                            observed,
                            observed,
                            verified,
                            verified,
                            revision_id,
                        ),
                    )
                else:
                    connection.execute(
                        """UPDATE pdf_cache_source_revision
                           SET final_url=?,etag=COALESCE(?,etag),
                               last_modified=COALESCE(?,last_modified),
                               last_observed_at=CASE
                                 WHEN last_observed_at < ? THEN ? ELSE last_observed_at END,
                               verified_at=CASE WHEN verified_at < ? THEN ? ELSE verified_at END
                           WHERE revision_id=?""",
                        (
                            final_url,
                            etag,
                            last_modified,
                            observed,
                            observed,
                            verified,
                            verified,
                            revision_id,
                        ),
                    )
            else:
                previous_revision_id = None if current is None else int(current["revision_id"])
                if current is not None:
                    connection.execute(
                        """UPDATE pdf_cache_source_revision SET superseded_at=?
                           WHERE revision_id=? AND superseded_at IS NULL""",
                        (observed, previous_revision_id),
                    )
                cursor = connection.execute(
                    """INSERT INTO pdf_cache_source_revision
                       (source_id,pdf_sha256,pdf_size_bytes,page_count,final_url,etag,last_modified,
                        first_observed_at,last_observed_at,verified_at,superseded_at,
                        previous_revision_id)
                       VALUES(?,?,?,?,?,?,?,?,?,?,NULL,?)""",
                    (
                        source_id,
                        pdf_sha256,
                        pdf_size_bytes,
                        page_count,
                        final_url,
                        etag,
                        last_modified,
                        observed,
                        observed,
                        verified,
                        previous_revision_id,
                    ),
                )
                if cursor.lastrowid is None:  # pragma: no cover - SQLite INSERT invariant
                    raise RuntimeError("PDF cache revision did not receive an identity")
                revision_id = cursor.lastrowid

            # A newly observed discovery identity supersedes an older source
            # for the same logical product/document.  The older row and all of
            # its byte revisions remain available as history and reusable CAS.
            connection.execute(
                """UPDATE pdf_cache_source
                   SET superseded_by_source_id=?,superseded_at=?
                   WHERE issuer=? AND product_code=? AND document_type=?
                     AND source_id<>? AND superseded_at IS NULL""",
                (source_id, observed, issuer, product_code, document_type, source_id),
            )
            connection.execute(
                """UPDATE pdf_cache_source
                   SET last_observed_at=CASE
                         WHEN last_observed_at < ? THEN ? ELSE last_observed_at END,
                       last_verified_at=CASE
                         WHEN last_verified_at < ? THEN ? ELSE last_verified_at END,
                       superseded_by_source_id=NULL,superseded_at=NULL
                   WHERE source_id=?""",
                (observed, observed, verified, verified, source_id),
            )
            connection.execute(
                """UPDATE pdf_cache_object
                   SET last_verified_at=CASE
                     WHEN last_verified_at < ? THEN ? ELSE last_verified_at END
                   WHERE pdf_sha256=?""",
                (verified, verified, pdf_sha256),
            )

        binding = self.pdf_cache_source_binding(source_id)
        if binding is None or binding.revision_id != revision_id:  # pragma: no cover
            raise RuntimeError("PDF cache source binding was not persisted")
        return binding

    def pdf_cache_source_binding(self, source_id: str) -> PDFSourceRevisionRow | None:
        if not _SOURCE_ID.fullmatch(source_id):
            raise ValueError("PDF cache source_id is invalid")
        row = self.connection.execute(
            _PDF_REVISION_SELECT
            + " WHERE s.source_id=? AND r.superseded_at IS NULL ORDER BY r.revision_id DESC LIMIT 1",
            (source_id,),
        ).fetchone()
        return None if row is None else PDFSourceRevisionRow(**dict(row))

    def pdf_cache_source_history(self, source_id: str) -> tuple[PDFSourceRevisionRow, ...]:
        if not _SOURCE_ID.fullmatch(source_id):
            raise ValueError("PDF cache source_id is invalid")
        rows = self.connection.execute(
            _PDF_REVISION_SELECT + " WHERE s.source_id=? ORDER BY r.revision_id",
            (source_id,),
        ).fetchall()
        return tuple(PDFSourceRevisionRow(**dict(row)) for row in rows)

    def pdf_cache_lineage_history(
        self,
        *,
        issuer: str,
        product_code: str,
        document_type: str,
    ) -> tuple[PDFSourceRevisionRow, ...]:
        """Return durable byte revisions for one exact product lineage."""

        issuer = _required_text(issuer, field="issuer", maximum=64)
        product_code = _required_text(product_code, field="product_code", maximum=512)
        document_type = _required_text(document_type, field="document_type", maximum=128)
        rows = self.connection.execute(
            _PDF_REVISION_SELECT
            + """ WHERE s.issuer=? AND s.product_code=? AND s.document_type=?
                  ORDER BY s.first_observed_at,r.revision_id""",
            (issuer, product_code, document_type),
        ).fetchall()
        return tuple(PDFSourceRevisionRow(**dict(row)) for row in rows)

    def mark_pdf_cache_verified(
        self,
        *,
        source_id: str,
        pdf_sha256: str,
        verified_at: datetime | None = None,
    ) -> bool:
        if not _SOURCE_ID.fullmatch(source_id) or not _SHA256.fullmatch(pdf_sha256):
            raise ValueError("PDF cache verification identity is invalid")
        timestamp = _utc_iso(verified_at)
        with self.transaction() as connection:
            revision = connection.execute(
                """SELECT revision_id FROM pdf_cache_source_revision
                   WHERE source_id=? AND pdf_sha256=? AND superseded_at IS NULL""",
                (source_id, pdf_sha256),
            ).fetchone()
            if revision is None:
                return False
            connection.execute(
                "UPDATE pdf_cache_source_revision SET verified_at=? WHERE revision_id=?",
                (timestamp, int(revision["revision_id"])),
            )
            connection.execute(
                "UPDATE pdf_cache_source SET last_verified_at=? WHERE source_id=?",
                (timestamp, source_id),
            )
            connection.execute(
                "UPDATE pdf_cache_object SET last_verified_at=? WHERE pdf_sha256=?",
                (timestamp, pdf_sha256),
            )
        return True

    def get_embedding(self, cache_key: str) -> bytes | None:
        row = self.connection.execute(
            "SELECT embedding FROM embedding_cache WHERE cache_key=?", (cache_key,)
        ).fetchone()
        return None if row is None else bytes(row["embedding"])

    def put_embedding(
        self,
        *,
        cache_key: str,
        contract_sha256: str,
        text_sha256: str,
        embedding: bytes,
    ) -> None:
        self.connection.execute(
            """INSERT OR IGNORE INTO embedding_cache
               (cache_key,contract_sha256,text_sha256,embedding,created_at) VALUES(?,?,?,?,?)""",
            (cache_key, contract_sha256, text_sha256, embedding, _now().isoformat()),
        )

    def get_embedding_v5(
        self,
        cache_key: str,
        *,
        profile_id: str,
        input_sha256: str,
        dimension: int,
        dtype: str = "float32",
        normalization: str = "l2",
    ) -> EmbeddingCacheV5Row | None:
        """Read only the profile-bound v5 cache; the legacy table is never consulted."""

        if not _SHA256.fullmatch(cache_key) or not _SHA256.fullmatch(input_sha256):
            raise ValueError("v5 embedding cache identities must be lowercase sha256 values")
        _required_text(profile_id, field="profile_id", maximum=512)
        if isinstance(dimension, bool) or not isinstance(dimension, int) or dimension <= 0:
            raise ValueError("embedding dimension must be a positive integer")
        if dtype != "float32" or normalization != "l2":
            raise ValueError("v5 embedding cache supports only float32 L2 vectors")
        row = self.connection.execute(
            """SELECT cache_key,profile_id,input_sha256,dimension,dtype,normalization,
                      embedding,created_at
               FROM embedding_cache_v5 WHERE cache_key=?""",
            (cache_key,),
        ).fetchone()
        if row is None:
            return None
        cached = EmbeddingCacheV5Row(**dict(row))
        expected = (profile_id, input_sha256, dimension, dtype, normalization)
        actual = (
            cached.profile_id,
            cached.input_sha256,
            cached.dimension,
            cached.dtype,
            cached.normalization,
        )
        if actual != expected:
            raise RuntimeError("v5 embedding cache key is bound to a different profile or input")
        _validate_embedding_cache_v5_blob(cached.embedding, dimension=cached.dimension)
        return cached

    def observe_embedding_cache_v5_wal(self) -> WorkerStateWALObservation:
        """Seal the current regular WAL identity and size without checkpointing it."""

        wal_path = self.path.with_name(f"{self.path.name}-wal")
        try:
            wal_stat = os.stat(wal_path, follow_symlinks=False)
        except FileNotFoundError:
            return WorkerStateWALObservation(
                exists=False,
                device=None,
                inode=None,
                size_bytes=0,
                mtime_ns=None,
                ctime_ns=None,
            )
        except OSError as exc:
            raise WorkerStateWALCapacityError("Worker state WAL size is unavailable") from exc
        if not stat.S_ISREG(wal_stat.st_mode):
            raise WorkerStateWALCapacityError("Worker state WAL is not a regular file")
        if wal_stat.st_size < 0 or wal_stat.st_size > (1 << 63) - 1:
            raise WorkerStateWALCapacityError("Worker state WAL size is outside the supported range")
        return WorkerStateWALObservation(
            exists=True,
            device=wal_stat.st_dev,
            inode=wal_stat.st_ino,
            size_bytes=wal_stat.st_size,
            mtime_ns=wal_stat.st_mtime_ns,
            ctime_ns=wal_stat.st_ctime_ns,
        )

    def check_embedding_cache_v5_wal_capacity(
        self,
        *,
        baseline: WorkerStateWALObservation,
        maximum_wal_growth_bytes: int,
    ) -> WorkerStateWALCapacitySnapshot:
        """Read-only hard bound for the WAL around paid v5 embedding batches.

        Auto-checkpoint is a trigger rather than a size cap when another reader
        pins old frames. The capacity model separately reserves one baseline
        WAL allocation for a possible automatic checkpoint into the main DB;
        this method never induces that mutation itself.
        """

        if not isinstance(baseline, WorkerStateWALObservation):
            raise TypeError("Worker state WAL baseline must be a sealed observation")
        if (
            type(maximum_wal_growth_bytes) is not int
            or maximum_wal_growth_bytes < 1
            or maximum_wal_growth_bytes > (1 << 63) - 1
        ):
            raise ValueError("maximum Worker state WAL growth must be a positive bounded integer")
        if maximum_wal_growth_bytes > (1 << 63) - 1 - baseline.size_bytes:
            raise WorkerStateWALCapacityError("Worker state WAL hard limit overflows")
        maximum_wal_bytes = baseline.size_bytes + maximum_wal_growth_bytes

        def require_same_wal_identity(observed: WorkerStateWALObservation) -> None:
            if baseline.exists and (
                not observed.exists or (observed.device, observed.inode) != (baseline.device, baseline.inode)
            ):
                raise WorkerStateWALCapacityError("Worker state WAL identity changed after preflight")

        before_checkpoint = self.observe_embedding_cache_v5_wal()
        require_same_wal_identity(before_checkpoint)
        if before_checkpoint.size_bytes > maximum_wal_bytes:
            raise WorkerStateWALCapacityError("Worker state WAL exceeds its predicted hard limit")

        try:
            page_size = self.connection.execute("PRAGMA page_size").fetchone()
            autocheckpoint = self.connection.execute("PRAGMA wal_autocheckpoint").fetchone()
            if page_size is None or int(page_size[0]) != WORKER_STATE_SQLITE_PAGE_BYTES:
                raise WorkerStateWALCapacityError("Worker state SQLite page-size contract changed")
            if autocheckpoint is None or int(autocheckpoint[0]) != WORKER_STATE_WAL_AUTOCHECKPOINT_PAGES:
                raise WorkerStateWALCapacityError("Worker state WAL auto-checkpoint contract changed")
        except WorkerStateWALCapacityError:
            raise
        except sqlite3.Error as exc:
            raise WorkerStateWALCapacityError("Worker state WAL capacity evidence failed") from exc

        observed = self.observe_embedding_cache_v5_wal()
        require_same_wal_identity(observed)
        if observed.size_bytes > maximum_wal_bytes:
            raise WorkerStateWALCapacityError("Worker state WAL exceeds its predicted hard limit")

        return WorkerStateWALCapacitySnapshot(
            baseline_wal_size_bytes=baseline.size_bytes,
            wal_size_bytes=observed.size_bytes,
            maximum_wal_bytes=maximum_wal_bytes,
        )

    def put_embedding_v5(
        self,
        *,
        cache_key: str,
        profile_id: str,
        input_sha256: str,
        dimension: int,
        values: Sequence[float],
        dtype: str = "float32",
        normalization: str = "l2",
    ) -> EmbeddingCacheV5Row:
        """Normalize and store a profile-specific v5 vector without touching v4 state."""

        if not _SHA256.fullmatch(cache_key) or not _SHA256.fullmatch(input_sha256):
            raise ValueError("v5 embedding cache identities must be lowercase sha256 values")
        _required_text(profile_id, field="profile_id", maximum=512)
        if dtype != "float32" or normalization != "l2":
            raise ValueError("v5 embedding cache supports only float32 L2 vectors")
        embedding = _encode_embedding_cache_v5(values, dimension=dimension)
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO embedding_cache_v5
                   (cache_key,profile_id,input_sha256,dimension,dtype,normalization,
                    embedding,created_at)
                   VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(cache_key) DO NOTHING""",
                (
                    cache_key,
                    profile_id,
                    input_sha256,
                    dimension,
                    dtype,
                    normalization,
                    embedding,
                    _now().isoformat(),
                ),
            )
        cached = self.get_embedding_v5(
            cache_key,
            profile_id=profile_id,
            input_sha256=input_sha256,
            dimension=dimension,
            dtype=dtype,
            normalization=normalization,
        )
        if cached is None:  # pragma: no cover - SQLite acknowledged the row.
            raise RuntimeError("v5 embedding cache write did not persist")
        if cached.embedding != embedding:
            raise RuntimeError("v5 embedding cache key collision contains different vector bytes")
        return cached

    def note_unreferenced(self, remote_path: str, *, observed_at: datetime | None = None) -> datetime:
        now = (observed_at or _now()).astimezone(UTC)
        self.connection.execute(
            """INSERT INTO gc_unreferenced(remote_path,first_seen_at,last_seen_at) VALUES(?,?,?)
               ON CONFLICT(remote_path) DO UPDATE SET last_seen_at=excluded.last_seen_at""",
            (remote_path, now.isoformat(), now.isoformat()),
        )
        value = self.connection.execute(
            "SELECT first_seen_at FROM gc_unreferenced WHERE remote_path=?", (remote_path,)
        ).fetchone()[0]
        return datetime.fromisoformat(str(value))

    def clear_unreferenced_except(self, paths: set[str]) -> None:
        existing = {str(row[0]) for row in self.connection.execute("SELECT remote_path FROM gc_unreferenced")}
        self.connection.executemany(
            "DELETE FROM gc_unreferenced WHERE remote_path=?",
            ((path,) for path in sorted(existing.difference(paths))),
        )

    def clear_unreferenced(self, remote_path: str) -> None:
        self.connection.execute("DELETE FROM gc_unreferenced WHERE remote_path=?", (remote_path,))


def retry_delay(attempt: int, *, base_seconds: float = 1.0, cap_seconds: float = 30.0) -> float:
    if attempt < 1:
        raise ValueError("attempt is one-based")
    return float(min(cap_seconds, base_seconds * (2 ** (attempt - 1))))
