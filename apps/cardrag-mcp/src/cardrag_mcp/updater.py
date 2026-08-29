"""Fail-closed WebDAV generation synchronization and atomic activation."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from cardrag_core import (
    EMBEDDING_DIMENSION,
    QWEN3_EMBEDDING_DIMENSION,
    DocumentAggregationPolicy,
    DocumentAggregationProfile,
    EmbeddingProfile,
    EmbeddingVectorSidecar,
    EmbeddingViewCount,
    StructureContract,
    canonical_sha256,
    generation_database_path,
    generation_vectors_path,
    object_path,
    sealed_v5_retrieval_policy,
)

from cardrag_mcp.observability import Metrics, log_event
from cardrag_mcp.quota import MAX_SAFE_BYTES, checked_add, validate_byte_limit
from cardrag_mcp.store import (
    GENERATION_ID,
    GenerationHandle,
    GenerationStore,
    cas_path,
    load_generation_handle,
)

logger = logging.getLogger("cardrag_mcp.updater")
VECTOR_ROW_RESIDENT_BYTES = EMBEDDING_DIMENSION * 4 + 4
VECTOR_NORM_RESIDENT_BYTES = 4
SCHEMA_EMBEDDING_DIMENSIONS = {
    "cardrag.serving-db.v2": EMBEDDING_DIMENSION,
    "cardrag.serving-db.v3": EMBEDDING_DIMENSION,
    "cardrag.serving-db.v4": EMBEDDING_DIMENSION,
    "cardrag.serving-db.v5": QWEN3_EMBEDDING_DIMENSION,
}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


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
    availability: str = "available"
    failure_reason_code: str | None = None
    failure_reason: str | None = None
    failure_attempts: int | None = None


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
    issuer_ocr_counts: tuple[tuple[str, int, int, int], ...] = ()
    vector_sidecar: RemoteArtifact | None = None
    structure_contract: StructureContract | None = None
    embedding_profiles: tuple[EmbeddingProfile, ...] = ()
    primary_embedding_profile_id: str | None = None
    embedding_view_counts: tuple[EmbeddingViewCount, ...] = ()
    vector_sidecar_contract: EmbeddingVectorSidecar | None = None
    parser_policy_sha256: str | None = None
    embedding_policy_sha256: str | None = None
    retrieval_policy_sha256: str | None = None
    document_aggregation_profile: DocumentAggregationProfile | None = None
    document_aggregation_policy: DocumentAggregationPolicy | None = None
    sealed_profile_sha256: str | None = None
    exact_row_corpus_sha256: str | None = None


class ArtifactReader(Protocol):
    async def read_stable_generation(self) -> RemoteGeneration | None: ...

    async def download_database(self, generation: RemoteGeneration, destination: Path) -> None: ...

    async def download_vector_sidecar(
        self,
        generation: RemoteGeneration,
        destination: Path,
    ) -> None: ...

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
        maximum_database_bytes: int | None = None,
        maximum_generation_download_bytes: int | None = None,
    ) -> None:
        if (
            isinstance(poll_seconds, bool)
            or not isinstance(poll_seconds, (int, float))
            or poll_seconds <= 0
        ):
            raise ValueError("poll_seconds must be positive")
        database_limit = (
            store.maximum_database_bytes
            if maximum_database_bytes is None
            else maximum_database_bytes
        )
        generation_limit = (
            store.maximum_generation_download_bytes
            if maximum_generation_download_bytes is None
            else maximum_generation_download_bytes
        )
        validate_byte_limit(maximum_pdf_bytes, label="maximum PDF bytes")
        validate_byte_limit(database_limit, label="maximum serving database bytes")
        validate_byte_limit(generation_limit, label="maximum generation download bytes")
        if database_limit > generation_limit or maximum_pdf_bytes > generation_limit:
            raise ValueError("artifact cap exceeds the generation download quota")
        self.reader = reader
        self.store = store
        self.metrics = metrics
        self.poll_seconds = poll_seconds
        self.maximum_pdf_bytes = maximum_pdf_bytes
        self.maximum_database_bytes = database_limit
        self.maximum_generation_download_bytes = generation_limit
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
            self._verify_remote_storage_contract(generation)
            candidate_growth = await asyncio.to_thread(
                self._prospective_state_growth,
                generation,
            )
            if candidate_growth:
                await asyncio.to_thread(
                    self.store.ensure_state_capacity,
                    candidate_growth,
                )
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
            self._verify_candidate_capacity(generation)
            await self._stage_and_activate(generation)
            self.metrics.updates.labels(outcome="activated").inc()
            self.metrics.update_age_seconds.set(time.time())
            self.metrics.ready.set(1)
            log_event(logger, "generation.activated", generation_id=generation.generation_id)
            return True

    async def _stage_and_activate(self, generation: RemoteGeneration) -> None:
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
        vectors = staging / "vectors.f32"
        try:
            database_reservation = await asyncio.to_thread(
                self.store.reserve_state_capacity,
                generation.database.size_bytes,
            )
            try:
                await self.reader.download_database(generation, database)
                await asyncio.to_thread(
                    _verify_artifact,
                    database,
                    generation.database,
                    maximum_bytes=self.maximum_database_bytes,
                )
                await asyncio.to_thread(_fsync_file, database)
            finally:
                await asyncio.to_thread(database_reservation.release)
            if generation.vector_sidecar is not None:
                sidecar_reservation = await asyncio.to_thread(
                    self.store.reserve_state_capacity,
                    generation.vector_sidecar.size_bytes,
                )
                try:
                    await self.reader.download_vector_sidecar(generation, vectors)
                    await asyncio.to_thread(
                        _verify_artifact,
                        vectors,
                        generation.vector_sidecar,
                        maximum_bytes=self.store.maximum_vector_sidecar_bytes,
                    )
                    await asyncio.to_thread(_fsync_file, vectors)
                finally:
                    await asyncio.to_thread(sidecar_reservation.release)
            await asyncio.to_thread(_fsync_directory, staging)
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
                vector_sidecar_path=(
                    None
                    if staged_handle.vector_sidecar_path is None
                    else (final / "vectors.f32").resolve(strict=True)
                ),
            )
            self.store.activate(final_handle)
        except BaseException:
            if staging.exists() and staging.resolve().parent == self.store.incoming.resolve():
                shutil.rmtree(staging)
            raise

    @staticmethod
    def _verify_remote_manifest_contract(generation: RemoteGeneration) -> None:
        """Defend the updater protocol even when its reader is not the core facade."""

        declared_counts = (
            generation.document_count,
            generation.pdf_object_count,
            generation.ocr_object_count,
            generation.chunk_count,
            generation.embedding_count,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in declared_counts
        ):
            raise RuntimeError("generation manifest contains an invalid count")
        expected_dimension = SCHEMA_EMBEDDING_DIMENSIONS.get(generation.serving_schema)
        if expected_dimension is None:
            raise RuntimeError("generation serving schema is unsupported")
        if generation.embedding_dimension != expected_dimension:
            raise RuntimeError("generation embedding dimension does not match its serving schema")
        if (
            generation.database.path
            != generation_database_path(generation.generation_id).as_posix()
            or generation.database.media_type != "application/vnd.sqlite3"
            or _SHA256.fullmatch(generation.database.sha256) is None
            or isinstance(generation.database.size_bytes, bool)
            or not isinstance(generation.database.size_bytes, int)
            or generation.database.size_bytes < 1
            or generation.database.size_bytes > MAX_SAFE_BYTES
        ):
            raise RuntimeError("generation serving database artifact is invalid")
        sidecar = generation.vector_sidecar
        if generation.serving_schema == "cardrag.serving-db.v5":
            if sidecar is None:
                raise RuntimeError("v5 generation requires a vector sidecar")
            if (
                sidecar.path != generation_vectors_path(generation.generation_id).as_posix()
                or sidecar.media_type != "application/octet-stream"
                or _SHA256.fullmatch(sidecar.sha256) is None
                or isinstance(sidecar.size_bytes, bool)
                or not isinstance(sidecar.size_bytes, int)
                or sidecar.size_bytes != generation.embedding_count * QWEN3_EMBEDDING_DIMENSION * 4
                or sidecar.size_bytes > MAX_SAFE_BYTES
            ):
                raise RuntimeError("v5 vector sidecar artifact is invalid")
            structure = generation.structure_contract
            sidecar_contract = generation.vector_sidecar_contract
            profiles = generation.embedding_profiles
            policies = (
                generation.parser_policy_sha256,
                generation.embedding_policy_sha256,
                generation.retrieval_policy_sha256,
            )
            if (
                structure is None
                or sidecar_contract is None
                or not profiles
                or generation.primary_embedding_profile_id is None
                or not generation.embedding_view_counts
                or any(value is None or _SHA256.fullmatch(value) is None for value in policies)
            ):
                raise RuntimeError("v5 generation semantic manifest contract is incomplete")
            profiles_by_id = {profile.profile_id: profile for profile in profiles}
            primary = profiles_by_id.get(generation.primary_embedding_profile_id)
            if (
                len(profiles_by_id) != len(profiles)
                or primary is None
                or tuple(profile.issuer for profile in structure.parser_profiles)
                != generation.issuer_codes
                or structure.revision_counts.total
                != sum(document.availability == "available" for document in generation.documents)
                or structure.major_class_counts.total != structure.node_counts.major_section
                or primary.provider != generation.embedding_provider
                or primary.model != generation.embedding_model
                or primary.dimension != generation.embedding_dimension
                or sidecar_contract.profile_id != primary.profile_id
                or sidecar_contract.row_count != generation.embedding_count
                or sidecar_contract.dimension != generation.embedding_dimension
                or sidecar_contract.dtype != primary.dtype
                or sidecar_contract.normalization != primary.normalization
                or sidecar_contract.byte_order != "little-endian"
                or sidecar_contract.layout != "row-major"
                or sum(row.count for row in generation.embedding_view_counts)
                != generation.embedding_count
                or (
                    sidecar_contract.artifact.path,
                    sidecar_contract.artifact.sha256,
                    sidecar_contract.artifact.size_bytes,
                    sidecar_contract.artifact.media_type,
                )
                != (sidecar.path, sidecar.sha256, sidecar.size_bytes, sidecar.media_type)
            ):
                raise RuntimeError("v5 generation semantic manifest contract is inconsistent")
            aggregation_presence = (
                generation.document_aggregation_profile is not None,
                generation.document_aggregation_policy is not None,
                generation.sealed_profile_sha256 is not None,
                generation.exact_row_corpus_sha256 is not None,
            )
            if len(set(aggregation_presence)) != 1:
                raise RuntimeError("v5 document aggregation manifest identity is all-or-nothing")
            aggregation_profile = generation.document_aggregation_profile
            if aggregation_profile is not None:
                aggregation_profile_sha256 = generation.sealed_profile_sha256
                if (
                    aggregation_profile_sha256 is None
                    or aggregation_profile.profile_sha256 != aggregation_profile_sha256
                    or aggregation_profile.aggregation_policy
                    != generation.document_aggregation_policy
                    or aggregation_profile.embedding_profile_id != primary.profile_id
                    or aggregation_profile.exact_row_corpus_sha256
                    != generation.exact_row_corpus_sha256
                    or generation.retrieval_policy_sha256
                    != canonical_sha256(
                        sealed_v5_retrieval_policy(
                            aggregation_profile,
                            aggregation_profile_sha256,
                        )
                    )
                ):
                    raise RuntimeError("v5 document aggregation manifest identity is inconsistent")
        elif sidecar is not None:
            raise RuntimeError("legacy generation must not declare a vector sidecar")
        elif any(
            (
                generation.structure_contract is not None,
                bool(generation.embedding_profiles),
                generation.primary_embedding_profile_id is not None,
                bool(generation.embedding_view_counts),
                generation.vector_sidecar_contract is not None,
                generation.parser_policy_sha256 is not None,
                generation.embedding_policy_sha256 is not None,
                generation.retrieval_policy_sha256 is not None,
                generation.document_aggregation_profile is not None,
                generation.document_aggregation_policy is not None,
                generation.sealed_profile_sha256 is not None,
                generation.exact_row_corpus_sha256 is not None,
            )
        ):
            raise RuntimeError("legacy generation contains v5 semantic manifest fields")
        if generation.issuer_codes != tuple(sorted(set(generation.issuer_codes))):
            raise RuntimeError("generation manifest issuer codes are not sorted and unique")
        if generation.document_count != len(generation.documents):
            raise RuntimeError("generation manifest document count differs from its documents")
        pdf_by_digest: dict[str, RemoteArtifact] = {}
        for document in generation.documents:
            artifact = document.pdf
            if (
                _SHA256.fullmatch(artifact.sha256) is None
                or artifact.path != object_path(artifact.sha256).as_posix()
                or artifact.media_type != "application/pdf"
                or isinstance(artifact.size_bytes, bool)
                or not isinstance(artifact.size_bytes, int)
                or artifact.size_bytes < 1
                or artifact.size_bytes > MAX_SAFE_BYTES
            ):
                raise RuntimeError("generation manifest PDF artifact is invalid")
            existing = pdf_by_digest.setdefault(artifact.sha256, artifact)
            if existing != artifact:
                raise RuntimeError("generation manifest reuses a PDF digest with another identity")
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
        if generation.serving_schema in {"cardrag.serving-db.v4", "cardrag.serving-db.v5"}:
            schema_label = "v4" if generation.serving_schema == "cardrag.serving-db.v4" else "v5"
            if tuple(row[0] for row in generation.issuer_ocr_counts) != generation.issuer_codes:
                raise RuntimeError(f"{schema_label} generation issuer OCR counts are incomplete")
            for issuer, acquired, succeeded, failed in generation.issuer_ocr_counts:
                matching = [item for item in generation.documents if item.issuer == issuer]
                if (
                    acquired != len(matching)
                    or succeeded != sum(item.availability == "available" for item in matching)
                    or failed != sum(item.availability == "ocr_failed" for item in matching)
                    or succeeded < 1
                    or succeeded * 100 < acquired * 95
                ):
                    raise RuntimeError(f"{schema_label} generation issuer OCR counts are invalid")
            for document in generation.documents:
                failure_values = (
                    document.failure_reason_code,
                    document.failure_reason,
                    document.failure_attempts,
                )
                if document.availability == "available":
                    if document.ocr_sha256 is None or any(
                        value is not None for value in failure_values
                    ):
                        raise RuntimeError(
                            f"available {schema_label} document has an invalid OCR disposition"
                        )
                elif document.availability == "ocr_failed":
                    if (
                        document.ocr_sha256 is not None
                        or any(value is None for value in failure_values)
                        or re.fullmatch(r"[a-z0-9_]{1,64}", document.failure_reason_code or "")
                        is None
                        or not document.failure_reason
                        or len(document.failure_reason) > 256
                        or "\n" in document.failure_reason
                        or "\r" in document.failure_reason
                        or isinstance(document.failure_attempts, bool)
                        or not isinstance(document.failure_attempts, int)
                        or document.failure_attempts < 1
                    ):
                        raise RuntimeError(
                            f"OCR-failed {schema_label} document has an invalid disposition"
                        )
                else:
                    raise RuntimeError(f"{schema_label} document availability is invalid")
        elif generation.issuer_ocr_counts or any(
            document.availability != "available" for document in generation.documents
        ):
            raise RuntimeError("legacy generation contains v4 OCR dispositions")

    def _verify_remote_storage_contract(self, generation: RemoteGeneration) -> None:
        if generation.database.size_bytes > self.maximum_database_bytes:
            raise RuntimeError("remote serving database exceeds its configured hard cap")
        sidecar = generation.vector_sidecar
        if sidecar is not None and sidecar.size_bytes > self.store.maximum_vector_sidecar_bytes:
            raise RuntimeError("v5 vector sidecar exceeds the configured file limit")
        total = generation.database.size_bytes
        if sidecar is not None:
            total = checked_add(total, sidecar.size_bytes, label="generation download bytes")
        unique_pdfs: dict[str, RemoteArtifact] = {}
        for document in generation.documents:
            artifact = document.pdf
            if artifact.size_bytes > self.maximum_pdf_bytes:
                raise RuntimeError("referenced PDF exceeds its configured hard cap")
            unique_pdfs.setdefault(artifact.sha256, artifact)
        for artifact in unique_pdfs.values():
            total = checked_add(total, artifact.size_bytes, label="generation download bytes")
        if total > self.maximum_generation_download_bytes:
            raise RuntimeError("remote generation exceeds the aggregate download quota")

    def _prospective_state_growth(self, generation: RemoteGeneration) -> int:
        final = self.store.generations / generation.generation_id
        growth = 0
        if not final.exists():
            growth = generation.database.size_bytes
            if generation.vector_sidecar is not None:
                growth = checked_add(
                    growth,
                    generation.vector_sidecar.size_bytes,
                    label="prospective generation state bytes",
                )
        unique_pdfs = {document.pdf.sha256: document.pdf for document in generation.documents}
        for artifact in unique_pdfs.values():
            destination = cas_path(self.store.objects, artifact.sha256)
            try:
                _verify_pdf_artifact(destination, artifact, self.maximum_pdf_bytes)
            except RuntimeError:
                growth = checked_add(
                    growth,
                    artifact.size_bytes,
                    label="prospective generation state bytes",
                )
        return growth

    def _validated_handle(
        self,
        directory: Path,
        generation: RemoteGeneration,
    ) -> GenerationHandle:
        database = directory / "index.sqlite3"
        _verify_artifact(
            database,
            generation.database,
            maximum_bytes=self.maximum_database_bytes,
        )
        sidecar_path = directory / "vectors.f32"
        if generation.vector_sidecar is None:
            if sidecar_path.exists() or sidecar_path.is_symlink():
                raise RuntimeError("legacy local generation contains a forbidden vector sidecar")
        else:
            _verify_artifact(
                sidecar_path,
                generation.vector_sidecar,
                maximum_bytes=self.store.maximum_vector_sidecar_bytes,
            )
        self._verify_candidate_capacity(generation)
        handle = load_generation_handle(
            directory,
            self.store.objects,
            maximum_vector_bytes=self.store.maximum_vector_bytes,
            maximum_database_bytes=self.maximum_database_bytes,
            maximum_vector_sidecar_bytes=self.store.maximum_vector_sidecar_bytes,
            maximum_resident_vector_bytes=self.store.maximum_resident_vector_bytes,
            expected_generation_id=generation.generation_id,
            expected_embedding_model=generation.embedding_model,
            expected_embedding_count=generation.embedding_count,
        )
        self._verify_generation_binding(handle, generation)
        return handle

    def _verify_candidate_capacity(self, generation: RemoteGeneration) -> None:
        sidecar = generation.vector_sidecar
        if sidecar is None:
            inline_bytes = generation.embedding_count * EMBEDDING_DIMENSION * 4
            if inline_bytes > self.store.maximum_vector_bytes:
                raise RuntimeError("legacy inline vectors exceed the configured size limit")
            candidate_resident_bytes = generation.embedding_count * VECTOR_ROW_RESIDENT_BYTES
        else:
            if sidecar.size_bytes > self.store.maximum_vector_sidecar_bytes:
                raise RuntimeError("v5 vector sidecar exceeds the configured file limit")
            candidate_resident_bytes = generation.embedding_count * VECTOR_NORM_RESIDENT_BYTES
        available_resident_bytes = (
            self.store.maximum_resident_vector_bytes - self.store.resident_vector_bytes
        )
        if candidate_resident_bytes > available_resident_bytes:
            raise RuntimeError(
                "candidate plus resident/pinned vector memory exceeds the resident limit"
            )

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
        sidecar = generation.vector_sidecar
        if sidecar is None:
            if (
                handle.vector_sidecar_path is not None
                or metadata.vector_sidecar_sha256 is not None
                or metadata.vector_sidecar_size_bytes is not None
            ):
                raise RuntimeError("legacy database unexpectedly declares a vector sidecar")
        else:
            if (
                handle.vector_sidecar_path is None
                or metadata.vector_sidecar_sha256 != sidecar.sha256
                or metadata.vector_sidecar_size_bytes != sidecar.size_bytes
                or handle.vectors.matrix.shape
                != (generation.embedding_count, generation.embedding_dimension)
                or handle.vectors.norms.shape != (generation.embedding_count,)
            ):
                raise RuntimeError(
                    "database/handle vector sidecar differs from generation manifest"
                )
            _verify_artifact(handle.vector_sidecar_path, sidecar)
        if generation.serving_schema == "cardrag.serving-db.v5":
            aggregation_profile = generation.document_aggregation_profile
            if aggregation_profile is None:
                if (
                    metadata.document_aggregation_status != "candidate_default"
                    or metadata.document_aggregation_policy != "max_child"
                    or metadata.sealed_profile_sha256 is not None
                ):
                    raise RuntimeError(
                        "unsealed v5 manifest differs from database aggregation identity"
                    )
            elif (
                metadata.document_aggregation_status != "sealed"
                or metadata.document_aggregation_policy != aggregation_profile.aggregation_policy
                or generation.document_aggregation_policy != aggregation_profile.aggregation_policy
                or metadata.sealed_profile_sha256 != generation.sealed_profile_sha256
                or metadata.exact_row_corpus_sha256 != generation.exact_row_corpus_sha256
            ):
                raise RuntimeError("sealed v5 manifest differs from database aggregation identity")
            self._verify_v5_semantic_binding(handle, generation)
        documents, counts, issuer_codes = self._database_contract(handle)
        declared = {item.document_id: item for item in generation.documents}
        if len(declared) != len(generation.documents):
            raise RuntimeError("generation manifest contains duplicate document IDs")
        if set(declared) != {item[0] for item in documents}:
            raise RuntimeError("manifest and database document sets differ")
        for (
            document_id,
            issuer,
            page_count,
            pdf_sha256,
            pdf_size_bytes,
            availability,
            reason_code,
            reason,
            attempts,
        ) in documents:
            manifest_document = declared[document_id]
            artifact = manifest_document.pdf
            if (
                manifest_document.issuer != issuer
                or manifest_document.page_count != page_count
                or artifact.sha256 != pdf_sha256
                or artifact.size_bytes != pdf_size_bytes
                or artifact.media_type != "application/pdf"
                or manifest_document.availability != availability
                or manifest_document.failure_reason_code != reason_code
                or manifest_document.failure_reason != reason
                or manifest_document.failure_attempts != attempts
            ):
                raise RuntimeError("manifest and database PDF references differ")
        available_documents = [
            document for document in generation.documents if document.availability == "available"
        ]
        expected_counts = (
            len(generation.issuer_codes),
            generation.document_count,
            len(available_documents),
            sum(document.page_count for document in available_documents),
            generation.chunk_count,
            generation.pdf_object_count,
        )
        if issuer_codes != generation.issuer_codes or counts != expected_counts:
            raise RuntimeError("manifest and database corpus counts differ")

    @staticmethod
    def _verify_v5_semantic_binding(
        handle: GenerationHandle,
        generation: RemoteGeneration,
    ) -> None:
        structure = generation.structure_contract
        sidecar = generation.vector_sidecar_contract
        if structure is None or sidecar is None:
            raise RuntimeError("v5 generation semantic contract is absent")
        node_counts = structure.node_counts
        major_counts = structure.major_class_counts
        revision_counts = structure.revision_counts
        expected_metadata: dict[str, str] = {
            "parser_policy_sha256": str(generation.parser_policy_sha256),
            "embedding_policy_sha256": str(generation.embedding_policy_sha256),
            "retrieval_policy_sha256": str(generation.retrieval_policy_sha256),
            "primary_embedding_profile_id": str(generation.primary_embedding_profile_id),
            "embedding_profile_count": str(len(generation.embedding_profiles)),
            "vector_sidecar_profile_id": sidecar.profile_id,
            "vector_sidecar_row_count": str(sidecar.row_count),
            "vector_sidecar_dimension": str(sidecar.dimension),
            "vector_sidecar_dtype": sidecar.dtype,
            "vector_sidecar_normalization": sidecar.normalization,
            "vector_sidecar_byte_order": sidecar.byte_order,
            "vector_sidecar_layout": sidecar.layout,
            "source_non_whitespace_count": str(
                structure.source_coverage.source_non_whitespace_characters
            ),
            "covered_non_whitespace_count": str(
                structure.source_coverage.covered_non_whitespace_characters
            ),
            "source_coverage_sha256": (structure.source_coverage.source_non_whitespace_sha256),
            "structure_node_count": str(node_counts.total),
            "contract_revision_count": str(revision_counts.total),
            "current_revision_count": str(revision_counts.current),
            "superseded_revision_count": str(revision_counts.superseded),
            "ambiguous_revision_count": str(revision_counts.ambiguous),
        }
        if generation.document_aggregation_profile is not None:
            expected_metadata.update(
                {
                    "document_aggregation_status": "sealed",
                    "document_aggregation_policy": (
                        generation.document_aggregation_profile.aggregation_policy
                    ),
                    "sealed_profile_sha256": str(generation.sealed_profile_sha256),
                    "exact_row_corpus_sha256": str(generation.exact_row_corpus_sha256),
                }
            )
        for profile in structure.parser_profiles:
            expected_metadata[f"parser_profile_id.{profile.issuer}"] = profile.profile_id
            expected_metadata[f"parser_profile_sha256.{profile.issuer}"] = profile.profile_sha256
        for node_type, count in (
            ("ROOT", node_counts.root),
            ("MAJOR_SECTION", node_counts.major_section),
            ("ITEM", node_counts.item),
            ("PARAGRAPH", node_counts.paragraph),
            ("LIST_ITEM", node_counts.list_item),
            ("TABLE", node_counts.table),
            ("TABLE_ROW", node_counts.table_row),
            ("FOOTNOTE", node_counts.footnote),
            ("BOILERPLATE", node_counts.boilerplate),
            ("UNCLASSIFIED", node_counts.unclassified),
        ):
            expected_metadata[f"structure_node_count.{node_type}"] = str(count)
        for major_class, count in (
            ("BENEFIT", major_counts.benefit),
            ("NOTICE", major_counts.notice),
            ("MIXED", major_counts.mixed),
            ("UNKNOWN", major_counts.unknown),
        ):
            expected_metadata[f"structure_major_class_count.{major_class}"] = str(count)
        for view_count in generation.embedding_view_counts:
            expected_metadata[f"embedding_view_count.{view_count.view_type}"] = str(
                view_count.count
            )
        with handle.connect() as connection:
            metadata = {
                str(row[0]): str(row[1])
                for row in connection.execute("SELECT key,value FROM metadata")
            }
            profile_rows = tuple(
                tuple(row)
                for row in connection.execute(
                    """SELECT profile_id,provider,model,provider_id,dimension,dtype,
                              normalization,document_policy,query_policy,maximum_tokens
                         FROM embedding_profiles ORDER BY profile_id"""
                )
            )
            database_view_counts = {
                str(row[0]): int(row[1])
                for row in connection.execute(
                    "SELECT view_type,count(*) FROM embedding_views GROUP BY view_type"
                )
            }
        if any(metadata.get(key) != value for key, value in expected_metadata.items()):
            raise RuntimeError("v5 database metadata differs from semantic manifest contract")
        expected_profiles = tuple(
            (
                profile.profile_id,
                profile.provider,
                profile.model,
                profile.provider_id,
                profile.dimension,
                profile.dtype,
                profile.normalization,
                profile.document_policy,
                profile.query_policy,
                profile.maximum_tokens,
            )
            for profile in generation.embedding_profiles
        )
        expected_view_counts = {
            row.view_type: row.count for row in generation.embedding_view_counts
        }
        if profile_rows != expected_profiles or database_view_counts != {
            key: value for key, value in expected_view_counts.items() if value
        }:
            raise RuntimeError("v5 database profiles/views differ from semantic manifest contract")

    @staticmethod
    def _database_contract(
        handle: GenerationHandle,
    ) -> tuple[
        list[tuple[str, str, int, str, int, str, str | None, str | None, int | None]],
        tuple[int, int, int, int, int, int],
        tuple[str, ...],
    ]:
        with handle.connect() as connection:
            if handle.metadata.schema_id == "cardrag.serving-db.v5":
                document_rows = connection.execute(
                    """SELECT r.document_id,l.issuer,r.page_count,r.pdf_sha256,r.pdf_size_bytes
                         FROM contract_revisions AS r
                         JOIN product_lineages AS l
                           ON l.product_lineage_id=r.product_lineage_id
                         ORDER BY r.document_id"""
                )
            else:
                document_rows = connection.execute(
                    "SELECT document_id,issuer,page_count,pdf_sha256,pdf_size_bytes "
                    "FROM documents ORDER BY document_id"
                )
            documents: list[
                tuple[str, str, int, str, int, str, str | None, str | None, int | None]
            ] = [
                (
                    str(row[0]),
                    str(row[1]),
                    int(row[2]),
                    str(row[3]),
                    int(row[4]),
                    "available",
                    None,
                    None,
                    None,
                )
                for row in document_rows
            ]
            if handle.metadata.schema_id in {
                "cardrag.serving-db.v4",
                "cardrag.serving-db.v5",
            }:
                documents.extend(
                    (
                        str(row[0]),
                        str(row[1]),
                        int(row[2]),
                        str(row[3]),
                        int(row[4]),
                        "ocr_failed",
                        str(row[5]),
                        str(row[6]),
                        int(row[7]),
                    )
                    for row in connection.execute(
                        """SELECT document_id,issuer,page_count,pdf_sha256,pdf_size_bytes,
                                  reason_code,reason,attempts
                             FROM ocr_failed_products ORDER BY document_id"""
                    )
                )
                documents.sort(key=lambda row: row[0])
            issuer_codes = tuple(
                str(row[0]) for row in connection.execute("SELECT code FROM issuers ORDER BY code")
            )
            if handle.metadata.schema_id == "cardrag.serving-db.v5":
                raw_counts = connection.execute(
                    """
                    SELECT
                      (SELECT count(*) FROM issuers),
                      (SELECT count(*) FROM contract_revisions) +
                        (SELECT count(*) FROM ocr_failed_products),
                      (SELECT count(*) FROM contract_revisions),
                      (SELECT count(*) FROM document_pages),
                      (SELECT count(*) FROM embedding_views),
                      (SELECT count(*) FROM (
                         SELECT pdf_sha256 FROM contract_revisions
                         UNION SELECT pdf_sha256 FROM ocr_failed_products
                       ))
                    """
                ).fetchone()
            elif handle.metadata.schema_id == "cardrag.serving-db.v4":
                raw_counts = connection.execute(
                    """
                    SELECT
                      (SELECT count(*) FROM issuers),
                      (SELECT count(*) FROM products) +
                        (SELECT count(*) FROM ocr_failed_products),
                      (SELECT count(*) FROM documents),
                      (SELECT count(*) FROM pages),
                      (SELECT count(*) FROM evidence),
                      (SELECT count(*) FROM (
                         SELECT pdf_sha256 FROM documents
                         UNION SELECT pdf_sha256 FROM ocr_failed_products
                       ))
                    """
                ).fetchone()
            else:
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
        reservation = await asyncio.to_thread(
            self.store.reserve_state_capacity,
            artifact.size_bytes,
        )
        try:
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
        finally:
            await asyncio.to_thread(reservation.release)

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
