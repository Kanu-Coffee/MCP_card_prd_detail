"""Async worker facade over cardrag-core's verified WebDAV publishers."""

from __future__ import annotations

import asyncio
import hashlib
import tempfile
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit

from cardrag_core import (
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
    generation_database_path,
    generation_manifest_path,
    generation_ready_path,
    sha256_file,
    validate_relative_path,
)
from cardrag_core import (
    WebDAVClient as CoreWebDAVClient,
)
from defusedxml import ElementTree  # type: ignore[import-untyped]


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


class WebDAVClient:
    """Coroutine-friendly facade; all byte publication remains in cardrag-core."""

    def __init__(self, client: CoreWebDAVClient) -> None:
        self.core = client
        self.immutable = ImmutablePublisher(client)
        self.cas = CASPublisher(client)
        self.stable = StablePointerPublisher(client)

    @classmethod
    def from_env(cls) -> WebDAVClient:
        return cls(CoreWebDAVClient(WebDAVSettings.from_env()))

    async def close(self) -> None:
        await asyncio.to_thread(self.core.close)

    async def get_bytes(
        self,
        path: str | PurePosixPath,
        *,
        max_bytes: int | None = CONTROL_OBJECT_MAX_BYTES,
    ) -> bytes | None:
        try:
            response = await asyncio.to_thread(self.core.get, path, max_bytes=max_bytes)
        except WebDAVHTTPError as exc:
            if exc.status_code == 404:
                return None
            raise
        return response.content

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

        return await asyncio.to_thread(propfind)

    async def delete(self, path: str | PurePosixPath, *, missing_ok: bool = False) -> None:
        await asyncio.to_thread(self.core.delete, path, missing_ok=missing_ok)

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
        await asyncio.to_thread(
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
        artifact = await asyncio.to_thread(self.cas.publish_bytes, body, media_type=media_type)
        return artifact.sha256, artifact.path

    async def put_cas_file(
        self, path: Path, *, media_type: str = "application/octet-stream"
    ) -> tuple[str, str]:
        artifact = await asyncio.to_thread(self.cas.publish_file, path, media_type=media_type)
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

        return await asyncio.to_thread(probe)

    async def validated_current_generation(self) -> RemoteGenerationIdentity | None:
        """Stream-hash the DB and every referenced CAS object before no-change."""

        def verify() -> RemoteGenerationIdentity:
            reader = MCPArtifactReader(self.core.read_only())
            current = reader.read_current_generation()
            with tempfile.TemporaryDirectory(prefix="cardrag-current-verify-") as directory:
                root = Path(directory).resolve()
                reader.download_serving_database(root / "index.sqlite3", current=current)
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
            )

        try:
            return await asyncio.to_thread(verify)
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


class WebDAVBundlePublisher:
    """Seal immutable generation members, then atomically replace stable.json."""

    def __init__(self, client: WebDAVClient) -> None:
        self.client = client

    async def publish(
        self,
        *,
        generation_id: str,
        database: Path,
        manifest: Mapping[str, Any],
    ) -> PublishedBundle:
        manifest_body = canonical_json_bytes(dict(manifest))
        validated_manifest = GenerationManifest.model_validate_json(manifest_body)
        if validated_manifest.generation_id != generation_id:
            raise ValueError("generation manifest ID does not match publication target")
        manifest_sha = hashlib.sha256(manifest_body).hexdigest()
        database_sha, database_size = await asyncio.to_thread(sha256_file, database)
        declared = manifest.get("serving_database")
        if (
            not isinstance(declared, dict)
            or declared.get("sha256") != database_sha
            or declared.get("size_bytes") != database_size
            or declared.get("path") != generation_database_path(generation_id).as_posix()
        ):
            raise ValueError("manifest serving_database is not bound to index.sqlite3")
        ready = GenerationReady(
            generation_id=generation_id,
            manifest_sha256=manifest_sha,
            serving_database_sha256=database_sha,
            serving_database_size_bytes=database_size,
        )
        ready_body = ready.canonical_bytes()

        # A retry reuses these exact sealed bytes; core read-back verifies existing objects.
        await asyncio.to_thread(
            self.client.immutable.publish_file,
            generation_database_path(generation_id),
            database,
            media_type="application/vnd.sqlite3",
        )
        await asyncio.to_thread(
            self.client.immutable.publish_bytes,
            generation_manifest_path(generation_id),
            manifest_body,
            media_type="application/json",
        )
        await asyncio.to_thread(
            self.client.immutable.publish_bytes,
            generation_ready_path(generation_id),
            ready_body,
            media_type="application/json",
        )
        pointer = GenerationPointer(
            generation_id=generation_id,
            manifest_sha256=manifest_sha,
            ready_sha256=hashlib.sha256(ready_body).hexdigest(),
        )
        await asyncio.to_thread(self.client.stable.atomic_replace_bytes, pointer.canonical_bytes())
        return PublishedBundle(generation_id, database_sha, manifest_sha)
