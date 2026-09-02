"""Atomic local generation activation with request-scoped handle pinning."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from cardrag_mcp.models import Document, ServingMetadata
from cardrag_mcp.quota import (
    DEFAULT_EXHAUSTIVE_AUDIT_MAX_ARTIFACT_BYTES,
    DEFAULT_EXHAUSTIVE_AUDIT_MAX_JOBS,
    DEFAULT_EXHAUSTIVE_AUDIT_MAX_TOTAL_BYTES,
    DEFAULT_MAX_GENERATION_DOWNLOAD_BYTES,
    DEFAULT_MAX_SERVING_DATABASE_BYTES,
    DEFAULT_MAX_STATE_BYTES,
    DEFAULT_RERANKER_AUDIT_MAX_ARTIFACT_BYTES,
    DEFAULT_RERANKER_AUDIT_MAX_JOBS,
    DEFAULT_RERANKER_AUDIT_MAX_TOTAL_BYTES,
    DEFAULT_RESERVED_FREE_SPACE_BYTES,
    StateQuotaPolicy,
    StateQuotaReservation,
    StorageQuotaError,
    configure_state_quota,
    ensure_global_state_growth,
    reserve_global_state_growth,
    safe_shared_exhaustive_audit_usage,
    state_quota_guard,
    state_quota_transaction,
    validate_byte_limit,
    validate_count_limit,
)
from cardrag_mcp.schema import LoadedVectors, load_vectors, readonly_connection, validate_schema
from cardrag_mcp.schema_v5 import LoadedVectorsV5, load_vectors_v5, validate_schema_v5

GENERATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
LOCAL_POINTER_SCHEMA = "cardrag.mcp-local-pointer.v1"
GENERATION_GC_ROOT_SCHEMA = "cardrag.mcp-generation-gc-root.v1"
GENERATION_GC_ROOT_OWNER = re.compile(r"^map-reduce-[0-9a-f]{64}$")
GENERATION_GC_ROOT_TEMP = re.compile(r"^\.generation-gc-root\.[A-Za-z0-9_-]{1,128}$")
MAXIMUM_GENERATION_GC_ROOT_BYTES = 4 * 1024
MAXIMUM_GENERATION_GC_ROOT_TEMPS = 64
GENERATION_LIFECYCLE_LOCK = ".generation-lifecycle.lock"


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(value, output, sort_keys=True, separators=(",", ":"))
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def cas_path(object_root: Path, digest: str) -> Path:
    if not SHA256.fullmatch(digest):
        raise ValueError("invalid CAS digest")
    return object_root / "sha256" / digest[:2] / digest


@dataclass(frozen=True, slots=True)
class GenerationHandle:
    generation_id: str
    directory: Path
    database_path: Path
    object_root: Path
    metadata: ServingMetadata
    vectors: LoadedVectors | LoadedVectorsV5
    vector_sidecar_path: Path | None = None

    def connect(self) -> sqlite3.Connection:
        return readonly_connection(self.database_path)

    def pdf_path(self, document: Document) -> Path:
        candidate = cas_path(self.object_root, document.pdf_sha256)
        resolved_root = self.object_root.resolve()
        resolved = candidate.resolve(strict=True)
        if not resolved.is_relative_to(resolved_root):
            raise RuntimeError("PDF CAS path escaped object root")
        mode = resolved.stat().st_mode
        if not stat.S_ISREG(mode) or resolved.is_symlink():
            raise RuntimeError("PDF CAS object is not a regular file")
        return resolved


def load_generation_handle(
    directory: Path,
    object_root: Path,
    *,
    maximum_vector_bytes: int,
    maximum_database_bytes: int = DEFAULT_MAX_SERVING_DATABASE_BYTES,
    maximum_vector_sidecar_bytes: int | None = None,
    maximum_resident_vector_bytes: int | None = None,
    expected_generation_id: str | None = None,
    expected_embedding_model: str | None = None,
    expected_embedding_count: int | None = None,
) -> GenerationHandle:
    sidecar_limit = (
        maximum_vector_bytes
        if maximum_vector_sidecar_bytes is None
        else maximum_vector_sidecar_bytes
    )
    resident_limit = (
        maximum_vector_bytes
        if maximum_resident_vector_bytes is None
        else maximum_resident_vector_bytes
    )
    for label, value in (
        ("maximum vector bytes", maximum_vector_bytes),
        ("maximum vector sidecar bytes", sidecar_limit),
        ("maximum resident vector bytes", resident_limit),
        ("maximum serving database bytes", maximum_database_bytes),
    ):
        validate_byte_limit(value, label=label)
    directory = directory.resolve(strict=True)
    generation_id = directory.name
    if not GENERATION_ID.fullmatch(generation_id):
        raise RuntimeError("invalid local generation ID")
    database_path = directory / "index.sqlite3"
    if database_path.is_symlink() or not database_path.is_file():
        raise RuntimeError("generation database is missing or unsafe")
    if database_path.stat().st_size > maximum_database_bytes:
        raise RuntimeError("generation database exceeds the configured hard cap")
    with readonly_connection(database_path) as connection:
        schema_row = connection.execute(
            "SELECT value FROM metadata WHERE key='schema_id'"
        ).fetchone()
        schema_id = "" if schema_row is None else str(schema_row[0])
        metadata = (
            validate_schema_v5(connection, maximum_sidecar_bytes=sidecar_limit)
            if schema_id == "cardrag.serving-db.v5"
            else validate_schema(connection, maximum_vector_bytes=maximum_vector_bytes)
        )
        if metadata.generation_id != generation_id:
            raise RuntimeError("database generation ID differs from its directory")
        if expected_generation_id is not None and metadata.generation_id != expected_generation_id:
            raise RuntimeError("database generation ID differs from manifest")
        if (
            expected_embedding_model is not None
            and metadata.embedding_model != expected_embedding_model
        ):
            raise RuntimeError("database embedding model differs from manifest")
        if (
            expected_embedding_count is not None
            and metadata.embedding_count != expected_embedding_count
        ):
            raise RuntimeError("database embedding count differs from manifest")
        resident_bytes = metadata.embedding_count * 4
        if metadata.schema_id != "cardrag.serving-db.v5":
            resident_bytes += metadata.embedding_count * metadata.embedding_dimension * 4
        if resident_bytes > resident_limit:
            raise RuntimeError(
                "generation vector arrays exceed the configured resident memory limit"
            )
        sidecar_candidate = directory / "vectors.f32"
        vector_sidecar_path: Path | None
        if metadata.schema_id == "cardrag.serving-db.v5":
            vectors: LoadedVectors | LoadedVectorsV5 = load_vectors_v5(
                connection,
                sidecar_candidate,
                metadata=metadata,
                maximum_sidecar_bytes=sidecar_limit,
            )
            vector_sidecar_path = sidecar_candidate
        else:
            vectors = load_vectors(
                connection,
                expected_count=metadata.embedding_count,
                maximum_bytes=maximum_vector_bytes,
            )
            vector_sidecar_path = None
    return GenerationHandle(
        generation_id=generation_id,
        directory=directory,
        database_path=database_path,
        object_root=object_root.resolve(),
        metadata=metadata,
        vectors=vectors,
        vector_sidecar_path=(
            None if vector_sidecar_path is None else vector_sidecar_path.resolve(strict=True)
        ),
    )


def _handle_resident_vector_bytes(handle: GenerationHandle) -> int:
    """Count heap-backed arrays while excluding the reclaimable mmap address range."""

    matrix = handle.vectors.matrix
    matrix_bytes = 0 if isinstance(matrix, np.memmap) else matrix.nbytes
    return matrix_bytes + handle.vectors.norms.nbytes


@dataclass(slots=True)
class _HandleEntry:
    handle: GenerationHandle
    references: int = 0


class GenerationStore:
    """Own the last-good handle and retain old generations until pins drain."""

    def __init__(
        self,
        root: Path,
        *,
        maximum_vector_bytes: int,
        maximum_vector_sidecar_bytes: int | None = None,
        maximum_resident_vector_bytes: int | None = None,
        maximum_pdf_bytes: int = 100 * 1024 * 1024,
        maximum_database_bytes: int = DEFAULT_MAX_SERVING_DATABASE_BYTES,
        maximum_generation_download_bytes: int = DEFAULT_MAX_GENERATION_DOWNLOAD_BYTES,
        maximum_state_bytes: int = DEFAULT_MAX_STATE_BYTES,
        reserved_free_space_bytes: int = DEFAULT_RESERVED_FREE_SPACE_BYTES,
        exhaustive_audit_max_jobs: int = DEFAULT_EXHAUSTIVE_AUDIT_MAX_JOBS,
        exhaustive_audit_max_total_bytes: int = DEFAULT_EXHAUSTIVE_AUDIT_MAX_TOTAL_BYTES,
        exhaustive_audit_max_artifact_bytes: int = DEFAULT_EXHAUSTIVE_AUDIT_MAX_ARTIFACT_BYTES,
        reranker_audit_max_jobs: int = DEFAULT_RERANKER_AUDIT_MAX_JOBS,
        reranker_audit_max_total_bytes: int = DEFAULT_RERANKER_AUDIT_MAX_TOTAL_BYTES,
        reranker_audit_max_artifact_bytes: int = DEFAULT_RERANKER_AUDIT_MAX_ARTIFACT_BYTES,
        retention: int = 2,
    ) -> None:
        if type(retention) is not int or retention < 2:
            raise ValueError("local retention must be at least two")
        sidecar_limit = (
            maximum_vector_bytes
            if maximum_vector_sidecar_bytes is None
            else maximum_vector_sidecar_bytes
        )
        resident_limit = (
            maximum_vector_bytes
            if maximum_resident_vector_bytes is None
            else maximum_resident_vector_bytes
        )
        for label, value in (
            ("maximum vector bytes", maximum_vector_bytes),
            ("maximum vector sidecar bytes", sidecar_limit),
            ("maximum resident vector bytes", resident_limit),
            ("maximum PDF bytes", maximum_pdf_bytes),
            ("maximum serving database bytes", maximum_database_bytes),
            ("maximum generation download bytes", maximum_generation_download_bytes),
        ):
            validate_byte_limit(value, label=label)
        validate_count_limit(exhaustive_audit_max_jobs, label="maximum exhaustive audit jobs")
        validate_count_limit(reranker_audit_max_jobs, label="maximum reranker audit jobs")
        if maximum_database_bytes > maximum_generation_download_bytes:
            raise ValueError("serving database cap exceeds generation download quota")
        if sidecar_limit > maximum_generation_download_bytes:
            raise ValueError("vector sidecar cap exceeds generation download quota")
        if maximum_pdf_bytes > maximum_generation_download_bytes:
            raise ValueError("PDF cap exceeds generation download quota")
        if maximum_generation_download_bytes > maximum_state_bytes:
            raise ValueError("generation download quota exceeds state quota")
        self.root = root.resolve()
        self.generations = self.root / "generations"
        self.objects = self.root / "objects"
        self.incoming = self.root / ".incoming"
        self.current_path = self.root / "current.json"
        self.generation_gc_roots = self.root / "generation-gc-roots"
        self.generation_lifecycle_lock = self.root / GENERATION_LIFECYCLE_LOCK
        self.maximum_vector_bytes = maximum_vector_bytes
        self.maximum_vector_sidecar_bytes = sidecar_limit
        self.maximum_resident_vector_bytes = resident_limit
        self.maximum_pdf_bytes = maximum_pdf_bytes
        self.maximum_database_bytes = maximum_database_bytes
        self.maximum_generation_download_bytes = maximum_generation_download_bytes
        self.maximum_state_bytes = maximum_state_bytes
        self.reserved_free_space_bytes = reserved_free_space_bytes
        self.exhaustive_audit_max_jobs = exhaustive_audit_max_jobs
        self.exhaustive_audit_max_total_bytes = exhaustive_audit_max_total_bytes
        self.retention = retention
        for path in (
            self.root,
            self.generations,
            self.objects,
            self.incoming,
            self.generation_gc_roots,
        ):
            if path.is_symlink():
                raise RuntimeError("local state directory must not be a symlink")
            path.mkdir(parents=True, exist_ok=True)
            if not path.is_dir():
                raise RuntimeError("local state path is not a directory")
        configure_state_quota(
            self.root,
            StateQuotaPolicy(
                maximum_state_bytes=maximum_state_bytes,
                reserved_free_space_bytes=reserved_free_space_bytes,
                exhaustive_audit_max_jobs=exhaustive_audit_max_jobs,
                exhaustive_audit_max_total_bytes=exhaustive_audit_max_total_bytes,
                exhaustive_audit_max_artifact_bytes=exhaustive_audit_max_artifact_bytes,
                reranker_audit_max_jobs=reranker_audit_max_jobs,
                reranker_audit_max_total_bytes=reranker_audit_max_total_bytes,
                reranker_audit_max_artifact_bytes=reranker_audit_max_artifact_bytes,
            ),
        )
        self._lock = threading.RLock()
        self._lifecycle_local_lock = threading.RLock()
        self._lifecycle_state = threading.local()
        self._lifecycle_pid = os.getpid()
        self._active: _HandleEntry | None = None
        self._entries: dict[str, _HandleEntry] = {}

    def _ensure_process_identity(self) -> None:
        """Discard inherited process-local locks and handles after ``fork``."""

        current_pid = os.getpid()
        if self._lifecycle_pid == current_pid:
            return
        # Only the calling thread survives fork.  No inherited handle remains
        # safe to serve; the child must explicitly load_current()/pin_generation.
        self._lock = threading.RLock()
        self._lifecycle_local_lock = threading.RLock()
        self._lifecycle_state = threading.local()
        self._lifecycle_pid = current_pid
        self._active = None
        self._entries = {}

    @contextlib.contextmanager
    def _generation_lifecycle(self) -> Iterator[None]:
        """Serialize root and generation mutations across processes.

        The only permitted nested order is lifecycle flock, then ``self._lock``,
        then the quota-global flock.  This helper never acquires either nested
        lock itself, which keeps every caller's order visible at the call site.
        """

        self._ensure_process_identity()
        with self._lifecycle_local_lock:
            depth = getattr(self._lifecycle_state, "depth", 0)
            if depth:
                self._lifecycle_state.depth = depth + 1
                try:
                    yield
                finally:
                    self._lifecycle_state.depth -= 1
                return
            flags = (
                os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            )
            descriptor: int | None = None
            try:
                descriptor = os.open(self.generation_lifecycle_lock, flags, 0o600)
                metadata = os.fstat(descriptor)
            except OSError as exc:
                if descriptor is not None:
                    os.close(descriptor)
                raise RuntimeError("generation lifecycle lock is unavailable") from exc
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_size != 0
            ):
                os.close(descriptor)
                raise RuntimeError("generation lifecycle lock is unsafe")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                self._lifecycle_state.depth = 1
                try:
                    yield
                finally:
                    self._lifecycle_state.depth = 0
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    @property
    def active_generation_id(self) -> str | None:
        self._ensure_process_identity()
        with self._lock:
            return self._active.handle.generation_id if self._active is not None else None

    @property
    def resident_vector_bytes(self) -> int:
        """Return heap-backed legacy matrices and norm arrays for resident handles."""

        self._ensure_process_identity()
        with self._lock:
            return sum(
                _handle_resident_vector_bytes(entry.handle) for entry in self._entries.values()
            )

    def ensure_state_capacity(
        self,
        logical_growth_bytes: int,
        *,
        peak_growth_bytes: int | None = None,
    ) -> None:
        """Reject a new local write without deleting any retained state."""

        ensure_global_state_growth(
            self.root,
            logical_growth_bytes,
            peak_growth_bytes=peak_growth_bytes,
        )

    def reserve_state_capacity(
        self,
        logical_growth_bytes: int,
        *,
        peak_growth_bytes: int | None = None,
    ) -> StateQuotaReservation:
        """Reserve state bytes while an asynchronous bounded write is in flight."""

        return reserve_global_state_growth(
            self.root,
            logical_growth_bytes,
            peak_growth_bytes=peak_growth_bytes,
        )

    def load_current(self) -> bool:
        self._ensure_process_identity()
        if not self.current_path.exists():
            return False
        try:
            payload = json.loads(self.current_path.read_text(encoding="utf-8"))
            if set(payload) != {"schema_version", "generation_id"}:
                raise ValueError
            if payload["schema_version"] != LOCAL_POINTER_SCHEMA:
                raise ValueError
            generation_id = str(payload["generation_id"])
            if not GENERATION_ID.fullmatch(generation_id):
                raise ValueError
            handle = load_generation_handle(
                self.generations / generation_id,
                self.objects,
                maximum_vector_bytes=self.maximum_vector_bytes,
                maximum_database_bytes=self.maximum_database_bytes,
                maximum_vector_sidecar_bytes=self.maximum_vector_sidecar_bytes,
                maximum_resident_vector_bytes=self.maximum_resident_vector_bytes,
                expected_generation_id=generation_id,
            )
            self.verify_handle_pdfs(handle)
        except Exception:
            return False
        with self._lock:
            entry = _HandleEntry(handle)
            self._entries[generation_id] = entry
            self._active = entry
        return True

    @contextlib.contextmanager
    def pin(self) -> Iterator[GenerationHandle]:
        self._ensure_process_identity()
        with self._lock:
            if self._active is None:
                raise RuntimeError("no serving generation is active")
            entry = self._active
            entry.references += 1
        try:
            yield entry.handle
        finally:
            with self._generation_lifecycle():
                with self._lock:
                    entry.references -= 1
                    if entry is not self._active and entry.references == 0:
                        self._prune_locked()

    @contextlib.contextmanager
    def pin_generation(self, generation_id: str) -> Iterator[GenerationHandle]:
        """Pin exactly one retained generation without falling back to active."""

        if GENERATION_ID.fullmatch(generation_id) is None:
            raise RuntimeError("requested generation ID is invalid")
        with self._generation_lifecycle():
            with self._lock:
                entry = self._entries.get(generation_id)
                if entry is None:
                    directory = self.generations / generation_id
                    if directory.is_symlink() or not directory.is_dir():
                        raise RuntimeError("requested generation is missing or unsafe")
                    try:
                        resolved = directory.resolve(strict=True)
                    except OSError as exc:
                        raise RuntimeError("requested generation is unreadable") from exc
                    if resolved.parent != self.generations.resolve():
                        raise RuntimeError("requested generation escaped its local root")
                    try:
                        handle = load_generation_handle(
                            resolved,
                            self.objects,
                            maximum_vector_bytes=self.maximum_vector_bytes,
                            maximum_database_bytes=self.maximum_database_bytes,
                            maximum_vector_sidecar_bytes=self.maximum_vector_sidecar_bytes,
                            maximum_resident_vector_bytes=self.maximum_resident_vector_bytes,
                            expected_generation_id=generation_id,
                        )
                        self.verify_handle_pdfs(handle)
                    except Exception as exc:
                        raise RuntimeError("requested generation failed exact validation") from exc
                    resident_bytes = sum(
                        _handle_resident_vector_bytes(item.handle)
                        for item in self._entries.values()
                    ) + _handle_resident_vector_bytes(handle)
                    if resident_bytes > self.maximum_resident_vector_bytes:
                        raise RuntimeError(
                            "requested generation exceeds the resident vector memory limit"
                        )
                    entry = _HandleEntry(handle)
                    self._entries[generation_id] = entry
                entry.references += 1
        try:
            yield entry.handle
        finally:
            with self._generation_lifecycle():
                with self._lock:
                    entry.references -= 1
                    if entry is not self._active and entry.references == 0:
                        self._prune_locked()

    def acquire_generation_gc_root(self, owner_id: str, generation_id: str) -> None:
        """Durably retain a generation for a resumable nonterminal job."""

        if GENERATION_GC_ROOT_OWNER.fullmatch(owner_id) is None:
            raise RuntimeError("generation GC root owner is invalid")
        if GENERATION_ID.fullmatch(generation_id) is None:
            raise RuntimeError("generation GC root generation is invalid")
        with self._generation_lifecycle():
            with self._lock:
                roots = self._durable_generation_roots_locked()
                self._acquire_generation_gc_root_locked(
                    owner_id,
                    generation_id,
                    roots=roots,
                )

    def claim_current_generation_gc_root(
        self,
        owner_for_generation: Callable[[str], str],
    ) -> tuple[str, str, bool]:
        """Atomically resume or root one durable-current generation identity.

        The callback must deterministically derive the owner from a generation
        ID plus caller-sealed query/profile inputs.  Scanning existing roots,
        reading the authoritative pointer, and publishing a new root all occur
        under the lifecycle flock, preventing cross-generation duplicate jobs.
        """

        with self._generation_lifecycle():
            with self._lock:
                roots = self._durable_generation_roots_locked()
                matching: list[tuple[str, str]] = []
                for owner_id, generation_id in roots.items():
                    derived = owner_for_generation(generation_id)
                    if GENERATION_GC_ROOT_OWNER.fullmatch(derived) is None:
                        raise RuntimeError("derived generation GC root owner is invalid")
                    if derived == owner_id:
                        matching.append((owner_id, generation_id))
                if len(matching) > 1:
                    owners = ",".join(sorted(owner for owner, _ in matching))
                    raise RuntimeError(
                        f"multiple durable jobs match one sealed query/profile: {owners}"
                    )
                if matching:
                    owner_id, generation_id = matching[0]
                    return owner_id, generation_id, False

                generation_id = self._durable_current_generation_id_locked()
                owner_id = owner_for_generation(generation_id)
                if GENERATION_GC_ROOT_OWNER.fullmatch(owner_id) is None:
                    raise RuntimeError("derived generation GC root owner is invalid")
                self._acquire_generation_gc_root_locked(
                    owner_id,
                    generation_id,
                    roots=roots,
                )
                return owner_id, generation_id, True

    def _durable_current_generation_id_locked(self) -> str:
        path = self.current_path
        if path.is_symlink() or not path.is_file():
            raise RuntimeError("durable current generation pointer is missing or unsafe")
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        try:
            descriptor = os.open(path, flags)
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_size < 1
                or before.st_size > 4 * 1024
            ):
                raise RuntimeError("durable current generation pointer is unsafe")
            encoded = os.read(descriptor, before.st_size + 1)
            after = os.fstat(descriptor)
        except OSError as exc:
            raise RuntimeError("durable current generation pointer is unreadable") from exc
        finally:
            if "descriptor" in locals():
                os.close(descriptor)
        if len(encoded) != before.st_size or (
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (after.st_ino, after.st_size, after.st_mtime_ns):
            raise RuntimeError("durable current generation pointer changed while reading")

        def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate key")
                result[key] = value
            return result

        try:
            parsed = json.loads(
                encoded.decode("utf-8"),
                object_pairs_hook=unique_object,
                parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
            )
        except (UnicodeError, ValueError) as exc:
            raise RuntimeError("durable current generation pointer is not strict JSON") from exc
        if not isinstance(parsed, dict) or set(parsed) != {"schema_version", "generation_id"}:
            raise RuntimeError("durable current generation pointer fields are invalid")
        generation_id = parsed["generation_id"]
        canonical = (json.dumps(parsed, sort_keys=True, separators=(",", ":")) + "\n").encode()
        if (
            parsed["schema_version"] != LOCAL_POINTER_SCHEMA
            or not isinstance(generation_id, str)
            or GENERATION_ID.fullmatch(generation_id) is None
            or encoded != canonical
        ):
            raise RuntimeError("durable current generation pointer identity is invalid")
        generation_path = self.generations / generation_id
        if generation_path.is_symlink() or not generation_path.is_dir():
            raise RuntimeError("durable current generation is missing")
        try:
            resolved_generation = generation_path.resolve(strict=True)
        except OSError as exc:
            raise RuntimeError("durable current generation is unreadable") from exc
        if resolved_generation.parent != self.generations.resolve():
            raise RuntimeError("durable current generation escaped its root")
        return generation_id

    def _acquire_generation_gc_root_locked(
        self,
        owner_id: str,
        generation_id: str,
        *,
        roots: dict[str, str],
    ) -> None:
        payload = {
            "schema_version": GENERATION_GC_ROOT_SCHEMA,
            "owner_id": owner_id,
            "generation_id": generation_id,
        }
        encoded = self._canonical_generation_gc_root(payload)
        generation_path = self.generations / generation_id
        if generation_path.is_symlink() or not generation_path.is_dir():
            raise RuntimeError("generation GC root references a missing generation")
        if generation_path.resolve(strict=True).parent != self.generations.resolve():
            raise RuntimeError("generation GC root escaped the generation directory")
        path = self.generation_gc_roots / f"{owner_id}.json"
        if path.exists() or path.is_symlink():
            existing = self._read_generation_gc_root(path)
            if existing != payload or roots.get(owner_id) != generation_id:
                raise RuntimeError("generation GC root already binds another generation")
            return
        try:
            with state_quota_guard(
                self.root,
                len(encoded),
                peak_growth_bytes=2 * len(encoded),
            ):
                total, jobs = safe_shared_exhaustive_audit_usage(
                    self.root,
                    prospective_map_job_id=owner_id,
                )
                if (
                    total > self.exhaustive_audit_max_total_bytes
                    or len(encoded) > self.exhaustive_audit_max_total_bytes - total
                    or 2 * len(encoded) > self.exhaustive_audit_max_total_bytes - total
                ):
                    raise RuntimeError(
                        "shared exhaustive audit total quota rejected generation root"
                    )
                if jobs > self.exhaustive_audit_max_jobs:
                    raise RuntimeError("shared exhaustive audit job quota rejected generation root")
                descriptor, temporary_name = tempfile.mkstemp(
                    prefix=".generation-gc-root.",
                    dir=self.incoming,
                )
                temporary = Path(temporary_name)
                try:
                    with os.fdopen(descriptor, "wb") as output:
                        output.write(encoded)
                        output.flush()
                        os.fchmod(output.fileno(), 0o400)
                        os.fsync(output.fileno())
                    try:
                        os.link(temporary, path, follow_symlinks=False)
                    except FileExistsError:
                        if self._read_generation_gc_root(path) != payload:
                            raise RuntimeError(
                                "generation GC root already binds another generation"
                            ) from None
                    else:
                        _fsync_directory(self.generation_gc_roots)
                finally:
                    if temporary.exists() or temporary.is_symlink():
                        temporary.unlink()
                        _fsync_directory(self.incoming)
        except StorageQuotaError:
            raise RuntimeError("MCP state quota rejected generation GC root") from None

    def verify_generation_gc_root(self, owner_id: str, generation_id: str) -> None:
        with self._generation_lifecycle():
            with self._lock:
                roots = self._durable_generation_roots_locked()
                if roots.get(owner_id) != generation_id:
                    raise RuntimeError("nonterminal job is missing its durable generation GC root")

    def generation_for_gc_root(self, owner_id: str) -> str | None:
        """Resolve one durable owner root without consulting the active pointer."""

        if GENERATION_GC_ROOT_OWNER.fullmatch(owner_id) is None:
            raise RuntimeError("generation GC root owner is invalid")
        with self._generation_lifecycle():
            with self._lock:
                return self._durable_generation_roots_locked().get(owner_id)

    def generation_gc_roots_snapshot(self) -> dict[str, str]:
        """Return a validated owner-to-generation snapshot for start recovery."""

        with self._generation_lifecycle():
            with self._lock:
                return dict(self._durable_generation_roots_locked())

    def release_generation_gc_root(self, owner_id: str, generation_id: str) -> None:
        """Release a job root only after its terminal artifact is durable."""

        with self._generation_lifecycle():
            with self._lock:
                roots = self._durable_generation_roots_locked()
                existing = roots.get(owner_id)
                if existing is None:
                    return
                if existing != generation_id:
                    raise RuntimeError("generation GC root release identity is stale")
                path = self.generation_gc_roots / f"{owner_id}.json"
                with state_quota_transaction(self.root):
                    path.unlink()
                    _fsync_directory(self.generation_gc_roots)

    def activate(self, handle: GenerationHandle) -> None:
        if handle.directory.parent.resolve() != self.generations.resolve():
            raise ValueError("generation is outside the local generation root")
        with self._generation_lifecycle():
            with self._lock:
                durable_roots = self._durable_generation_roots_locked()
                entry = self._entries.get(handle.generation_id)
                prospective_resident_bytes = sum(
                    _handle_resident_vector_bytes(item.handle)
                    for generation_id, item in self._entries.items()
                    if generation_id != handle.generation_id
                )
                prospective_resident_bytes += _handle_resident_vector_bytes(
                    handle if entry is None else entry.handle
                )
                if prospective_resident_bytes > self.maximum_resident_vector_bytes:
                    raise RuntimeError(
                        "candidate plus resident/pinned vector memory exceeds the resident limit"
                    )
                pointer = {
                    "schema_version": LOCAL_POINTER_SCHEMA,
                    "generation_id": handle.generation_id,
                }
                pointer_bytes = (
                    json.dumps(pointer, sort_keys=True, separators=(",", ":")) + "\n"
                ).encode()
                if self.current_path.is_symlink():
                    raise RuntimeError("local generation pointer must not be a symlink")
                current_size = 0
                if self.current_path.exists():
                    if not self.current_path.is_file():
                        raise RuntimeError("local generation pointer is not a regular file")
                    current_size = self.current_path.stat().st_size
                logical_growth = max(0, len(pointer_bytes) - current_size)
                with state_quota_guard(
                    self.root,
                    logical_growth,
                    peak_growth_bytes=len(pointer_bytes),
                ):
                    _atomic_json(self.current_path, pointer)
                if entry is None:
                    entry = _HandleEntry(handle)
                    self._entries[handle.generation_id] = entry
                self._active = entry
                self._prune_locked(durable_roots)

    @staticmethod
    def _canonical_generation_gc_root(value: dict[str, str]) -> bytes:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")

    def _read_generation_gc_root(
        self,
        path: Path,
        *,
        allowed_links: tuple[int, ...] = (1,),
    ) -> dict[str, str]:
        if path.is_symlink() or not path.is_file():
            raise RuntimeError("generation GC root is missing or unsafe")
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise RuntimeError("generation GC root is unreadable") from exc
        try:
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink not in allowed_links
                or before.st_size < 1
                or before.st_size > MAXIMUM_GENERATION_GC_ROOT_BYTES
            ):
                raise RuntimeError("generation GC root has an invalid size or type")
            encoded = os.read(descriptor, before.st_size + 1)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        if len(encoded) != before.st_size or (
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise RuntimeError("generation GC root changed while being read")

        def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate key")
                result[key] = value
            return result

        try:
            parsed = json.loads(
                encoded.decode("utf-8"),
                object_pairs_hook=unique_object,
                parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
            )
            if not isinstance(parsed, dict) or set(parsed) != {
                "schema_version",
                "owner_id",
                "generation_id",
            }:
                raise ValueError
            payload = {key: str(value) for key, value in parsed.items()}
        except (UnicodeError, ValueError) as exc:
            raise RuntimeError("generation GC root is not strict JSON") from exc
        if encoded != self._canonical_generation_gc_root(payload):
            raise RuntimeError("generation GC root is not canonical JSON")
        owner_id = payload["owner_id"]
        generation_id = payload["generation_id"]
        if (
            payload["schema_version"] != GENERATION_GC_ROOT_SCHEMA
            or GENERATION_GC_ROOT_OWNER.fullmatch(owner_id) is None
            or GENERATION_ID.fullmatch(generation_id) is None
            or path.name != f"{owner_id}.json"
        ):
            raise RuntimeError("generation GC root identity is invalid")
        return payload

    def _reconcile_generation_gc_root_temporaries_locked(self) -> None:
        """Recover only writer-owned root publication crash windows."""

        if (
            self.incoming.is_symlink()
            or not self.incoming.is_dir()
            or self.incoming.resolve(strict=True) != self.incoming
        ):
            raise RuntimeError("incoming generation directory is unsafe")
        candidates = tuple(
            path
            for path in sorted(self.incoming.iterdir(), key=lambda item: item.name)
            if GENERATION_GC_ROOT_TEMP.fullmatch(path.name)
        )
        if len(candidates) > MAXIMUM_GENERATION_GC_ROOT_TEMPS:
            raise RuntimeError("generation GC root temporary count exceeds its cap")
        if not candidates:
            return
        removed = False
        root_entries = tuple(sorted(self.generation_gc_roots.iterdir(), key=lambda item: item.name))
        with state_quota_transaction(self.root):
            for path in candidates:
                if path.is_symlink() or not path.is_file():
                    raise RuntimeError("generation GC root temporary is unsafe")
                metadata = path.stat(follow_symlinks=False)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_size > MAXIMUM_GENERATION_GC_ROOT_BYTES
                    or metadata.st_nlink not in {1, 2}
                ):
                    raise RuntimeError("generation GC root temporary is unsafe")
                if metadata.st_nlink == 2:
                    companions: list[Path] = []
                    for candidate in root_entries:
                        if candidate.is_symlink() or not candidate.is_file():
                            continue
                        candidate_metadata = candidate.stat(follow_symlinks=False)
                        if (
                            candidate_metadata.st_dev,
                            candidate_metadata.st_ino,
                        ) == (metadata.st_dev, metadata.st_ino):
                            companions.append(candidate)
                    if len(companions) != 1:
                        raise RuntimeError("generation GC root temporary link is ambiguous")
                    self._read_generation_gc_root(companions[0], allowed_links=(2,))
                path.unlink()
                removed = True
            if removed:
                _fsync_directory(self.incoming)

    def _durable_generation_roots_locked(self) -> dict[str, str]:
        self._reconcile_generation_gc_root_temporaries_locked()
        root = self.generation_gc_roots
        if root.is_symlink() or not root.is_dir() or root.resolve(strict=True) != root:
            raise RuntimeError("generation GC root directory is unsafe")
        roots: dict[str, str] = {}
        for path in sorted(root.iterdir(), key=lambda item: item.name):
            payload = self._read_generation_gc_root(path)
            owner_id = payload["owner_id"]
            generation_id = payload["generation_id"]
            generation_path = self.generations / generation_id
            if generation_path.is_symlink() or not generation_path.is_dir():
                raise RuntimeError("generation GC root references a missing generation")
            try:
                resolved = generation_path.resolve(strict=True)
            except OSError as exc:
                raise RuntimeError("generation GC root generation is unreadable") from exc
            if resolved.parent != self.generations.resolve():
                raise RuntimeError("generation GC root escaped the generation directory")
            roots[owner_id] = generation_id
        return roots

    def _prune_locked(self, durable_roots: dict[str, str] | None = None) -> None:
        roots = self._durable_generation_roots_locked() if durable_roots is None else durable_roots
        active_id = self._active.handle.generation_id if self._active is not None else None
        candidates: list[tuple[int, str, Path]] = []
        for path in self.generations.iterdir():
            if not path.is_dir() or path.is_symlink() or not GENERATION_ID.fullmatch(path.name):
                continue
            candidates.append((path.stat().st_mtime_ns, path.name, path))
        candidates.sort(reverse=True)
        retained = {name for _, name, _ in candidates[: self.retention]}
        if active_id is not None:
            retained.add(active_id)
        retained.update(roots.values())
        for _, generation_id, path in candidates:
            entry = self._entries.get(generation_id)
            pinned = entry is not None and entry.references > 0
            if entry is not None and entry is not self._active and not pinned:
                # Disk rollback retention and in-memory request retention are
                # intentionally separate. An inactive handle still owns its
                # legacy matrix or v5 norms/mmap until all request pins drain.
                self._entries.pop(generation_id, None)
                entry = None
            if generation_id in retained or pinned:
                continue
            resolved = path.resolve()
            if resolved.parent != self.generations.resolve():
                continue
            shutil.rmtree(resolved)
            self._entries.pop(generation_id, None)
        self._prune_cas_locked()

    def _prune_cas_locked(self) -> None:
        """Mark from every retained/pinned DB; abort rather than guess on read failure."""

        referenced: set[str] = set()
        try:
            for path in self.generations.iterdir():
                if not path.is_dir() or path.is_symlink() or not GENERATION_ID.fullmatch(path.name):
                    continue
                with readonly_connection(path / "index.sqlite3") as connection:
                    schema_row = connection.execute(
                        "SELECT value FROM metadata WHERE key='schema_id'"
                    ).fetchone()
                    if schema_row is not None and str(schema_row[0]) == "cardrag.serving-db.v5":
                        referenced.update(
                            str(row[0])
                            for row in connection.execute(
                                "SELECT pdf_sha256 FROM contract_revisions"
                            )
                        )
                    else:
                        referenced.update(
                            str(row[0])
                            for row in connection.execute("SELECT pdf_sha256 FROM documents")
                        )
                    if (
                        connection.execute(
                            "SELECT 1 FROM sqlite_schema "
                            "WHERE type='table' AND name='ocr_failed_products'"
                        ).fetchone()
                        is not None
                    ):
                        referenced.update(
                            str(row[0])
                            for row in connection.execute(
                                "SELECT pdf_sha256 FROM ocr_failed_products"
                            )
                        )
        except Exception:
            return
        sha_root = self.objects / "sha256"
        if not sha_root.exists() or sha_root.is_symlink():
            return
        for prefix in sha_root.iterdir():
            if (
                not prefix.is_dir()
                or prefix.is_symlink()
                or not re.fullmatch(r"[0-9a-f]{2}", prefix.name)
            ):
                continue
            for item in prefix.iterdir():
                if item.is_symlink() or not item.is_file() or not SHA256.fullmatch(item.name):
                    continue
                if item.name not in referenced:
                    item.unlink()
            with contextlib.suppress(OSError):
                prefix.rmdir()

    def verify_pdf(self, document: Document, *, maximum_bytes: int) -> Path:
        with self.pin() as handle:
            return self.verify_pdf_for_handle(handle, document, maximum_bytes=maximum_bytes)

    def verify_pdf_for_handle(
        self,
        handle: GenerationHandle,
        document: Document,
        *,
        maximum_bytes: int,
    ) -> Path:
        if document.pdf_size_bytes > maximum_bytes:
            raise RuntimeError("PDF exceeds the configured 100 MiB bound")
        path = handle.pdf_path(document)
        if path.stat().st_size != document.pdf_size_bytes:
            raise RuntimeError("PDF size differs from serving metadata")
        digest = hashlib.sha256()
        with path.open("rb") as source:
            if source.read(5) != b"%PDF-":
                raise RuntimeError("source object is not application/pdf")
            source.seek(0)
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
        if digest.hexdigest() != document.pdf_sha256:
            raise RuntimeError("PDF hash differs from serving metadata")
        return path

    def verify_handle_pdfs(self, handle: GenerationHandle) -> None:
        with handle.connect() as connection:
            if handle.metadata.schema_id == "cardrag.serving-db.v5":
                sql = """SELECT r.document_id,l.issuer,l.product_code,l.name AS title,
                                 r.pdf_sha256,r.pdf_size_bytes,r.page_count
                            FROM contract_revisions AS r
                            JOIN product_lineages AS l
                              ON l.product_lineage_id=r.product_lineage_id"""
            else:
                sql = "SELECT * FROM documents"
            if handle.metadata.schema_id in {
                "cardrag.serving-db.v4",
                "cardrag.serving-db.v5",
            }:
                sql += """ UNION ALL
                    SELECT document_id,issuer,product_code,title,pdf_sha256,
                           pdf_size_bytes,page_count FROM ocr_failed_products"""
            rows = connection.execute(sql + " ORDER BY document_id").fetchall()
        for row in rows:
            self.verify_pdf_for_handle(
                handle,
                Document(**dict(row)),
                maximum_bytes=self.maximum_pdf_bytes,
            )
