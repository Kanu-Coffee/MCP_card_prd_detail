"""Fail-closed v1.0.28 embedding-cache seed recovery for the v1.0.29 candidate.

This module is the only sanctioned path that may move ``embedding_cache_v5``
rows between worker state databases.  The FIX_01 candidate volume received its
embedding cache through an undocumented out-of-band table copy that bypassed
the Worker lock, WAL identity checks, and the hash-bound ledger pattern used
by :mod:`cardrag_worker.state_seed_v122`.  This module closes that gap:

* the source database is opened read-only (``mode=ro&immutable=1`` through a
  verified file descriptor) and never written;
* every row is validated per-row for ``profile_id``, ``input_sha256``,
  ``dimension``, ``dtype``, ``normalization``, embedding blob length, finite
  values, and the L2 norm contract shared with :mod:`cardrag_worker.state`;
* the plan seals a content-addressed Merkle root over canonical per-row
  digests plus a per-profile rollup into one ledger file committed below
  destination ``audit-reports/embedding-seed/`` only after every row applied;
* re-apply is idempotent (zero imported rows); any destination row that
  differs from the plan for the same ``cache_key`` fails closed;
* source identity (device/inode, size, SHA-256) is re-verified before and
  during the apply so a mutating source aborts.

Embedding row provenance is recorded by the ledger, not by rewriting
``created_at``: imported rows keep their source timestamps and the ledger
binds them to the exact source database digest.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import uuid
from collections.abc import Iterator
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from cardrag_core.canonical import canonical_json_bytes, canonical_sha256

from .state import WorkerState, _validate_embedding_cache_v5_blob
from .state_seed_v122 import (
    MAX_DATABASE_BYTES,
    StateSeedError,
    _absolute_without_resolving,
    _ensure_destination_dir,
    _hash_descriptor,
    _open_regular,
    paths_overlap,
)

__all__ = [
    "EmbeddingSeedPlan",
    "EmbeddingSeedRow",
    "StateSeedError",
    "apply_embedding_cache_seed_v122",
    "build_embedding_cache_seed_v122_plan",
    "load_embedding_seed_ledger_v122",
]

EMBEDDING_LEDGER_SCHEMA_VERSION = "cardrag.embedding-seed-ledger.v1"
EMBEDDING_REPORT_SCHEMA_VERSION = "cardrag.embedding-seed-report.v1"

MAX_EMBEDDING_ROWS = 1_048_576
MAX_EMBEDDING_DIMENSION = 16_384
EMBEDDING_LEDGER_CAP_BYTES = 16 * 1024 * 1024
_EMBEDDING_BATCH_ROWS = 2_000

_EMBEDDING_LEDGER_DIRECTORY = Path("audit-reports/embedding-seed")
_DATABASE_NAME = "worker-state.sqlite3"
_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PROFILE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._+-]{0,511}$")


@dataclass(frozen=True, slots=True)
class EmbeddingSeedRow:
    """Validated source row metadata; the embedding blob itself is never held."""

    cache_key: str
    profile_id: str
    input_sha256: str
    dimension: int
    dtype: str
    normalization: str
    embedding_sha256: str
    created_at: str
    row_sha256: str


@dataclass(frozen=True, slots=True)
class EmbeddingSeedPlan:
    source_root: Path
    source_database_sha256: str
    source_database_size_bytes: int
    source_database_identity: tuple[int, int]
    rows: tuple[EmbeddingSeedRow, ...]
    row_root_sha256: str
    profile_rollup: dict[str, dict[str, Any]]
    ledger_bytes: bytes
    ledger_sha256: str

    def report(self, *, applied: bool, ledger_path: str | None = None) -> dict[str, Any]:
        return {
            "applied": applied,
            "dry_run": not applied,
            "imported_rows": 0 if applied else None,
            "ledger_path": ledger_path,
            "ledger_sha256": self.ledger_sha256,
            "ledger_size_bytes": len(self.ledger_bytes),
            "profile_rollup": self.profile_rollup,
            "reused_rows": 0 if applied else None,
            "row_count": len(self.rows),
            "row_root_sha256": self.row_root_sha256,
            "schema_version": EMBEDDING_REPORT_SCHEMA_VERSION,
            "source_database_sha256": self.source_database_sha256,
            "status": "applied" if applied else "dry_run",
        }


def _canonical_row_sha256(row_values: dict[str, Any]) -> str:
    return canonical_sha256({**row_values, "schema_version": "cardrag.embedding-seed-row.v1"})


def _row_leaf_digest(row: EmbeddingSeedRow) -> str:
    return hashlib.sha256(row.row_sha256.encode("ascii")).hexdigest()


def _merkle_root_hex(leaf_digests: list[str]) -> str:
    if not leaf_digests:
        return hashlib.sha256(b"cardrag.embedding-seed-row-root.v1:empty").hexdigest()
    level = [bytes.fromhex(digest) for digest in leaf_digests]
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        level = [
            hashlib.sha256(left + right).digest()
            for left, right in zip(level[0::2], level[1::2], strict=True)
        ]
    return level[0].hex()


def _validate_source_row(raw: sqlite3.Row) -> EmbeddingSeedRow:
    cache_key = str(raw["cache_key"])
    profile_id = str(raw["profile_id"])
    input_sha256 = str(raw["input_sha256"])
    dtype = str(raw["dtype"])
    normalization = str(raw["normalization"])
    created_at = str(raw["created_at"])
    dimension_value = raw["dimension"]
    embedding = raw["embedding"]
    if not _SHA256.fullmatch(cache_key) or not _SHA256.fullmatch(input_sha256):
        raise StateSeedError("source_embedding_row_identity_invalid")
    if not _PROFILE_ID.fullmatch(profile_id):
        raise StateSeedError("source_embedding_row_profile_invalid")
    if isinstance(dimension_value, bool) or not isinstance(dimension_value, int):
        raise StateSeedError("source_embedding_row_dimension_invalid")
    if not 0 < dimension_value <= MAX_EMBEDDING_DIMENSION:
        raise StateSeedError("source_embedding_row_dimension_invalid")
    if dtype != "float32" or normalization != "l2":
        raise StateSeedError("source_embedding_row_contract_invalid")
    if not isinstance(embedding, bytes) or len(embedding) != dimension_value * 4:
        raise StateSeedError("source_embedding_row_length_invalid")
    try:
        _validate_embedding_cache_v5_blob(embedding, dimension=dimension_value)
    except RuntimeError as exc:
        raise StateSeedError("source_embedding_row_norm_invalid") from exc
    try:
        parsed_created = datetime.fromisoformat(created_at)
    except ValueError as exc:
        raise StateSeedError("source_embedding_row_timestamp_invalid") from exc
    if parsed_created.tzinfo is None:
        raise StateSeedError("source_embedding_row_timestamp_invalid")
    embedding_sha256 = hashlib.sha256(embedding).hexdigest()
    return EmbeddingSeedRow(
        cache_key=cache_key,
        profile_id=profile_id,
        input_sha256=input_sha256,
        dimension=dimension_value,
        dtype=dtype,
        normalization=normalization,
        embedding_sha256=embedding_sha256,
        created_at=created_at,
        row_sha256=_canonical_row_sha256(
            {
                "cache_key": cache_key,
                "created_at": created_at,
                "dimension": dimension_value,
                "dtype": dtype,
                "embedding_sha256": embedding_sha256,
                "input_sha256": input_sha256,
                "normalization": normalization,
                "profile_id": profile_id,
            }
        ),
    )


def _open_source_database(root: Path) -> tuple[int, sqlite3.Connection]:
    database_path = root / _DATABASE_NAME
    for suffix in _SIDECAR_SUFFIXES:
        sidecar = Path(f"{database_path}{suffix}")
        try:
            sidecar.lstat()
        except FileNotFoundError:
            continue
        raise StateSeedError("source_has_sidecars")
    descriptor = _open_regular(database_path, code="source_database_missing_or_unsafe")
    try:
        identity = os.fstat(descriptor)
    except OSError as exc:
        os.close(descriptor)
        raise StateSeedError("source_database_missing_or_unsafe") from exc
    if identity.st_size <= 0 or identity.st_size > MAX_DATABASE_BYTES:
        os.close(descriptor)
        raise StateSeedError("source_database_size_invalid")
    try:
        connection: sqlite3.Connection = sqlite3.connect(
            f"file:/proc/self/fd/{descriptor}?mode=ro&immutable=1",
            uri=True,
            timeout=10,
        )
    except sqlite3.Error as exc:
        os.close(descriptor)
        raise StateSeedError("source_database_open_failed") from exc
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA trusted_schema=OFF")
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or str(integrity[0]) != "ok":
            raise StateSeedError("source_database_integrity_failed")
        active = connection.execute("SELECT count(*) FROM run WHERE status='running'").fetchone()
        if active is not None and int(active[0]) > 0:
            raise StateSeedError("source_run_active")
    except StateSeedError:
        connection.close()
        os.close(descriptor)
        raise
    return descriptor, connection


def _iter_validated_rows(connection: sqlite3.Connection) -> Iterator[EmbeddingSeedRow]:
    seen: str | None = None
    try:
        cursor = connection.execute(
            """
            SELECT cache_key, profile_id, input_sha256, dimension, dtype, normalization,
                   embedding, created_at
            FROM embedding_cache_v5 ORDER BY cache_key
            """
        )
    except sqlite3.Error as exc:
        raise StateSeedError("source_embedding_table_missing") from exc
    for count, raw in enumerate(cursor, start=1):
        if count > MAX_EMBEDDING_ROWS:
            raise StateSeedError("source_embedding_row_limit_exceeded")
        row = _validate_source_row(raw)
        if seen is not None and row.cache_key <= seen:
            raise StateSeedError("source_embedding_row_order_invalid")
        seen = row.cache_key
        yield row


def _profile_rollup(
    rows: Iterator[EmbeddingSeedRow],
) -> tuple[list[EmbeddingSeedRow], dict[str, dict[str, Any]]]:
    collected: list[EmbeddingSeedRow] = []
    rollup: dict[str, dict[str, Any]] = {}
    for row in rows:
        collected.append(row)
        entry = rollup.setdefault(
            row.profile_id,
            {
                "dimensions": {row.dimension},
                "max_created_at": row.created_at,
                "min_created_at": row.created_at,
                "row_count": 0,
            },
        )
        entry["row_count"] += 1
        entry["dimensions"].add(row.dimension)
        entry["min_created_at"] = min(entry["min_created_at"], row.created_at)
        entry["max_created_at"] = max(entry["max_created_at"], row.created_at)
    return collected, rollup


def build_embedding_cache_seed_v122_plan(
    source_root: Path,
    *,
    expected_rows: int | None = None,
) -> EmbeddingSeedPlan:
    """Stream-verify the source ``embedding_cache_v5`` table and seal a plan."""

    if expected_rows is not None and (
        isinstance(expected_rows, bool) or not isinstance(expected_rows, int) or expected_rows < 0
    ):
        raise StateSeedError("expected_rows_invalid")
    root = _absolute_without_resolving(source_root)
    if not source_root.is_absolute() or root == Path(root.anchor) or len(os.fspath(root)) > 4096:
        raise StateSeedError("unsafe_source_path")
    descriptor, connection = _open_source_database(root)
    try:
        identity = os.fstat(descriptor)
        database_sha256 = _hash_descriptor(descriptor)
        rows, rollup = _profile_rollup(_iter_validated_rows(connection))
        os.lseek(descriptor, 0, os.SEEK_SET)
        if _hash_descriptor(descriptor) != database_sha256:
            raise StateSeedError("source_database_changed")
    finally:
        connection.close()
        os.close(descriptor)

    if expected_rows is not None and len(rows) != expected_rows:
        raise StateSeedError("expected_rows_mismatch")

    row_root = _merkle_root_hex([_row_leaf_digest(row) for row in rows])
    ledger_payload: dict[str, Any] = {
        "profile_rollup": {
            profile_id: {
                "dimensions": sorted(entry["dimensions"]),
                "max_created_at": entry["max_created_at"],
                "min_created_at": entry["min_created_at"],
                "row_count": entry["row_count"],
            }
            for profile_id, entry in sorted(rollup.items())
        },
        "row_count": len(rows),
        "row_root_sha256": row_root,
        "schema_version": EMBEDDING_LEDGER_SCHEMA_VERSION,
        "source_database_sha256": database_sha256,
        "source_database_size_bytes": identity.st_size,
    }
    ledger_bytes = canonical_json_bytes(ledger_payload)
    if len(ledger_bytes) > EMBEDDING_LEDGER_CAP_BYTES:
        raise StateSeedError("embedding_ledger_limit_exceeded")
    return EmbeddingSeedPlan(
        source_root=root,
        source_database_sha256=database_sha256,
        source_database_size_bytes=identity.st_size,
        source_database_identity=(identity.st_dev, identity.st_ino),
        rows=tuple(rows),
        row_root_sha256=row_root,
        profile_rollup={
            profile_id: {
                "dimensions": sorted(entry["dimensions"]),
                "max_created_at": entry["max_created_at"],
                "min_created_at": entry["min_created_at"],
                "row_count": entry["row_count"],
            }
            for profile_id, entry in sorted(rollup.items())
        },
        ledger_bytes=ledger_bytes,
        ledger_sha256=hashlib.sha256(ledger_bytes).hexdigest(),
    )


def _persist_ledger_bytes(ledger_dir: Path, *, ledger_sha256: str, payload_bytes: bytes) -> Path:
    dir_fd = os.open(ledger_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    final_name = f"{ledger_sha256}.json"
    temp_name = f".{ledger_sha256}.{uuid.uuid4().hex}.tmp"
    temp_fd = -1
    try:
        flags = (
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
        temp_fd = os.open(temp_name, flags, 0o600, dir_fd=dir_fd)
        view = memoryview(payload_bytes)
        while view:
            written = os.write(temp_fd, view)
            if written < 1:
                raise OSError("embedding seed ledger write stalled")
            view = view[written:]
        os.fsync(temp_fd)
        os.close(temp_fd)
        temp_fd = -1
        try:
            os.link(temp_name, final_name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd, follow_symlinks=False)
        except FileExistsError:
            listed = os.stat(final_name, dir_fd=dir_fd, follow_symlinks=False)
            if (
                stat.S_ISLNK(listed.st_mode)
                or not stat.S_ISREG(listed.st_mode)
                or listed.st_size != len(payload_bytes)
            ):
                raise StateSeedError("embedding_seed_ledger_conflict") from None
            try:
                existing_fd = os.open(final_name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=dir_fd)
            except OSError as exc:
                raise StateSeedError("embedding_seed_ledger_conflict") from exc
            try:
                with os.fdopen(existing_fd, "rb") as handle:
                    existing_digest = hashlib.sha256(handle.read()).hexdigest()
            except OSError as exc:
                raise StateSeedError("embedding_seed_ledger_conflict") from exc
            if existing_digest != ledger_sha256:
                raise StateSeedError("embedding_seed_ledger_conflict") from None
        os.unlink(temp_name, dir_fd=dir_fd)
        os.fsync(dir_fd)
    except OSError as exc:
        raise StateSeedError("embedding_seed_ledger_write_failed") from exc
    finally:
        if temp_fd >= 0:
            os.close(temp_fd)
        with suppress(FileNotFoundError):
            os.unlink(temp_name, dir_fd=dir_fd)
        os.close(dir_fd)
    return ledger_dir / final_name


def apply_embedding_cache_seed_v122(
    plan: EmbeddingSeedPlan,
    state: WorkerState,
    destination_state_dir: Path,
) -> dict[str, Any]:
    """Idempotently import verified embedding rows; never overwrite destination rows."""

    destination = _absolute_without_resolving(destination_state_dir)
    if paths_overlap(plan.source_root, destination):
        raise StateSeedError("source_destination_overlap")
    _ensure_destination_dir(destination, _EMBEDDING_LEDGER_DIRECTORY)

    imported = 0
    reused = 0
    batch: list[EmbeddingSeedRow] = []
    descriptor, connection = _open_source_database(plan.source_root)
    try:
        identity = os.fstat(descriptor)
        if (identity.st_dev, identity.st_ino) != plan.source_database_identity:
            raise StateSeedError("source_database_changed")
        if _hash_descriptor(descriptor) != plan.source_database_sha256:
            raise StateSeedError("source_database_changed")
        source_rows, _ = _profile_rollup(_iter_validated_rows(connection))
        if tuple(row.row_sha256 for row in source_rows) != tuple(row.row_sha256 for row in plan.rows):
            raise StateSeedError("source_database_changed")

        def insert_batch(rows: list[EmbeddingSeedRow]) -> None:
            nonlocal imported
            with state.transaction() as txn:
                for row in rows:
                    blob = _source_blob(connection, row)
                    txn.execute(
                        """INSERT INTO embedding_cache_v5
                           (cache_key,profile_id,input_sha256,dimension,dtype,normalization,
                            embedding,created_at)
                           VALUES(?,?,?,?,?,?,?,?)""",
                        (
                            row.cache_key,
                            row.profile_id,
                            row.input_sha256,
                            row.dimension,
                            row.dtype,
                            row.normalization,
                            blob,
                            row.created_at,
                        ),
                    )
            imported += len(rows)

        for row in plan.rows:
            existing = state.connection.execute(
                """SELECT profile_id,input_sha256,dimension,dtype,normalization,embedding,created_at
                   FROM embedding_cache_v5 WHERE cache_key=?""",
                (row.cache_key,),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing[0]) != row.profile_id
                    or str(existing[1]) != row.input_sha256
                    or int(existing[2]) != row.dimension
                    or str(existing[3]) != row.dtype
                    or str(existing[4]) != row.normalization
                    or str(existing[6]) != row.created_at
                    or hashlib.sha256(bytes(existing[5])).hexdigest() != row.embedding_sha256
                ):
                    raise StateSeedError("destination_row_conflict")
                reused += 1
                continue
            batch.append(row)
            if len(batch) >= _EMBEDDING_BATCH_ROWS:
                insert_batch(batch)
                batch = []
        if batch:
            insert_batch(batch)
            batch = []
    finally:
        connection.close()
        os.close(descriptor)

    ledger_path = _persist_ledger_bytes(
        destination / _EMBEDDING_LEDGER_DIRECTORY,
        ledger_sha256=plan.ledger_sha256,
        payload_bytes=plan.ledger_bytes,
    )
    report = plan.report(applied=True, ledger_path=str(ledger_path.relative_to(destination)))
    report["imported_rows"] = imported
    report["reused_rows"] = reused
    return report


def _source_blob(connection: sqlite3.Connection, row: EmbeddingSeedRow) -> bytes:
    raw = connection.execute(
        "SELECT embedding FROM embedding_cache_v5 WHERE cache_key=?", (row.cache_key,)
    ).fetchone()
    if raw is None:
        raise StateSeedError("source_database_changed")
    blob = bytes(raw[0])
    if hashlib.sha256(blob).hexdigest() != row.embedding_sha256:
        raise StateSeedError("source_embedding_row_length_invalid")
    return blob


def load_embedding_seed_ledger_v122(state_dir: Path) -> dict[str, Any] | None:
    """Return the single sealed embedding-seed ledger, or ``None`` when absent."""

    directory = _absolute_without_resolving(state_dir) / _EMBEDDING_LEDGER_DIRECTORY
    try:
        names = sorted(
            name for name in os.listdir(directory) if name.endswith(".json") and _SHA256.fullmatch(name[:-5])
        )
    except FileNotFoundError:
        return None
    if not names:
        return None
    if len(names) > 1:
        raise StateSeedError("embedding_seed_ledger_conflict")
    path = directory / names[0]
    listed = path.lstat()
    if stat.S_ISLNK(listed.st_mode) or not stat.S_ISREG(listed.st_mode):
        raise StateSeedError("unsafe_destination_ledger_path")
    payload_bytes = path.read_bytes()
    if hashlib.sha256(payload_bytes).hexdigest() != names[0][:-5]:
        raise StateSeedError("embedding_seed_ledger_conflict")
    payload: Any = json.loads(payload_bytes)
    if not isinstance(payload, dict) or payload.get("schema_version") != EMBEDDING_LEDGER_SCHEMA_VERSION:
        raise StateSeedError("embedding_seed_ledger_conflict")
    return payload
