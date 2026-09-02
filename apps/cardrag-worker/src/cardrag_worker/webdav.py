"""Async worker facade over cardrag-core's verified WebDAV publishers."""

from __future__ import annotations

import hashlib
import os
import tempfile
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from urllib.parse import unquote, urlsplit

from cardrag_core import (
    ArtifactRef,
    CASPublisher,
    GenerationManifest,
    GenerationPointer,
    GenerationReady,
    ImmutablePublisher,
    MCPArtifactReader,
    StablePointerPublisher,
    WebDAVHTTPError,
    WebDAVSettings,
    canonical_json_bytes,
    channel_pointer_path,
    generation_database_path,
    generation_manifest_path,
    generation_ready_path,
    generation_vectors_path,
    object_path,
    validate_relative_path,
)
from cardrag_core import (
    WebDAVClient as CoreWebDAVClient,
)
from defusedxml import ElementTree  # type: ignore[import-untyped]

from .async_utils import to_thread_fenced


class WebDAVError(RuntimeError):
    pass


CONTROL_OBJECT_MAX_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class WebDAVCheck:
    reachable: bool
    operations: tuple[str, ...]
    overwrite_false_conflict_status: int


@dataclass(frozen=True, slots=True)
class RemoteGenerationIdentity:
    generation_id: str
    corpus_sha256: str
    contract_sha256: str
    generation_schema: Literal[
        "cardrag.generation.v1",
        "cardrag.generation.v2",
        "cardrag.generation.v3",
        "cardrag.generation.v4",
        "cardrag.generation.v5",
    ] = "cardrag.generation.v3"
    serving_schema: Literal[
        "cardrag.serving-db.v1",
        "cardrag.serving-db.v2",
        "cardrag.serving-db.v3",
        "cardrag.serving-db.v4",
        "cardrag.serving-db.v5",
    ] = "cardrag.serving-db.v3"
    ocr_failed_document_count: int = 0

    def __post_init__(self) -> None:
        if self.ocr_failed_document_count < 0:
            raise ValueError("remote OCR-failed document count must be non-negative")
        expected = {
            "cardrag.generation.v1": "cardrag.serving-db.v1",
            "cardrag.generation.v2": "cardrag.serving-db.v2",
            "cardrag.generation.v3": "cardrag.serving-db.v3",
            "cardrag.generation.v4": "cardrag.serving-db.v4",
            "cardrag.generation.v5": "cardrag.serving-db.v5",
        }[self.generation_schema]
        if self.serving_schema != expected:
            raise ValueError("remote generation and serving schema versions must match")


class WebDAVClient:
    """Coroutine-friendly facade; all byte publication remains in cardrag-core."""

    def __init__(
        self,
        client: CoreWebDAVClient,
        *,
        channel: str = "stable",
        stable_publication_approved: bool = False,
    ) -> None:
        if type(stable_publication_approved) is not bool:
            raise ValueError("stable publication approval must be boolean")
        self.core = client
        self.channel = channel
        self.stable_publication_approved = stable_publication_approved
        self.pointer_path = channel_pointer_path(channel)
        self.immutable = ImmutablePublisher(client)
        self.cas = CASPublisher(client)
        self.stable = StablePointerPublisher(client, channel=channel)

    @classmethod
    def from_env(cls, *, stable_publication_approved: bool = False) -> WebDAVClient:
        return cls(
            CoreWebDAVClient(WebDAVSettings.from_env()),
            channel=os.environ.get("CARDRAG_CHANNEL", "stable"),
            stable_publication_approved=stable_publication_approved,
        )

    async def close(self) -> None:
        await to_thread_fenced(self.core.close)

    async def get_bytes(
        self,
        path: str | PurePosixPath,
        *,
        max_bytes: int | None = CONTROL_OBJECT_MAX_BYTES,
    ) -> bytes | None:
        try:
            response = await to_thread_fenced(self.core.get, path, max_bytes=max_bytes)
        except WebDAVHTTPError as exc:
            if exc.status_code == 404:
                return None
            raise
        return response.content

    async def exists(self, path: str | PurePosixPath) -> bool:
        """Check object existence through the capability-limited read-only facade."""

        return await to_thread_fenced(self.core.read_only().exists, path)

    async def get_json(
        self,
        path: str | PurePosixPath,
        *,
        max_bytes: int = CONTROL_OBJECT_MAX_BYTES,
    ) -> dict[str, Any] | None:
        body = await self.get_bytes(path, max_bytes=max_bytes)
        if body is None:
            return None
        import json

        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise WebDAVError(f"WebDAV object {path} is not a JSON object")
        return payload

    async def list_children(self, path: str | PurePosixPath) -> tuple[PurePosixPath, ...]:
        relative = validate_relative_path(path)

        def propfind() -> tuple[PurePosixPath, ...]:
            response = self.core.propfind(relative, depth="1")
            try:
                root = ElementTree.fromstring(response.content)
            except ElementTree.ParseError as exc:
                raise WebDAVError(f"invalid PROPFIND XML for {relative}") from exc
            base_path = unquote(urlsplit(self.core.settings.base_url).path).rstrip("/") + "/"
            children: set[PurePosixPath] = set()
            for node in root.findall("{DAV:}response"):
                href = node.findtext("{DAV:}href")
                if not href:
                    raise WebDAVError("PROPFIND response is missing DAV:href")
                decoded = unquote(urlsplit(href).path)
                if not decoded.startswith(base_path):
                    raise WebDAVError("PROPFIND href escaped configured WebDAV base")
                child_raw = decoded[len(base_path) :].strip("/")
                if not child_raw:
                    continue
                child = validate_relative_path(child_raw)
                if child != relative:
                    children.add(child)
            return tuple(sorted(children, key=lambda item: item.as_posix()))

        return await to_thread_fenced(propfind)

    async def delete(self, path: str | PurePosixPath, *, missing_ok: bool = False) -> None:
        await to_thread_fenced(self.core.delete, path, missing_ok=missing_ok)

    async def put_bytes(
        self,
        path: str | PurePosixPath,
        body: bytes,
        *,
        content_type: str,
        immutable: bool = True,
    ) -> None:
        if not immutable:
            raise ValueError("mutable direct PUT is forbidden; use stable pointer publication")
        await to_thread_fenced(
            self.immutable.publish_bytes,
            path,
            body,
            media_type=content_type,
        )

    async def put_json(
        self,
        path: str | PurePosixPath,
        payload: Mapping[str, Any],
        *,
        immutable: bool = True,
    ) -> bytes:
        if not immutable:
            raise ValueError("only StablePointerPublisher may replace stable.json")
        body = canonical_json_bytes(dict(payload))
        await self.put_bytes(path, body, content_type="application/json", immutable=True)
        return body

    async def put_cas(self, body: bytes, *, media_type: str = "application/octet-stream") -> tuple[str, str]:
        artifact = await to_thread_fenced(self.cas.publish_bytes, body, media_type=media_type)
        return artifact.sha256, artifact.path

    async def put_cas_file(
        self,
        path: Path,
        *,
        media_type: str = "application/octet-stream",
        expected_sha256: str | None = None,
        expected_size_bytes: int | None = None,
    ) -> tuple[str, str]:
        artifact = await to_thread_fenced(
            self.cas.publish_file,
            path,
            media_type=media_type,
            expected_sha256=expected_sha256,
            expected_size_bytes=expected_size_bytes,
        )
        if (
            artifact.path != object_path(artifact.sha256).as_posix()
            or artifact.media_type != media_type
            or (expected_sha256 is not None and artifact.sha256 != expected_sha256)
            or (expected_size_bytes is not None and artifact.size_bytes != expected_size_bytes)
        ):
            raise WebDAVError("CAS publisher returned a mismatched artifact identity")
        return artifact.sha256, artifact.path

    async def check(self) -> WebDAVCheck:
        """Exercise the full read/write/MOVE contract and clean up the probe."""

        def probe() -> WebDAVCheck:
            token = uuid.uuid4().hex
            root = PurePosixPath("v1", ".health", token)
            source = root / "source.bin"
            moved = root / "moved.bin"
            blocker = root / "blocker.bin"
            operations: list[str] = []
            conflict_status = 0
            self.core.ensure_collection(root)
            operations.append("MKCOL")
            try:
                self.core.propfind(root, depth="0")
                operations.append("PROPFIND")
                self.immutable.publish_bytes(
                    source, b"cardrag-webdav-probe", media_type="application/octet-stream"
                )
                operations.append("PUT")
                if self.core.get(source).content != b"cardrag-webdav-probe":
                    raise WebDAVError("WebDAV probe GET returned different bytes")
                operations.append("GET")
                self.core.head(source)
                operations.append("HEAD")
                self.immutable.publish_bytes(blocker, b"occupied", media_type="application/octet-stream")
                self.core.move(source, moved, overwrite=False)
                operations.append("MOVE")
                try:
                    self.core.move(moved, blocker, overwrite=False)
                except WebDAVHTTPError as exc:
                    conflict_status = exc.status_code
                    if exc.status_code not in {409, 412}:
                        raise
                else:
                    raise WebDAVError("WebDAV server ignored Overwrite:F destination conflict")
                operations.append("MOVE_OVERWRITE_F_CONFLICT")
            finally:
                for path in (source, moved, blocker):
                    self.core.delete(path, missing_ok=True)
                self.core.delete(root, missing_ok=True)
                operations.append("DELETE")
            return WebDAVCheck(True, tuple(operations), conflict_status)

        return await to_thread_fenced(probe)

    async def validated_current_generation(self) -> RemoteGenerationIdentity | None:
        """Stream-hash the DB and every referenced CAS object before no-change."""

        def verify() -> RemoteGenerationIdentity:
            reader = MCPArtifactReader(self.core.read_only(), channel=self.channel)
            current = reader.read_current_generation()
            with tempfile.TemporaryDirectory(prefix="cardrag-current-verify-") as directory:
                root = Path(directory).resolve()
                reader.download_serving_database(root / "index.sqlite3", current=current)
                if current.manifest.schema_version == "cardrag.generation.v5":
                    reader.download_vector_sidecar(root / "vectors.f32", current=current)
                references = {
                    (document.pdf.sha256, document.pdf.path): document.pdf
                    for document in current.manifest.documents
                }
                references.update(
                    {
                        (document.ocr.sha256, document.ocr.path): document.ocr
                        for document in current.manifest.documents
                        if document.ocr is not None
                    }
                )
                for index, reference in enumerate(references.values()):
                    reader.download_object(reference, root / f"object-{index:06d}")
            return RemoteGenerationIdentity(
                generation_id=current.manifest.generation_id,
                corpus_sha256=current.manifest.corpus_sha256,
                contract_sha256=current.manifest.contract_sha256,
                generation_schema=current.manifest.schema_version,
                serving_schema=current.manifest.serving_schema,
                ocr_failed_document_count=sum(
                    document.availability == "ocr_failed" for document in current.manifest.documents
                ),
            )

        try:
            return await to_thread_fenced(verify)
        except Exception:
            return None

    async def current_generation_matches(
        self,
        *,
        generation_id: str,
        corpus_sha256: str,
        contract_sha256: str,
    ) -> bool:
        current = await self.validated_current_generation()
        return bool(
            current is not None
            and current.generation_id == generation_id
            and current.corpus_sha256 == corpus_sha256
            and current.contract_sha256 == contract_sha256
        )


@dataclass(frozen=True, slots=True)
class PublishedBundle:
    generation_id: str
    index_sha256: str
    manifest_sha256: str


def _guard_v5_stable_publication(client: object, *, schema_version: str) -> None:
    if (
        schema_version == "cardrag.generation.v5"
        and getattr(client, "channel", "stable") == "stable"
        and not getattr(client, "stable_publication_approved", False)
    ):
        raise ValueError("stable v1.0.13 publication requires explicit approval")


def _require_exact_artifact(
    actual: object,
    expected: ArtifactRef,
    *,
    label: str,
) -> None:
    if not isinstance(actual, ArtifactRef) or actual != expected:
        raise WebDAVError(f"{label} publisher returned a mismatched artifact identity")


class WebDAVBundlePublisher:
    """Seal immutable members, with an explicit capability for v5 stable publication."""

    def __init__(self, client: WebDAVClient) -> None:
        self.client = client

    async def publish(
        self,
        *,
        generation_id: str,
        database: Path,
        manifest: Mapping[str, Any],
        vectors: Path | None = None,
    ) -> PublishedBundle:
        manifest_body = canonical_json_bytes(dict(manifest))
        validated_manifest = GenerationManifest.model_validate_json(manifest_body)
        if validated_manifest.generation_id != generation_id:
            raise ValueError("generation manifest ID does not match publication target")
        _guard_v5_stable_publication(
            self.client,
            schema_version=validated_manifest.schema_version,
        )
        manifest_sha = hashlib.sha256(manifest_body).hexdigest()
        database_artifact = validated_manifest.serving_database
        if (
            database_artifact.path != generation_database_path(generation_id).as_posix()
            or database_artifact.media_type != "application/vnd.sqlite3"
        ):
            raise ValueError("manifest serving_database is not bound to index.sqlite3")
        database_sha = database_artifact.sha256
        database_size = database_artifact.size_bytes
        vector_sha: str | None = None
        vector_size: int | None = None
        vector_artifact: ArtifactRef | None = None
        if validated_manifest.schema_version == "cardrag.generation.v5":
            if vectors is None or validated_manifest.vector_sidecar is None:
                raise ValueError("v5 publication requires a vector sidecar")
            vector_artifact = validated_manifest.vector_sidecar.artifact
            if (
                vector_artifact.path != generation_vectors_path(generation_id).as_posix()
                or vector_artifact.media_type != "application/octet-stream"
            ):
                raise ValueError("manifest vector_sidecar is not bound to vectors.f32")
            vector_sha = vector_artifact.sha256
            vector_size = vector_artifact.size_bytes
        elif vectors is not None or validated_manifest.vector_sidecar is not None:
            raise ValueError("legacy generation publication cannot contain a vector sidecar")
        ready = GenerationReady(
            generation_id=generation_id,
            manifest_sha256=manifest_sha,
            serving_database_sha256=database_sha,
            serving_database_size_bytes=database_size,
            vector_sidecar_sha256=vector_sha,
            vector_sidecar_size_bytes=vector_size,
        )
        ready_body = ready.canonical_bytes()

        # A retry reuses these exact sealed bytes; core read-back verifies existing objects.
        published_database = await to_thread_fenced(
            self.client.immutable.publish_file,
            generation_database_path(generation_id),
            database,
            media_type="application/vnd.sqlite3",
            expected_sha256=database_sha,
            expected_size_bytes=database_size,
        )
        _require_exact_artifact(published_database, database_artifact, label="serving database")
        if vectors is not None:
            assert vector_artifact is not None
            published_vectors = await to_thread_fenced(
                self.client.immutable.publish_file,
                generation_vectors_path(generation_id),
                vectors,
                media_type="application/octet-stream",
                expected_sha256=vector_artifact.sha256,
                expected_size_bytes=vector_artifact.size_bytes,
            )
            _require_exact_artifact(published_vectors, vector_artifact, label="vector sidecar")
        expected_manifest = ArtifactRef(
            sha256=manifest_sha,
            size_bytes=len(manifest_body),
            media_type="application/json",
            path=generation_manifest_path(generation_id).as_posix(),
        )
        published_manifest = await to_thread_fenced(
            self.client.immutable.publish_bytes,
            generation_manifest_path(generation_id),
            manifest_body,
            media_type="application/json",
        )
        _require_exact_artifact(published_manifest, expected_manifest, label="generation manifest")
        expected_ready = ArtifactRef(
            sha256=hashlib.sha256(ready_body).hexdigest(),
            size_bytes=len(ready_body),
            media_type="application/json",
            path=generation_ready_path(generation_id).as_posix(),
        )
        published_ready = await to_thread_fenced(
            self.client.immutable.publish_bytes,
            generation_ready_path(generation_id),
            ready_body,
            media_type="application/json",
        )
        _require_exact_artifact(published_ready, expected_ready, label="generation READY")
        pointer = GenerationPointer(
            generation_id=generation_id,
            manifest_sha256=manifest_sha,
            ready_sha256=expected_ready.sha256,
        )
        pointer_body = pointer.canonical_bytes()
        expected_pointer = ArtifactRef(
            sha256=hashlib.sha256(pointer_body).hexdigest(),
            size_bytes=len(pointer_body),
            media_type="application/json",
            path=channel_pointer_path(getattr(self.client, "channel", "stable")).as_posix(),
        )
        published_pointer = await to_thread_fenced(
            self.client.stable.atomic_replace_bytes,
            pointer_body,
        )
        _require_exact_artifact(published_pointer, expected_pointer, label="channel pointer")
        return PublishedBundle(generation_id, database_sha, manifest_sha)
