"""Adapter from the shared read-only artifact facade to the updater protocol."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

from cardrag_core import (
    STABLE_POINTER_PATH,
    ArtifactRef,
    CurrentGeneration,
    MCPArtifactReader,
    ReadOnlyWebDAVClient,
    WebDAVClient,
    WebDAVHTTPError,
    WebDAVSettings,
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
        manifest = current.manifest
        contract = manifest.embedding_contract
        vector_sidecar = manifest.vector_sidecar
        remote = RemoteGeneration(
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
                    failure_attempts=(
                        None if item.ocr_failure is None else item.ocr_failure.attempts
                    ),
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
