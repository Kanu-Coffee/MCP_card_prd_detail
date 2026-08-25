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

from cardrag_mcp.models import Document, ServingMetadata
from cardrag_mcp.schema import LoadedVectors, load_vectors, readonly_connection, validate_schema

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
    vectors: LoadedVectors

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
    expected_generation_id: str | None = None,
    expected_embedding_model: str | None = None,
    expected_embedding_count: int | None = None,
) -> GenerationHandle:
    directory = directory.resolve(strict=True)
    generation_id = directory.name
    if not GENERATION_ID.fullmatch(generation_id):
        raise RuntimeError("invalid local generation ID")
    database_path = directory / "index.sqlite3"
    if database_path.is_symlink() or not database_path.is_file():
        raise RuntimeError("generation database is missing or unsafe")
    with readonly_connection(database_path) as connection:
        metadata = validate_schema(
            connection,
            maximum_vector_bytes=maximum_vector_bytes,
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
        vectors = load_vectors(
            connection,
            expected_count=metadata.embedding_count,
            maximum_bytes=maximum_vector_bytes,
        )
    return GenerationHandle(
        generation_id=generation_id,
        directory=directory,
        database_path=database_path,
        object_root=object_root.resolve(),
        metadata=metadata,
        vectors=vectors,
    )


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
        maximum_pdf_bytes: int = 100 * 1024 * 1024,
        retention: int = 3,
    ) -> None:
        if retention < 3:
            raise ValueError("local retention must be at least three")
        self.root = root.resolve()
        self.generations = self.root / "generations"
        self.objects = self.root / "objects"
        self.incoming = self.root / ".incoming"
        self.current_path = self.root / "current.json"
        self.maximum_vector_bytes = maximum_vector_bytes
        self.maximum_pdf_bytes = maximum_pdf_bytes
        self.retention = retention
        for path in (self.root, self.generations, self.objects, self.incoming):
            path.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._active: _HandleEntry | None = None
        self._entries: dict[str, _HandleEntry] = {}

    @property
    def active_generation_id(self) -> str | None:
        with self._lock:
            return self._active.handle.generation_id if self._active is not None else None

    @property
    def resident_vector_bytes(self) -> int:
        """Return actual NumPy vector/norm bytes held by active or pinned handles."""

        with self._lock:
            return sum(
                entry.handle.vectors.matrix.nbytes + entry.handle.vectors.norms.nbytes
                for entry in self._entries.values()
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
        _atomic_json(
            self.current_path,
            {
                "schema_version": LOCAL_POINTER_SCHEMA,
                "generation_id": handle.generation_id,
            },
        )
        with self._lock:
            entry = self._entries.get(handle.generation_id)
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
                # intentionally separate. Keeping an inactive handle here also
                # keeps its entire NumPy embedding matrix resident, which could
                # multiply the 1 GiB per-generation promotion cap by three.
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
                    referenced.update(
                        str(row[0])
                        for row in connection.execute("SELECT pdf_sha256 FROM documents")
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
            rows = connection.execute("SELECT * FROM documents ORDER BY document_id").fetchall()
        for row in rows:
            self.verify_pdf_for_handle(
                handle,
                Document(**dict(row)),
                maximum_bytes=self.maximum_pdf_bytes,
            )
