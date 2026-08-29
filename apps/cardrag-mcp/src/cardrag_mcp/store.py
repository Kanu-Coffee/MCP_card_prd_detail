"""Atomic local generation activation with request-scoped handle pinning."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from cardrag_mcp.models import Document, ServingMetadata
from cardrag_mcp.quota import (
    DEFAULT_EXHAUSTIVE_AUDIT_MAX_ARTIFACT_BYTES,
    DEFAULT_EXHAUSTIVE_AUDIT_MAX_JOBS,
    DEFAULT_EXHAUSTIVE_AUDIT_MAX_TOTAL_BYTES,
    DEFAULT_MAX_STATE_BYTES,
    DEFAULT_RERANKER_AUDIT_MAX_ARTIFACT_BYTES,
    DEFAULT_RERANKER_AUDIT_MAX_JOBS,
    DEFAULT_RERANKER_AUDIT_MAX_TOTAL_BYTES,
    DEFAULT_RESERVED_FREE_SPACE_BYTES,
    StateQuotaPolicy,
    StateQuotaReservation,
    configure_state_quota,
    ensure_global_state_growth,
    reserve_global_state_growth,
    state_quota_guard,
    validate_byte_limit,
    validate_count_limit,
)
from cardrag_mcp.schema import LoadedVectors, load_vectors, readonly_connection, validate_schema
from cardrag_mcp.schema_v5 import LoadedVectorsV5, load_vectors_v5, validate_schema_v5

GENERATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
LOCAL_POINTER_SCHEMA = "cardrag.mcp-local-pointer.v1"


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
    maximum_database_bytes: int = 4 * 1024 * 1024 * 1024,
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
        maximum_database_bytes: int = 4 * 1024 * 1024 * 1024,
        maximum_generation_download_bytes: int = 32 * 1024 * 1024 * 1024,
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
        self.maximum_vector_bytes = maximum_vector_bytes
        self.maximum_vector_sidecar_bytes = sidecar_limit
        self.maximum_resident_vector_bytes = resident_limit
        self.maximum_pdf_bytes = maximum_pdf_bytes
        self.maximum_database_bytes = maximum_database_bytes
        self.maximum_generation_download_bytes = maximum_generation_download_bytes
        self.maximum_state_bytes = maximum_state_bytes
        self.reserved_free_space_bytes = reserved_free_space_bytes
        self.retention = retention
        for path in (self.root, self.generations, self.objects, self.incoming):
            path.mkdir(parents=True, exist_ok=True)
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
        self._active: _HandleEntry | None = None
        self._entries: dict[str, _HandleEntry] = {}

    @property
    def active_generation_id(self) -> str | None:
        with self._lock:
            return self._active.handle.generation_id if self._active is not None else None

    @property
    def resident_vector_bytes(self) -> int:
        """Return heap-backed legacy matrices and norm arrays for resident handles."""

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
        with self._lock:
            if self._active is None:
                raise RuntimeError("no serving generation is active")
            entry = self._active
            entry.references += 1
        try:
            yield entry.handle
        finally:
            with self._lock:
                entry.references -= 1
                if entry is not self._active and entry.references == 0:
                    self._prune_locked()

    def activate(self, handle: GenerationHandle) -> None:
        if handle.directory.parent.resolve() != self.generations.resolve():
            raise ValueError("generation is outside the local generation root")
        with self._lock:
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
            self._prune_locked()

    def _prune_locked(self) -> None:
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
