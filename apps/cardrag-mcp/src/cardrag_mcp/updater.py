"""Fail-closed WebDAV generation synchronization and atomic activation."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from cardrag_core import EMBEDDING_DIMENSION

from cardrag_mcp.observability import Metrics, log_event
from cardrag_mcp.store import (
    GENERATION_ID,
    GenerationHandle,
    GenerationStore,
    cas_path,
    load_generation_handle,
)

logger = logging.getLogger("cardrag_mcp.updater")
VECTOR_ROW_RESIDENT_BYTES = EMBEDDING_DIMENSION * 4 + 4  # float32 vector plus its float32 norm


@dataclass(frozen=True, slots=True)
class RemoteArtifact:
    path: str
    sha256: str
    size_bytes: int
    media_type: str


@dataclass(frozen=True, slots=True)
class RemoteDocument:
    document_id: str
    issuer: str
    page_count: int
    pdf: RemoteArtifact
    ocr_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class RemoteGeneration:
    generation_id: str
    serving_schema: str
    corpus_sha256: str
    contract_sha256: str
    database: RemoteArtifact
    documents: tuple[RemoteDocument, ...]
    issuer_codes: tuple[str, ...]
    document_count: int
    pdf_object_count: int
    ocr_object_count: int
    chunk_count: int
    embedding_provider: str
    embedding_model: str
    embedding_dimension: int
    embedding_count: int


class ArtifactReader(Protocol):
    async def read_stable_generation(self) -> RemoteGeneration | None: ...

    async def download_database(self, generation: RemoteGeneration, destination: Path) -> None: ...

    async def download_object(self, artifact: RemoteArtifact, destination: Path) -> None: ...


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _verify_artifact(
    path: Path,
    artifact: RemoteArtifact,
    *,
    maximum_bytes: int | None = None,
) -> None:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("downloaded artifact is missing or unsafe")
    if maximum_bytes is not None and artifact.size_bytes > maximum_bytes:
        raise RuntimeError("artifact exceeds its configured size bound")
    digest, size = _hash_file(path)
    if digest != artifact.sha256 or size != artifact.size_bytes:
        raise RuntimeError("downloaded artifact hash or size differs from its manifest")


def _fsync_file(path: Path) -> None:
    with path.open("rb") as source:
        os.fsync(source.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _verify_pdf_artifact(path: Path, artifact: RemoteArtifact, maximum_bytes: int) -> None:
    _verify_artifact(path, artifact, maximum_bytes=maximum_bytes)
    with path.open("rb") as source:
        if source.read(5) != b"%PDF-":
            raise RuntimeError("referenced CAS object is not application/pdf")


class WebDAVUpdater:
    def __init__(
        self,
        reader: ArtifactReader,
        store: GenerationStore,
        metrics: Metrics,
        *,
        poll_seconds: float = 300,
        maximum_pdf_bytes: int = 100 * 1024 * 1024,
    ) -> None:
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        self.reader = reader
        self.store = store
        self.metrics = metrics
        self.poll_seconds = poll_seconds
        self.maximum_pdf_bytes = maximum_pdf_bytes
        self._poll_lock = asyncio.Lock()

    async def poll_once(self) -> bool:
        """Promote one fully verified generation; leave last-good on every failure."""

        async with self._poll_lock:
            generation = await self.reader.read_stable_generation()
            if generation is None:
                raise RuntimeError("stable WebDAV channel is absent")
            if not GENERATION_ID.fullmatch(generation.generation_id):
                raise RuntimeError("remote generation ID is unsafe")
            self._verify_remote_manifest_contract(generation)
            if self.store.active_generation_id == generation.generation_id:
                # A no-op channel poll is also the repair loop for the active
                # generation's local CAS. Request handlers never reach out to
                # WebDAV, so a deleted or corrupt local PDF must be restored
                # here before the poll is reported as healthy/unchanged.
                with self.store.pin() as active_handle:
                    await asyncio.to_thread(
                        self._verify_generation_binding,
                        active_handle,
                        generation,
                    )
                    for document in generation.documents:
                        await self._sync_pdf(document.pdf)
                self.metrics.updates.labels(outcome="unchanged").inc()
                self.metrics.update_age_seconds.set(time.time())
                return False
            await self._stage_and_activate(generation)
            self.metrics.updates.labels(outcome="activated").inc()
            self.metrics.update_age_seconds.set(time.time())
            self.metrics.ready.set(1)
            log_event(logger, "generation.activated", generation_id=generation.generation_id)
            return True

    async def _stage_and_activate(self, generation: RemoteGeneration) -> None:
        if generation.embedding_dimension != EMBEDDING_DIMENSION:
            raise RuntimeError(f"remote embedding dimension is not {EMBEDDING_DIMENSION}")
        if generation.database.media_type != "application/vnd.sqlite3":
            raise RuntimeError("remote serving database media type is invalid")
        final = self.store.generations / generation.generation_id
        if final.exists():
            existing_handle = await asyncio.to_thread(
                self._validated_handle,
                final,
                generation,
            )
            # A restart may find a completely sealed SQLite generation while
            # its shared local PDF cache was lost independently. Rebuild that
            # cache before activation just as for a freshly downloaded DB.
            for document in generation.documents:
                await self._sync_pdf(document.pdf)
            await asyncio.to_thread(self.store.verify_handle_pdfs, existing_handle)
            self.store.activate(existing_handle)
            return
        staging = self.store.incoming / generation.generation_id
        if staging.exists():
            resolved = staging.resolve()
            if resolved.parent != self.store.incoming.resolve():
                raise RuntimeError("incoming generation path escaped staging root")
            shutil.rmtree(resolved)
        staging.mkdir(mode=0o700)
        database = staging / "index.sqlite3"
        try:
            await self.reader.download_database(generation, database)
            await asyncio.to_thread(_verify_artifact, database, generation.database)
            await asyncio.to_thread(_fsync_file, database)
            staged_handle = await asyncio.to_thread(
                self._validated_handle,
                staging,
                generation,
            )
            for document in generation.documents:
                await self._sync_pdf(document.pdf)
            await asyncio.to_thread(self.store.verify_handle_pdfs, staged_handle)

            if final.exists():
                raise RuntimeError("immutable local generation already exists but is not active")
            os.replace(staging, final)
            _fsync_directory(self.store.generations)
            final_handle = replace(
                staged_handle,
                directory=final.resolve(),
                database_path=(final / "index.sqlite3").resolve(),
            )
            self.store.activate(final_handle)
        except BaseException:
            if staging.exists() and staging.resolve().parent == self.store.incoming.resolve():
                shutil.rmtree(staging)
            raise

    @staticmethod
    def _verify_remote_manifest_contract(generation: RemoteGeneration) -> None:
        """Defend the updater protocol even when its reader is not the core facade."""

        if generation.issuer_codes != tuple(sorted(set(generation.issuer_codes))):
            raise RuntimeError("generation manifest issuer codes are not sorted and unique")
        if generation.document_count != len(generation.documents):
            raise RuntimeError("generation manifest document count differs from its documents")
        if generation.pdf_object_count != len(
            {document.pdf.sha256 for document in generation.documents}
        ):
            raise RuntimeError("generation manifest PDF object count differs from its documents")
        if generation.ocr_object_count != len(
            {
                document.ocr_sha256
                for document in generation.documents
                if document.ocr_sha256 is not None
            }
        ):
            raise RuntimeError("generation manifest OCR object count differs from its documents")
        if generation.chunk_count != generation.embedding_count:
            raise RuntimeError("generation manifest chunk count differs from embedding count")
        document_issuers = {document.issuer for document in generation.documents}
        if not document_issuers.issubset(generation.issuer_codes):
            raise RuntimeError("generation document references an undeclared issuer")

    def _validated_handle(
        self,
        directory: Path,
        generation: RemoteGeneration,
    ) -> GenerationHandle:
        database = directory / "index.sqlite3"
        _verify_artifact(database, generation.database)
        candidate_vector_bytes = generation.embedding_count * VECTOR_ROW_RESIDENT_BYTES
        available_vector_bytes = self.store.maximum_vector_bytes - self.store.resident_vector_bytes
        if candidate_vector_bytes > available_vector_bytes:
            raise RuntimeError(
                "candidate plus resident/pinned vector memory exceeds the promotion limit"
            )
        handle = load_generation_handle(
            directory,
            self.store.objects,
            maximum_vector_bytes=self.store.maximum_vector_bytes,
            expected_generation_id=generation.generation_id,
            expected_embedding_model=generation.embedding_model,
            expected_embedding_count=generation.embedding_count,
        )
        self._verify_generation_binding(handle, generation)
        return handle

    def _verify_generation_binding(
        self,
        handle: GenerationHandle,
        generation: RemoteGeneration,
    ) -> None:
        metadata = handle.metadata
        if (
            metadata.schema_id != generation.serving_schema
            or metadata.generation_id != generation.generation_id
            or metadata.corpus_sha256 != generation.corpus_sha256
            or metadata.contract_sha256 != generation.contract_sha256
            or metadata.embedding_provider != generation.embedding_provider
            or metadata.embedding_model != generation.embedding_model
            or metadata.embedding_dimension != generation.embedding_dimension
            or metadata.embedding_count != generation.embedding_count
        ):
            raise RuntimeError("database contract differs from generation manifest")
        documents, counts, issuer_codes = self._database_contract(handle)
        declared = {item.document_id: item for item in generation.documents}
        if len(declared) != len(generation.documents):
            raise RuntimeError("generation manifest contains duplicate document IDs")
        if set(declared) != {item[0] for item in documents}:
            raise RuntimeError("manifest and database document sets differ")
        for document_id, issuer, page_count, pdf_sha256, pdf_size_bytes in documents:
            manifest_document = declared[document_id]
            artifact = manifest_document.pdf
            if (
                manifest_document.issuer != issuer
                or manifest_document.page_count != page_count
                or artifact.sha256 != pdf_sha256
                or artifact.size_bytes != pdf_size_bytes
                or artifact.media_type != "application/pdf"
            ):
                raise RuntimeError("manifest and database PDF references differ")
        expected_counts = (
            len(generation.issuer_codes),
            generation.document_count,
            generation.document_count,
            sum(document.page_count for document in generation.documents),
            generation.chunk_count,
            generation.pdf_object_count,
        )
        if issuer_codes != generation.issuer_codes or counts != expected_counts:
            raise RuntimeError("manifest and database corpus counts differ")

    @staticmethod
    def _database_contract(
        handle: GenerationHandle,
    ) -> tuple[
        list[tuple[str, str, int, str, int]],
        tuple[int, int, int, int, int, int],
        tuple[str, ...],
    ]:
        with handle.connect() as connection:
            documents = [
                (str(row[0]), str(row[1]), int(row[2]), str(row[3]), int(row[4]))
                for row in connection.execute(
                    "SELECT document_id,issuer,page_count,pdf_sha256,pdf_size_bytes "
                    "FROM documents ORDER BY document_id"
                )
            ]
            issuer_codes = tuple(
                str(row[0]) for row in connection.execute("SELECT code FROM issuers ORDER BY code")
            )
            raw_counts = connection.execute(
                """
                SELECT
                  (SELECT count(*) FROM issuers),
                  (SELECT count(*) FROM products),
                  (SELECT count(*) FROM documents),
                  (SELECT count(*) FROM pages),
                  (SELECT count(*) FROM evidence),
                  (SELECT count(DISTINCT pdf_sha256) FROM documents)
                """
            ).fetchone()
            if raw_counts is None:
                raise RuntimeError("serving database corpus counts are unavailable")
            counts = (
                int(raw_counts[0]),
                int(raw_counts[1]),
                int(raw_counts[2]),
                int(raw_counts[3]),
                int(raw_counts[4]),
                int(raw_counts[5]),
            )
            return documents, counts, issuer_codes

    async def _sync_pdf(self, artifact: RemoteArtifact) -> None:
        if artifact.size_bytes > self.maximum_pdf_bytes:
            raise RuntimeError("referenced PDF exceeds 100 MiB")
        destination = cas_path(self.store.objects, artifact.sha256)
        replace_corrupt = False
        if destination.exists():
            try:
                await asyncio.to_thread(
                    _verify_pdf_artifact,
                    destination,
                    artifact,
                    self.maximum_pdf_bytes,
                )
            except RuntimeError:
                replace_corrupt = True
            else:
                return
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{artifact.sha256}.", suffix=".part", dir=self.store.incoming
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            await self.reader.download_object(artifact, temporary)
            await asyncio.to_thread(
                _verify_pdf_artifact,
                temporary,
                artifact,
                self.maximum_pdf_bytes,
            )
            await asyncio.to_thread(_fsync_file, temporary)
            if replace_corrupt:
                os.replace(temporary, destination)
            else:
                try:
                    os.link(temporary, destination)
                except FileExistsError:
                    await asyncio.to_thread(
                        _verify_pdf_artifact,
                        destination,
                        artifact,
                        self.maximum_pdf_bytes,
                    )
            _fsync_directory(destination.parent)
        finally:
            await asyncio.to_thread(temporary.unlink, missing_ok=True)

    async def run_forever(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.metrics.updates.labels(outcome="failed").inc()
                self.metrics.ready.set(1 if self.store.active_generation_id is not None else 0)
                log_event(
                    logger,
                    "generation.update_failed",
                    error_type=type(exc).__name__,
                    last_good_available=self.store.active_generation_id is not None,
                )
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.poll_seconds)
            except TimeoutError:
                pass

    async def close(self) -> None:
        closer = getattr(self.reader, "close", None)
        if closer is None:
            return
        result = closer()
        if asyncio.iscoroutine(result):
            await result
