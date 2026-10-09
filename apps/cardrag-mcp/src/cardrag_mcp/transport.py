"""Adapter from the shared read-only artifact facade to the updater protocol."""

from __future__ import annotations

import asyncio
import shutil
import uuid
from collections.abc import Callable
from pathlib import Path, PurePosixPath

from cardrag_core import (
    STABLE_POINTER_PATH,
    ArtifactRef,
    CurrentGeneration,
    GenerationManifest,
    GenerationPointer,
    GenerationReady,
    MCPArtifactReader,
    ReadOnlyWebDAVClient,
    WebDAVClient,
    WebDAVHTTPError,
    WebDAVSettings,
    channel_pointer_path,
    generation_database_path,
    generation_manifest_path,
    generation_ready_path,
    generation_vectors_path,
    sha256_bytes,
    validate_relative_path,
)
from pydantic import SecretStr

from cardrag_mcp.config import Settings
from cardrag_mcp.updater import RemoteArtifact, RemoteDocument, RemoteGeneration


async def _cancellation_fenced_to_thread[**P, T](
    function: Callable[P, T],
    *args: P.args,
    **kwargs: P.kwargs,
) -> T:
    """Do not release a storage reservation while its blocking write still runs."""

    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        if task.done() and not task.cancelled():
            task.exception()
        raise


def _artifact(value: ArtifactRef) -> RemoteArtifact:
    return RemoteArtifact(
        path=value.path,
        sha256=value.sha256,
        size_bytes=value.size_bytes,
        media_type=value.media_type,
    )


def manifest_to_remote_generation(manifest: GenerationManifest) -> RemoteGeneration:
    contract = manifest.embedding_contract
    vector_sidecar = manifest.vector_sidecar
    return RemoteGeneration(
        generation_id=manifest.generation_id,
        serving_schema=manifest.serving_schema,
        corpus_sha256=manifest.corpus_sha256,
        contract_sha256=manifest.contract_sha256,
        database=_artifact(manifest.serving_database),
        documents=tuple(
            RemoteDocument(
                document_id=item.document_id,
                issuer=item.issuer,
                page_count=item.page_count,
                pdf=_artifact(item.pdf),
                ocr_sha256=None if item.ocr is None else item.ocr.sha256,
                availability=item.availability or "available",
                failure_reason_code=(
                    None if item.ocr_failure is None else item.ocr_failure.reason_code
                ),
                failure_reason=None if item.ocr_failure is None else item.ocr_failure.reason,
                failure_attempts=(None if item.ocr_failure is None else item.ocr_failure.attempts),
            )
            for item in manifest.documents
        ),
        issuer_codes=manifest.issuer_codes,
        document_count=manifest.counts.documents,
        pdf_object_count=manifest.counts.pdf_objects,
        ocr_object_count=manifest.counts.ocr_objects,
        chunk_count=manifest.counts.chunks,
        embedding_provider=contract.provider,
        embedding_model=contract.model,
        embedding_dimension=contract.dimension,
        embedding_count=contract.count,
        issuer_ocr_counts=tuple(
            (row.issuer, row.acquired, row.succeeded, row.failed)
            for row in manifest.issuer_ocr_counts
        ),
        vector_sidecar=(None if vector_sidecar is None else _artifact(vector_sidecar.artifact)),
        structure_contract=manifest.structure_contract,
        embedding_profiles=manifest.embedding_profiles,
        primary_embedding_profile_id=manifest.primary_embedding_profile_id,
        embedding_view_counts=manifest.embedding_view_counts,
        vector_sidecar_contract=manifest.vector_sidecar,
        parser_policy_sha256=manifest.parser_policy_sha256,
        embedding_policy_sha256=manifest.embedding_policy_sha256,
        retrieval_policy_sha256=manifest.retrieval_policy_sha256,
        document_aggregation_profile=manifest.document_aggregation_profile,
        document_aggregation_policy=manifest.document_aggregation_policy,
        sealed_profile_sha256=manifest.sealed_profile_sha256,
        exact_row_corpus_sha256=manifest.exact_row_corpus_sha256,
    )


class CoreArtifactReader:
    """Async boundary around cardrag_core's synchronous, hash-verifying facade."""

    def __init__(self, reader: MCPArtifactReader, client: ReadOnlyWebDAVClient) -> None:
        self._reader = reader
        self._client = client
        self._pointer_path = getattr(reader, "pointer_path", STABLE_POINTER_PATH)
        self._current: CurrentGeneration | None = None
        self._last_etag: str | None = None
        self._last_remote: RemoteGeneration | None = None

    async def read_stable_generation(self) -> RemoteGeneration:
        etag: str | None = None
        try:
            stat = await asyncio.to_thread(self._client.head, self._pointer_path)
            etag = stat.etag
        except WebDAVHTTPError as exc:
            if exc.status_code != 405:
                raise
        if etag is not None and etag == self._last_etag and self._last_remote is not None:
            return self._last_remote
        current = await asyncio.to_thread(self._reader.read_current_generation)
        self._current = current
        remote = manifest_to_remote_generation(current.manifest)
        self._last_etag = etag
        self._last_remote = remote
        return remote

    async def download_database(self, generation: RemoteGeneration, destination: Path) -> None:
        current = self._current
        if current is None or current.manifest.generation_id != generation.generation_id:
            raise RuntimeError("stable generation changed before database download")
        await _cancellation_fenced_to_thread(
            self._reader.download_serving_database,
            destination,
            current=current,
        )

    async def download_vector_sidecar(
        self,
        generation: RemoteGeneration,
        destination: Path,
    ) -> None:
        current = self._current
        if current is None or current.manifest.generation_id != generation.generation_id:
            raise RuntimeError("stable generation changed before vector sidecar download")
        if generation.vector_sidecar is None:
            raise RuntimeError("remote generation does not declare a vector sidecar")
        await _cancellation_fenced_to_thread(
            self._reader.download_vector_sidecar,
            destination,
            current=current,
        )

    async def download_object(self, artifact: RemoteArtifact, destination: Path) -> None:
        reference = ArtifactRef(
            path=artifact.path,
            sha256=artifact.sha256,
            size_bytes=artifact.size_bytes,
            media_type=artifact.media_type,
        )
        await _cancellation_fenced_to_thread(
            self._reader.download_object,
            reference,
            destination,
        )

    async def close(self) -> None:
        await asyncio.to_thread(self._client.close)


class LocalArtifactReader:
    """Read-only artifact reader reading directly from a local serving volume."""

    def __init__(self, serving_dir: Path, *, channel: str = "stable") -> None:
        self._serving_dir = serving_dir.resolve()
        self._channel = channel
        self._pointer_rel_path = channel_pointer_path(channel)
        self._pointer_path = self._serving_dir / self._pointer_rel_path
        self._last_pointer_bytes: bytes | None = None
        self._last_remote: RemoteGeneration | None = None

    def _resolve_safe_path(self, relative: str | PurePosixPath) -> Path:
        rel = validate_relative_path(relative)
        full = (self._serving_dir / rel).resolve()
        if full != self._serving_dir and self._serving_dir not in full.parents:
            raise RuntimeError(f"artifact path escapes serving directory: {relative}")
        return full

    async def read_stable_generation(self) -> RemoteGeneration | None:
        return await asyncio.to_thread(self._read_stable_generation_sync)

    def _read_stable_generation_sync(self) -> RemoteGeneration | None:
        if not self._pointer_path.exists():
            return None
        try:
            pointer_bytes = self._pointer_path.read_bytes()
        except OSError:
            return None
        if (
            self._last_pointer_bytes is not None
            and pointer_bytes == self._last_pointer_bytes
            and self._last_remote is not None
        ):
            return self._last_remote

        try:
            pointer = GenerationPointer.model_validate_json(pointer_bytes)
        except Exception as exc:
            raise RuntimeError("local stable generation pointer is malformed") from exc
        if pointer.canonical_bytes() != pointer_bytes:
            raise RuntimeError("local stable generation pointer is not canonical JSON")

        ready_path = self._resolve_safe_path(generation_ready_path(pointer.generation_id))
        if not ready_path.exists():
            raise RuntimeError("local generation READY file is missing")
        ready_bytes = ready_path.read_bytes()
        if sha256_bytes(ready_bytes) != pointer.ready_sha256:
            raise RuntimeError("local pointer does not bind generation READY")
        try:
            ready = GenerationReady.model_validate_json(ready_bytes)
        except Exception as exc:
            raise RuntimeError("local generation READY is malformed") from exc
        if ready.generation_id != pointer.generation_id:
            raise RuntimeError("local generation READY ID does not match pointer")
        if ready.canonical_bytes() != ready_bytes:
            raise RuntimeError("local generation READY is not canonical JSON")
        if ready.manifest_sha256 != pointer.manifest_sha256:
            raise RuntimeError("local pointer and generation READY disagree on manifest SHA")

        manifest_path = self._resolve_safe_path(generation_manifest_path(pointer.generation_id))
        if not manifest_path.exists():
            raise RuntimeError("local generation manifest is missing")
        manifest_bytes = manifest_path.read_bytes()
        if sha256_bytes(manifest_bytes) != ready.manifest_sha256:
            raise RuntimeError("local generation manifest SHA does not match READY")
        try:
            manifest = GenerationManifest.model_validate_json(manifest_bytes)
        except Exception as exc:
            raise RuntimeError(f"local generation manifest is malformed: {exc}") from exc
        if manifest.generation_id != pointer.generation_id:
            raise RuntimeError("local generation manifest ID does not match READY")
        if manifest.canonical_bytes() != manifest_bytes:
            raise RuntimeError("local generation manifest is not canonical JSON")

        database = manifest.serving_database
        if (
            ready.serving_database_sha256 != database.sha256
            or ready.serving_database_size_bytes != database.size_bytes
        ):
            raise RuntimeError("local generation READY does not bind serving database")
        vector_sidecar = manifest.vector_sidecar
        if manifest.schema_version in {"cardrag.generation.v5", "cardrag.generation.v6"}:
            if vector_sidecar is None:
                raise RuntimeError("local v5 generation manifest does not declare vector sidecar")
            if (
                ready.vector_sidecar_sha256 != vector_sidecar.artifact.sha256
                or ready.vector_sidecar_size_bytes != vector_sidecar.artifact.size_bytes
            ):
                raise RuntimeError("local generation READY does not bind vector sidecar")

        remote = manifest_to_remote_generation(manifest)
        self._last_pointer_bytes = pointer_bytes
        self._last_remote = remote
        return remote

    async def download_database(self, generation: RemoteGeneration, destination: Path) -> None:
        source_rel = generation_database_path(generation.generation_id)
        source = self._resolve_safe_path(source_rel)
        await _cancellation_fenced_to_thread(self._copy_artifact, source, destination)

    async def download_vector_sidecar(
        self, generation: RemoteGeneration, destination: Path
    ) -> None:
        if generation.vector_sidecar is None:
            raise RuntimeError("local generation does not declare a vector sidecar")
        source_rel = generation_vectors_path(generation.generation_id)
        source = self._resolve_safe_path(source_rel)
        await _cancellation_fenced_to_thread(self._copy_artifact, source, destination)

    async def download_object(self, artifact: RemoteArtifact, destination: Path) -> None:
        source = self._resolve_safe_path(artifact.path)
        await _cancellation_fenced_to_thread(self._copy_artifact, source, destination)

    def _copy_artifact(self, source: Path, destination: Path) -> None:
        if not source.is_file():
            raise RuntimeError(f"local artifact file not found: {source}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp_dest = destination.parent / f".tmp-{destination.name}-{uuid.uuid4().hex[:8]}"
        try:
            shutil.copyfile(source, temp_dest)
            temp_dest.replace(destination)
        finally:
            if temp_dest.exists():
                try:
                    temp_dest.unlink()
                except OSError:
                    pass

    async def close(self) -> None:
        pass


def build_core_reader(settings: Settings) -> CoreArtifactReader:
    if settings.webdav_base_url is None:
        raise ValueError("webdav_base_url is required")
    username = settings.webdav_username_value()
    password = settings.webdav_password_value()
    if username is None or password is None:  # guarded by Settings validation
        raise RuntimeError("WebDAV credentials are unavailable")
    webdav_settings = WebDAVSettings(
        environment=settings.environment,
        base_url=str(settings.webdav_base_url),
        username=username,
        password=SecretStr(password),
        connect_timeout_seconds=settings.webdav_connect_timeout_seconds,
        transfer_timeout_seconds=settings.webdav_transfer_timeout_seconds,
        ca_file=settings.webdav_ca_file,
    )
    client = WebDAVClient(webdav_settings).read_only()
    return CoreArtifactReader(MCPArtifactReader(client, channel=settings.channel), client)


def build_local_reader(settings: Settings) -> LocalArtifactReader:
    return LocalArtifactReader(settings.serving_dir, channel=settings.channel)
